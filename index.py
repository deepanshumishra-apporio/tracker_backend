"""
Web API for the multi-carrier tracker.

Lookups go through tracking_service: a saved result that is still fresh is
returned from SQLite in milliseconds; otherwise the carrier is asked via the
fast HTTP fetchers (fast/), falling back to the browser scrapers. Every result
is saved, and a background refresher keeps undelivered shipments current.

Ways in:
  * GET  /api/track      — one shipment (cache, or live with refresh=true).
  * GET  /api/shipments  — search everything saved.
  * POST /api/batch  — upload a company/awb spreadsheet; rows are scraped by a
                       background pool and polled via GET /api/batch/{id}
                       (see batch.py).

Run (dev):
    uvicorn index:app --reload --port 8000
    # or: python index.py

"""
from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable, Iterator, Optional

from fastapi import (
    FastAPI,
    File,
    HTTPException,
    Query,
    UploadFile,
    status as http_status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

import batch
import browser_pool
import config
import storage
import tracking_service
from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.aramex import AramexScraper
from scrapers.dhl import DHLScraper
from scrapers.fedex import FedExScraper
from scrapers.ups import UPSScraper

# One reusable scraper instance per carrier. Tests monkeypatch entries here with
# a fake so no browser/network is needed.
SCRAPERS = {
    Carrier.UPS: UPSScraper(),
    Carrier.FEDEX: FedExScraper(),
    Carrier.DHL: DHLScraper(),
    Carrier.ARAMEX: AramexScraper(),
}

# Real carrier tracking numbers are ASCII alphanumeric, occasionally hyphenated.
_TRACKING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


# ---------------------------------------------------------------------------
# Response schema — the contract the frontend codes against.
# ---------------------------------------------------------------------------
class ShipmentOut(BaseModel):
    tracking_number: str
    carrier: Carrier
    status: Status
    estimated_delivery: Optional[str] = None
    delivered_at: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None
    service: Optional[str] = None
    weight: Optional[str] = None
    pieces: Optional[int] = None
    signed_by: Optional[str] = None
    details: dict[str, str] = {}
    events: list[TrackingEvent] = []
    scraped_at: Optional[str] = None
    ok: bool = True
    error: Optional[str] = None
    from_cache: bool = False


class BatchJobOut(BaseModel):
    """Live state of a bulk-upload job (see batch.BatchJob.to_dict)."""
    id: str
    filename: str
    state: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    notes: list[str] = []
    counts: dict[str, int] = {}
    progress: float = 0.0
    rows: list[dict] = []


def _validate_number(number: str) -> str:
    """Shared tracking-number check for the single and bulk paths."""
    number = number.strip()
    if not _TRACKING_RE.match(number):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tracking_number may only contain letters, digits, and hyphens",
        )
    return number


def _payload(result: TrackingResult) -> dict:
    """TrackingResult -> the JSON-ready ShipmentOut shape the frontend reads."""
    return ShipmentOut(**result.model_dump(mode="json")).model_dump(mode="json")


@contextmanager
def carrier_session(carrier: Carrier) -> Iterator[Callable[[str], dict]]:
    """Yield a ``track(number) -> payload`` bound to ONE browser.

    Opening Chrome is ~10-15s of a ~40s lookup, so a bulk run that launched one
    per row spent most of its time starting browsers. Every number for a carrier
    now goes through a single session. Still one page at a time — this removes
    wasted startup rather than adding concurrency.

    Raises on failure; the caller decides how to record it.
    """
    scraper = SCRAPERS[carrier]
    opener = getattr(scraper, "session", None)
    if opener is None:
        # A stand-in scraper (tests) that only implements scrape(): one-shot.
        yield lambda number: _payload(scraper.scrape(number))
        return
    with opener() as session:
        yield lambda number: _payload(session.track(number))


@contextmanager
def browser_session(carrier: Carrier) -> Iterator[Callable[[str], TrackingResult]]:
    """carrier_session, but yielding raw TrackingResults — the browser
    fallback tracking_service uses behind its fast fetchers."""
    scraper = SCRAPERS[carrier]
    opener = getattr(scraper, "session", None)
    if opener is None:
        yield scraper.scrape
        return
    with opener() as session:
        yield session.track


