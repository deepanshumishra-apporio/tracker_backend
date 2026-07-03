"""
Web API for the multi-carrier tracker (live, no-storage).

A user picks a carrier + tracking number; the API scrapes the carrier live and
returns a normalized result. Nothing is persisted — the result is ephemeral.

Run (dev):
    uvicorn index:app --reload --port 8000
    # or: python index.py

(An optional batch scraper that DOES persist to SQLite lives in runner.py +
storage.py; it is independent of this web API.)
"""
from __future__ import annotations

import os
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, status as http_status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

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


def create_app() -> FastAPI:
    app = FastAPI(
        title="Multi-Carrier Tracker API",
        version="1.0.0",
        description="Live UPS / FedEx / DHL / Aramex tracking (no storage).",
    )

    # CORS — allow the Next.js frontend origins (override via CORS_ORIGINS).
    origins = os.getenv(
        "CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in origins if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Sync `def` (not async) so FastAPI runs the blocking scrape in a threadpool
    # and the event loop stays responsive. Nothing is persisted.
    @app.get("/api/track", response_model=ShipmentOut)
    def track(
        carrier: Carrier,
        tracking_number: str = Query(..., min_length=1, max_length=64),
    ) -> ShipmentOut:
        number = tracking_number.strip()
        if not _TRACKING_RE.match(number):
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tracking_number may only contain letters, digits, and hyphens",
            )
        scraper = SCRAPERS[carrier]
        try:
            result = scraper.scrape(number)
        except Exception as exc:  # blocked after retries, timeout, etc.
            result = TrackingResult.failure(number, carrier, str(exc))
        return ShipmentOut(**result.model_dump(mode="json"))

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "index:app",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
        reload=bool(os.getenv("RELOAD", "")),
    )
