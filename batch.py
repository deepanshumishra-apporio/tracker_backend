"""
Bulk tracking from an uploaded spreadsheet.

The user uploads an Excel/CSV file with two columns — ``company`` and ``awb`` —
and we track every row. Because a single scrape drives a real browser and can
take tens of seconds, the work cannot happen inside the upload request: the
upload parses + validates the file and returns a job id, a background worker
pool scrapes the rows, and the frontend polls the job for live progress.

Everything here is in-memory (matching the live, no-storage design of the
/api/track endpoint). Restarting the API clears jobs.

Public surface:
    parse_workbook(filename, data) -> ParsedFile   # column detection + validation
    JOBS.create(...) / JOBS.get(...) / JOBS.list() # job registry
    build_results_xlsx(job) -> bytes               # results download
    build_template_xlsx() -> bytes                 # blank input template
"""
from __future__ import annotations

import csv
import io
import re
import threading
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import config
from models import Carrier, Status, TrackingResult
from scrapers.base import humanize_error

# ---------------------------------------------------------------------------
# Limits — a spreadsheet is user input, so cap what we will accept.
# ---------------------------------------------------------------------------
MAX_UPLOAD_BYTES = 5 * 1024 * 1024   # 5 MB
MAX_ROWS = 500                       # rows tracked per upload
MAX_JOBS = 50                        # jobs retained in memory (oldest evicted)

# How many rows are scraped at once. Each worker drives its own Chrome, so this
# multiplies memory; config caps it and BATCH_CONCURRENCY tunes it per deploy.
DEFAULT_CONCURRENCY = config.BATCH_CONCURRENCY

_TRACKING_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


class UploadError(Exception):
    """The file itself is unusable (bad format, no header, no rows)."""


# ---------------------------------------------------------------------------
# Column detection
# ---------------------------------------------------------------------------
# Header spellings we accept for each logical column, so the user does not have
# to match one exact string. Compared after lowercasing and stripping every
# non-alphanumeric character ("AWB No." -> "awbno").
COMPANY_HEADERS = {
    "company", "companyname", "carrier", "carriername", "courier",
    "couriername", "shippingcompany", "vendor", "service", "servicename",
}
AWB_HEADERS = {
    "awb", "awbno", "awbnumber", "awbid", "awbcode", "trackingnumber",
    "trackingno", "trackingid", "tracking", "waybill", "waybillnumber",
    "consignment", "consignmentno", "consignmentnumber", "shipmentnumber",
    "number",
}

# Company cell -> Carrier. Keyed by the same normalized form as headers, so
# "DHL Express", "dhl-express" and "DHL  Express" all land on DHL.
CARRIER_ALIASES: dict[str, Carrier] = {
    "ups": Carrier.UPS,
    "upsexpress": Carrier.UPS,
    "unitedparcelservice": Carrier.UPS,
    "fedex": Carrier.FEDEX,
    "fedexexpress": Carrier.FEDEX,
    "federalexpress": Carrier.FEDEX,
    "fdx": Carrier.FEDEX,
    "dhl": Carrier.DHL,
    "dhlexpress": Carrier.DHL,
    "dhlecommerce": Carrier.DHL,
    "dhlglobalforwarding": Carrier.DHL,
    "aramex": Carrier.ARAMEX,
    "aramexexpress": Carrier.ARAMEX,
}


def _norm(value: Any) -> str:
    """Lowercase and strip everything that isn't a letter or digit."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def resolve_carrier(company: str) -> Optional[Carrier]:
    """Map a spreadsheet company cell to a Carrier, or None if unrecognized."""
    key = _norm(company)
    if not key:
        return None
    if key in CARRIER_ALIASES:
        return CARRIER_ALIASES[key]
    # Fall back to a containment match so "DHL Express Worldwide" or
    # "Ship via FedEx" still resolve. Longest alias first: "fedexexpress"
    # should win over a bare "fedex" substring hit.
    for alias in sorted(CARRIER_ALIASES, key=len, reverse=True):
        if alias in key:
            return CARRIER_ALIASES[alias]
    return None


def _cell_text(value: Any) -> str:
    """Spreadsheet cell -> clean string.

    Excel happily stores a numeric AWB as a float, so 1234567890 comes back as
    "1234567890.0" via str(). Integral floats are rendered without the decimal
    tail; datetimes are ISO-formatted rather than repr'd.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