@contextmanager
def refresh_session(carrier: Carrier) -> Iterator[Callable[[str], dict]]:
    """Per-number lookups that skip the cache but use the fast path and save
    the result — for an explicit single-row refresh."""
    yield lambda number: _payload(
        tracking_service.track_one(carrier, number, browser_session, use_cache=False)[0]
    )


def bulk_track(
    items: list[tuple[Carrier, str]],
    on_result: Callable[[Carrier, str, dict], None],
    cancelled: Callable[[], bool],
) -> None:
    """Batch runner hook: cache hits instantly, then the fast fetchers for
    all rows at once, then the browser for whatever they couldn't answer."""
    tracking_service.track_many(
        items,
        browser_session,
        on_result=lambda c, n, r, _cached: on_result(c, n, _payload(r)),
        cancelled=cancelled,
    )


def _xlsx_response(data: bytes, filename: str) -> Response:
    return Response(
        content=data,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The browser fetches this cross-origin; without this the JS can't
            # read the filename off the response.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


def create_app() -> FastAPI:
    refresher = tracking_service.Refresher(browser_session)
    pool = browser_pool.BrowserPool(
        SCRAPERS,
        size=config.BROWSER_POOL_SIZE,
        carriers=[Carrier(c) for c in config.BROWSER_POOL_CARRIERS if c in Carrier._value2member_map_],
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # The pool is installed only while its workers run: a lookup that
        # found it installed but not started would wait forever.
        pool.start()
        if pool.size:
            tracking_service.POOL = pool
        if config.REFRESH_ENABLED:
            refresher.start()
        yield
        refresher.stop()
        if tracking_service.POOL is pool:
            tracking_service.POOL = None
        pool.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="Multi-Carrier Tracker API",
        version="1.0.0",
        description="Live UPS / FedEx / DHL / Aramex tracking (no storage).",
    )

    # CORS — allowed frontend origins. Defaults include the deployed Vercel app
    # and local dev; extra origins can be appended via CORS_ORIGINS.
    default_origins = [
        "https://tracker-frontend-tawny.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    extra = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
    origins = default_origins + extra

    # A fixed list alone cannot hold: Vercel gives every branch and every
    # deployment its own hostname (tracker-frontend-tawny-git-<branch>-<scope>
    # .vercel.app), and a dev server whose port 3000 was taken lands on 3001 or
    # higher. Both then get "Disallowed CORS origin", which reaches the user as
    # "Cannot reach the tracker API" — the API is fine, the browser simply threw
    # the answer away. The pattern stays scoped to this project's own previews
    # rather than all of vercel.app, and to loopback for local work.
    default_origin_regex = (
        r"^https://tracker-frontend[a-z0-9-]*\.vercel\.app$"
        r"|^http://(localhost|127\.0\.0\.1)(:\d+)?$"
    )
    origin_regex = os.getenv("CORS_ORIGIN_REGEX", default_origin_regex)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=origin_regex,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "fast_path": sorted(c.value for c in tracking_service.FAST),
            "last_refresh": refresher.last_sweep or None,
            "browser_pool": pool.status() if tracking_service.POOL is pool else None,
        }

    # Sync `def` (not async) so FastAPI runs the blocking lookup in a threadpool
    # and the event loop stays responsive. A fresh-enough saved result is
    # returned straight from the database; refresh=true forces a live lookup.
    @app.get("/api/track", response_model=ShipmentOut)
    def track(
        carrier: Carrier,
        tracking_number: str = Query(..., min_length=1, max_length=64),
        refresh: bool = Query(False, description="Skip the cache and ask the carrier now."),
    ) -> ShipmentOut:
        number = _validate_number(tracking_number)
        result, from_cache = tracking_service.track_one(
            carrier, number, browser_session, use_cache=not refresh
        )
        return ShipmentOut(**{**_payload(result), "from_cache": from_cache})

    # -----------------------------------------------------------------------
    # Bulk tracking from an uploaded spreadsheet (company + awb columns).
    #
    # Scraping N rows takes minutes, so the upload only parses and queues; the
    # rows are scraped by a background pool and the client polls GET /api/batch
    # /{job_id} for progress. Jobs live in memory only.
    # -----------------------------------------------------------------------
    @app.post(
        "/api/batch",
        response_model=BatchJobOut,
        status_code=http_status.HTTP_202_ACCEPTED,
    )
    async def create_batch(
        file: UploadFile = File(..., description="Excel/CSV with 'company' and 'awb' columns"),
        concurrency: int = Query(
            batch.DEFAULT_CONCURRENCY,
            ge=1,
            le=8,
            description="Rows scraped in parallel. Each one drives its own browser.",
        ),
    ) -> BatchJobOut:
        # UploadFile is async; read it here and hand plain bytes to the parser.
        data = await file.read()
        try:
            parsed = batch.parse_workbook(file.filename or "upload.xlsx", data)
        except batch.UploadError as exc:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        job = batch.JOBS.create(
            parsed, carrier_session, concurrency=concurrency,
            # With fast fetchers the whole upload is tracked in bulk (cached rows
            # instantly); without them, per-carrier browser sessions as before.
            bulk=bulk_track if tracking_service.FAST else None,
        )
        return BatchJobOut(**job.to_dict())

    @app.get("/api/batch", response_model=list[BatchJobOut])
    def list_batches() -> list[BatchJobOut]:
        """Recent jobs, newest first — without rows, so it stays small."""
        return [BatchJobOut(**j.to_dict(include_rows=False)) for j in batch.JOBS.list()]

    @app.get("/api/batch/template.xlsx")
    def batch_template() -> Response:
        """Blank upload template with the two required columns."""
        return _xlsx_response(batch.build_template_xlsx(), "tracking-template.xlsx")

    @app.get("/api/batch/{job_id}", response_model=BatchJobOut)
    def get_batch(job_id: str) -> BatchJobOut:
        job = batch.JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="No such job — it may have expired when the server restarted.",
            )
        return BatchJobOut(**job.to_dict())

    @app.post("/api/batch/{job_id}/cancel", response_model=BatchJobOut)
    def cancel_batch(job_id: str) -> BatchJobOut:
        """Stop a run. Rows already in flight finish; the rest are skipped."""
        job = batch.JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail="No such job."
            )
        job.cancel()
        return BatchJobOut(**job.to_dict())

    @app.post("/api/batch/{job_id}/rows/{index}/retry", response_model=BatchJobOut)
    def retry_batch_row(job_id: str, index: int) -> BatchJobOut:
        """Re-run one row's lookup, without re-uploading the spreadsheet.

        Returns the job immediately with that row back in "running"; the client
        polls as it would for the original run. A duplicate refreshes the row it
        mirrors, so every copy of that waybill updates together.
        """
        job = batch.JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail="No such job."
            )
        try:
            batch.JOBS.retry(job, index, refresh_session)
        except batch.RowNotRetryable as exc:
            raise HTTPException(
                status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return BatchJobOut(**job.to_dict())

    @app.get("/api/batch/{job_id}/export.xlsx")
    def export_batch(job_id: str) -> Response:
        """Download the job's results (plus full event history) as .xlsx."""
        job = batch.JOBS.get(job_id)
        if job is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail="No such job."
            )
        name = batch.safe_filename(job.filename)
        return _xlsx_response(batch.build_results_xlsx(job), f"{name}-results.xlsx")

    @app.get("/api/shipments")
    def list_shipments(
        status: Optional[Status] = None,
        carrier: Optional[Carrier] = None,
        search: Optional[str] = Query(None, max_length=64),
    ) -> list[dict[str, Any]]:
        """Search every saved shipment (milliseconds — straight from SQLite).
        The background refresher keeps undelivered ones current."""
        storage.ensure_db()
        return storage.list_shipments(
            status=status.value if status else None,
            carrier=carrier.value if carrier else None,
            search=search,
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    # Bind 0.0.0.0 by default so it works on hosts like Render/Railway/Docker
    # (they route to the container's public interface, not localhost). PORT is
    # provided by the platform.
    uvicorn.run(
        "index:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD", "")),
    )