@dataclass
class ParsedRow:
    """One data row from the upload, already validated."""
    row_number: int          # 1-based line in the source file (header included)
    company: str             # the raw cell text, echoed back to the user
    awb: str
    carrier: Optional[Carrier] = None
    error: Optional[str] = None
    # A repeat of an earlier (carrier, awb). Tracked separately from `error`
    # because a duplicate is not something the user needs to fix — real files
    # legitimately repeat a waybill across order lines.
    duplicate: bool = False

    @property
    def valid(self) -> bool:
        return self.error is None


@dataclass
class ParsedFile:
    filename: str
    company_header: str
    awb_header: str
    rows: list[ParsedRow]
    skipped_blank: int = 0
    truncated: bool = False   # file had more than MAX_ROWS data rows

    @property
    def valid_rows(self) -> list[ParsedRow]:
        return [r for r in self.rows if r.valid]


def _read_table(filename: str, data: bytes) -> list[list[Any]]:
    """Read an .xlsx/.xlsm or .csv upload into a list of raw rows."""
    name = (filename or "").lower()
    if name.endswith((".csv", ".txt", ".tsv")):
        return _read_csv(data, delimiter="\t" if name.endswith(".tsv") else None)
    if name.endswith(".xls"):
        raise UploadError(
            "Legacy .xls files aren't supported — save the file as .xlsx or .csv "
            "and upload it again."
        )
    if not name.endswith((".xlsx", ".xlsm")):
        raise UploadError("Upload an Excel (.xlsx) or CSV file.")
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise UploadError(f"Couldn't read that Excel file ({exc}).") from exc
    try:
        ws = wb.worksheets[0]
        return [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _read_csv(data: bytes, delimiter: Optional[str] = None) -> list[list[Any]]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:2048], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    return [list(row) for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _find_header(table: list[list[Any]]) -> tuple[int, int, int, str, str]:
    """Locate the header row and the company/awb column indexes.

    Scans the first few rows so a file with a title line or blank padding above
    the real header still works. Returns
    (header_row_index, company_col, awb_col, company_header, awb_header).
    """
    for idx, row in enumerate(table[:10]):
        cells = [_cell_text(c) for c in row]
        normalized = [_norm(c) for c in cells]
        company_col = next(
            (i for i, n in enumerate(normalized) if n in COMPANY_HEADERS), None
        )
        awb_col = next((i for i, n in enumerate(normalized) if n in AWB_HEADERS), None)
        if company_col is not None and awb_col is not None:
            return idx, company_col, awb_col, cells[company_col], cells[awb_col]
    raise UploadError(
        "Couldn't find the required columns. The sheet needs a header row with "
        "a 'company' column and an 'awb' column."
    )


def parse_workbook(filename: str, data: bytes) -> ParsedFile:
    """Validate an uploaded spreadsheet and return its rows.

    Raises UploadError when the file as a whole is unusable. Individual bad rows
    are kept with an ``error`` set so the UI can show exactly what to fix.
    """
    if not data:
        raise UploadError("The uploaded file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(
            f"File is too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."
        )

    table = _read_table(filename, data)
    if not table:
        raise UploadError("The sheet has no rows.")

    header_idx, company_col, awb_col, company_header, awb_header = _find_header(table)

    rows: list[ParsedRow] = []
    skipped_blank = 0
    truncated = False
    seen: set[tuple[str, str]] = set()

    for offset, raw in enumerate(table[header_idx + 1:], start=header_idx + 2):
        company = _cell_text(raw[company_col] if company_col < len(raw) else "")
        awb = _cell_text(raw[awb_col] if awb_col < len(raw) else "")
        if not company and not awb:
            skipped_blank += 1
            continue
        if len(rows) >= MAX_ROWS:
            truncated = True
            break

        row = ParsedRow(row_number=offset, company=company, awb=awb)
        carrier = resolve_carrier(company)
        if not awb:
            row.error = "Missing AWB number."
        elif not company:
            row.error = "Missing company."
        elif carrier is None:
            row.error = (
                f"Unknown company '{company}'. Supported: "
                f"{', '.join(c.value.upper() for c in Carrier)}."
            )
        elif not _TRACKING_RE.match(awb):
            row.error = "AWB may only contain letters, digits, and hyphens."
        elif len(awb) > 64:
            row.error = "AWB is too long (max 64 characters)."
        else:
            key = (carrier.value, awb.upper())
            if key in seen:
                row.error = "Duplicate of an earlier row — tracked once."
                row.duplicate = True
                row.carrier = carrier
            else:
                seen.add(key)
                row.carrier = carrier
        rows.append(row)

    if not rows:
        raise UploadError("No data rows found under the header.")

    return ParsedFile(
        filename=filename or "upload",
        company_header=company_header,
        awb_header=awb_header,
        rows=rows,
        skipped_blank=skipped_blank,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
@dataclass
class JobRow:
    """One tracked row: the input, its live state, and the scrape result."""
    index: int                       # 0-based position within the job
    row_number: int                  # line in the source file
    company: str
    awb: str
    carrier: Optional[str] = None
    # queued -> running -> done | failed | skipped (skipped = invalid input)
    state: str = "queued"
    error: Optional[str] = None
    result: Optional[dict[str, Any]] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "row_number": self.row_number,
            "company": self.company,
            "awb": self.awb,
            "carrier": self.carrier,
            "state": self.state,
            "error": self.error,
            "result": self.result,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": (self.result or {}).get("status"),
        }


@dataclass
class BatchJob:
    id: str
    filename: str
    rows: list[JobRow]
    created_at: str
    # queued -> running -> completed | cancelled
    state: str = "queued"
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- progress ----------------------------------------------------------
    def counts(self) -> dict[str, int]:
        c = {"total": len(self.rows), "queued": 0, "running": 0,
             "done": 0, "failed": 0, "skipped": 0, "duplicate": 0}
        for row in self.rows:
            c[row.state] = c.get(row.state, 0) + 1
        c["finished"] = c["done"] + c["failed"] + c["skipped"] + c["duplicate"]
        c["delivered"] = sum(
            1 for r in self.rows if (r.result or {}).get("status") == Status.DELIVERED.value
        )
        c["in_transit"] = sum(
            1 for r in self.rows
            if (r.result or {}).get("status") in {
                Status.IN_TRANSIT.value, Status.OUT_FOR_DELIVERY.value, Status.PENDING.value
            }
        )
        return c

    def to_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        counts = self.counts()
        data: dict[str, Any] = {
            "id": self.id,
            "filename": self.filename,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "notes": list(self.notes),
            "counts": counts,
            "progress": (counts["finished"] / counts["total"]) if counts["total"] else 1.0,
        }
        if include_rows:
            data["rows"] = [r.to_dict() for r in self.rows]
        return data

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self.state in {"queued", "running"}:
                for row in self.rows:
                    if row.state == "queued":
                        row.state = "skipped"
                        row.error = "Cancelled before this row ran."
                self.state = "cancelled"
                self.finished_at = _now()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobRegistry:
    """In-memory job store + the worker pool that drains them."""

    def __init__(self, *, max_jobs: int = MAX_JOBS) -> None:
        self._jobs: dict[str, BatchJob] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    # -- registry ----------------------------------------------------------
    def get(self, job_id: str) -> Optional[BatchJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[BatchJob]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def _register(self, job: BatchJob) -> None:
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            # Evict oldest finished jobs so a long-lived server can't grow
            # without bound. Running jobs are never evicted.
            while len(self._order) > self._max_jobs:
                for i, old_id in enumerate(self._order):
                    old = self._jobs.get(old_id)
                    if old is None or old.state in {"completed", "cancelled"}:
                        self._order.pop(i)
                        self._jobs.pop(old_id, None)
                        break
                else:
                    break  # everything still running — keep them all

    # -- creation + execution ---------------------------------------------
    def create(
        self,
        parsed: ParsedFile,
        session_factory: Callable[[Carrier], AbstractContextManager],
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> BatchJob:
        rows = [
            JobRow(
                index=i,
                row_number=p.row_number,
                company=p.company,
                awb=p.awb,
                carrier=p.carrier.value if p.carrier else None,
                state=("duplicate" if p.duplicate else "skipped" if not p.valid else "queued"),
                error=p.error,
            )
            for i, p in enumerate(parsed.rows)
        ]
        notes: list[str] = []
        if parsed.truncated:
            notes.append(f"Only the first {MAX_ROWS} rows were taken from this file.")
        if parsed.skipped_blank:
            notes.append(f"{parsed.skipped_blank} blank row(s) ignored.")
        invalid = sum(1 for r in rows if r.state == "skipped")
        if invalid:
            notes.append(f"{invalid} row(s) couldn't be tracked — see the errors below.")
        repeats = sum(1 for r in rows if r.state == "duplicate")
        if repeats:
            notes.append(
                f"{repeats} row(s) repeat an AWB listed earlier — each shipment is "
                "tracked once."
            )

        job = BatchJob(
            id=uuid.uuid4().hex[:12],
            filename=parsed.filename,
            rows=rows,
            created_at=_now(),
            notes=notes,
        )
        self._register(job)
        self._start(job, session_factory, concurrency)
        return job

    def _start(
        self,
        job: BatchJob,
        session_factory: Callable[[Carrier], AbstractContextManager],
        concurrency: int,
    ) -> None:
        pending = [r for r in job.rows if r.state == "queued"]
        if not pending:
            job.state = "completed"
            job.started_at = job.finished_at = _now()
            return

        # Group by carrier so each group can share ONE browser. Opening Chrome
        # costs ~10-15s of a ~40s lookup, and the old row-at-a-time loop paid it
        # for every single row. Grouping also keeps a carrier's numbers on one
        # warmed-up session instead of hopping between sites.
        groups: dict[str, list[JobRow]] = defaultdict(list)
        for row in pending:
            groups[row.carrier or ""].append(row)

        def worker() -> None:
            job.state = "running"
            job.started_at = _now()
            # One worker per carrier group at most; concurrency still caps it.
            workers = max(1, min(concurrency, len(groups)))
            try:
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix=f"batch-{job.id}"
                ) as pool:
                    for _ in pool.map(
                        lambda item: _run_group(job, item[0], item[1], session_factory),
                        list(groups.items()),
                    ):
                        pass
            finally:
                with job._lock:
                    if job.state != "cancelled":
                        job.state = "completed"
                        job.finished_at = _now()

        threading.Thread(
            target=worker, name=f"batch-job-{job.id}", daemon=True
        ).start()


def _run_group(
    job: BatchJob,
    carrier: str,
    rows: list[JobRow],
    session_factory: Callable[[Carrier], AbstractContextManager],
) -> None:
    """Run every row for one carrier through a single shared browser."""
    if job.cancelled:
        _abandon(rows, "Cancelled before this row ran.", state="skipped")
        return
    try:
        with session_factory(Carrier(carrier)) as track:
            for row in rows:
                _run_row(job, row, track)
    except Exception as exc:
        # The session itself could not be opened or died unrecoverably; the
        # rows it never reached would otherwise hang in "queued" forever.
        _abandon(rows, humanize_error(exc), state="failed")


def _abandon(rows: list[JobRow], message: str, *, state: str) -> None:
    for row in rows:
        if row.state in ("queued", "running"):
            row.state = state
            row.error = message
            row.finished_at = _now()


def _run_row(
    job: BatchJob,
    row: JobRow,
    track: Callable[[str], dict[str, Any]],
) -> None:
    """Scrape one row on an open session, recording state. Never raises."""
    if job.cancelled:
        if row.state == "queued":
            row.state = "skipped"
            row.error = "Cancelled before this row ran."
        return

    row.state = "running"
    row.started_at = _now()
    try:
        result = track(row.awb)
        row.result = result
        if result.get("ok"):
            row.state = "done"
            row.error = None
        else:
            row.state = "failed"
            row.error = result.get("error") or "The carrier returned no data."
    except Exception as exc:  # blocked, timeout, browser crash…
        row.state = "failed"
        # Selenium's raw message is a multi-kilobyte hex stack trace; the table
        # needs one readable line.
        row.error = humanize_error(exc)
        row.result = TrackingResult.failure(
            row.awb, Carrier(row.carrier), row.error
        ).model_dump(mode="json")
    finally:
        row.finished_at = _now()


JOBS = JobRegistry()


# ---------------------------------------------------------------------------
# Spreadsheet output
# ---------------------------------------------------------------------------
_HEADER_FILL = PatternFill("solid", fgColor="1E293B")
_HEADER_FONT = Font(color="FFFFFF", bold=True)

_RESULT_COLUMNS: list[tuple[str, Callable[[JobRow], Any]]] = [
    ("Row", lambda r: r.row_number),
    ("Company", lambda r: r.company),
    ("AWB", lambda r: r.awb),
    ("Carrier", lambda r: (r.carrier or "").upper()),
    # Only a successfully tracked row has a real carrier status. A failed one
    # carries the placeholder "unknown", which would read as real data here —
    # the State + Error columns say what actually happened.
    ("Status", lambda r: _status_label(
        (r.result or {}).get("status") if r.state == "done" else None
    )),
    ("State", lambda r: r.state),
    ("Origin", lambda r: (r.result or {}).get("origin")),
    ("Destination", lambda r: (r.result or {}).get("destination")),
    ("Service", lambda r: (r.result or {}).get("service")),
    ("Weight", lambda r: (r.result or {}).get("weight")),
    ("Pieces", lambda r: (r.result or {}).get("pieces")),
    ("Estimated delivery", lambda r: (r.result or {}).get("estimated_delivery")),
    ("Delivered at", lambda r: (r.result or {}).get("delivered_at")),
    ("Signed by", lambda r: (r.result or {}).get("signed_by")),
    ("Last update", lambda r: _latest_event(r, "timestamp")),
    ("Last activity", lambda r: _latest_event(r, "description")),
    ("Last location", lambda r: _latest_event(r, "location")),
    ("Events", lambda r: len((r.result or {}).get("events") or [])),
    ("Error", lambda r: r.error),
]

_STATUS_LABELS = {
    "pending": "Pending",
    "in_transit": "In transit",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "exception": "Exception",
    "unknown": "Unknown",
}


def _status_label(status: Optional[str]) -> str:
    return _STATUS_LABELS.get(status or "", "—")


def _latest_event(row: JobRow, key: str) -> Any:
    events: Iterable[dict[str, Any]] = (row.result or {}).get("events") or []
    first = next(iter(events), None)
    return (first or {}).get(key)


def _autofit(ws, widths: dict[int, int]) -> None:
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = min(max(width + 2, 10), 48)


def build_results_xlsx(job: BatchJob) -> bytes:
    """Render a finished (or in-flight) job as a downloadable .xlsx."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Tracking results"

    headers = [name for name, _ in _RESULT_COLUMNS]
    ws.append(headers)
    widths = {i: len(h) for i, h in enumerate(headers, start=1)}
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(vertical="center")

    for row in job.rows:
        values = [getter(row) for _, getter in _RESULT_COLUMNS]
        ws.append(values)
        for i, value in enumerate(values, start=1):
            widths[i] = max(widths.get(i, 0), len(_cell_text(value)))

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    _autofit(ws, widths)

    # Second sheet: the full event history, one line per scan.
    hist = wb.create_sheet("Event history")
    hist_headers = ["AWB", "Carrier", "Timestamp", "Status", "Location", "Description"]
    hist.append(hist_headers)
    for cell in hist[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    hist_widths = {i: len(h) for i, h in enumerate(hist_headers, start=1)}
    for row in job.rows:
        for event in (row.result or {}).get("events") or []:
            values = [
                row.awb,
                (row.carrier or "").upper(),
                event.get("timestamp"),
                _status_label(event.get("status")),
                event.get("location"),
                event.get("description"),
            ]
            hist.append(values)
            for i, value in enumerate(values, start=1):
                hist_widths[i] = max(hist_widths.get(i, 0), len(_cell_text(value)))
    hist.freeze_panes = "A2"
    _autofit(hist, hist_widths)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_template_xlsx() -> bytes:
    """A ready-to-fill upload template: the two required columns + examples."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Shipments"
    ws.append(["company", "awb"])
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
    examples = [
        ("UPS", "1Z999AA10123456784"),
        ("FedEx", "987654321098"),
        ("DHL", "1234567890"),
        ("Aramex", "4567891234"),
    ]
    for company, awb in examples:
        ws.append([company, awb])
        # Keep long numeric AWBs as text so Excel doesn't reformat them.
        ws.cell(row=ws.max_row, column=2).number_format = "@"
    _autofit(ws, {1: 18, 2: 24})

    notes = wb.create_sheet("How to use")
    for line in [
        "Fill in the 'Shipments' sheet and upload it in the Bulk tracking tab.",
        "",
        "company  — UPS, FedEx, DHL or Aramex (case-insensitive).",
        "awb      — the tracking / air waybill number for that shipment.",
        "",
        f"Up to {MAX_ROWS} rows per upload. Delete the example rows before uploading.",
        "Extra columns are ignored, so you can keep your own reference data.",
    ]:
        notes.append([line])
    notes.column_dimensions["A"].width = 82

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def safe_filename(name: str, *, fallback: str = "tracking-results") -> str:
    """Strip a user-supplied filename down to something safe for a header."""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").rsplit(".", 1)[0]).strip("-")
    return stem[:60] or fallback


__all__ = [
    "BatchJob", "JobRow", "JobRegistry", "JOBS", "ParsedFile", "ParsedRow",
    "UploadError", "MAX_ROWS", "MAX_UPLOAD_BYTES", "DEFAULT_CONCURRENCY",
    "parse_workbook", "resolve_carrier", "build_results_xlsx",
    "build_template_xlsx", "safe_filename",
]
