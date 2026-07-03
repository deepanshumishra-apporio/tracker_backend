"""
FedEx scraper — FINALIZED against the live site (2026-07).

FedEx is an Akamai-protected SPA; UC Mode clears it. Data loads async (poll).
Stable hooks found on www.fedex.com/fedextrack:
  #statCode                        human status message
  [data-test-id=delivery-date-text] estimated delivery ("Sunday 7/05/2026 ...")
FROM/TO + scan history have no stable ids, so we parse the ordered page text:
  ... FROM <origin> <MILESTONE HEADERS...> <scan location> <M/D/YY H:MM AM> ... TO <dest>
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.base import BaseScraper

# Milestone headers in reached-order; index => how far along the shipment is.
_MILESTONES = [
    ("delivered", Status.DELIVERED),
    ("out for delivery", Status.OUT_FOR_DELIVERY),
    ("on the way", Status.IN_TRANSIT),
    ("at local facility", Status.IN_TRANSIT),
    ("we have your package", Status.IN_TRANSIT),
    ("picked up", Status.IN_TRANSIT),
    ("shipment information sent", Status.PENDING),
    ("label created", Status.PENDING),
]

_DT_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})\s+(\d{1,2}:\d{2}\s*[AP]\.?M\.?)", re.I)
_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})")
# Delivered shipments render "Tuesday, 6/09/2026 at 11:23 am" (note the "at").
_DT_AT_RE = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{2,4})\s+at\s+(\d{1,2}:\d{2}\s*[AP]\.?M\.?)", re.I)
_SIGNED_RE = re.compile(r"Signed for by:\s*(.+)", re.I)


def _status_from_message(msg: str) -> Optional[Status]:
    m = (msg or "").lower()
    if "delivered" in m:
        return Status.DELIVERED
    if "out for delivery" in m:
        return Status.OUT_FOR_DELIVERY
    if "exception" in m or "delay" in m or "held" in m:
        return Status.EXCEPTION
    return None


def _parse_scan_dt(date_s: str, time_s: str) -> Optional[datetime]:
    cleaned = f"{date_s} {time_s}".replace(".", "").upper()
    for fmt in ("%m/%d/%y %I:%M %p", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


# --- Full "Travel history" parsing (revealed by clicking "View more details") --
# The section is a flat list: a weekday date header ("Friday, 6/26/26"), then
# repeating [time, description, optional LOCATION] groups until the next header.
_TH_DATE_RE = re.compile(r"^[A-Za-z]+,\s+(\d{1,2}/\d{1,2}/\d{2,4})$")
_TH_TIME_RE = re.compile(r"^\d{1,2}:\d{2}\s*[AP]M$", re.I)


def _is_location(line: str) -> bool:
    """Travel-history locations are upper-case place names ('NEW DELHI IN')."""
    return (
        len(line) > 2
        and line == line.upper()
        and any(c.isalpha() for c in line)
        and not _TH_TIME_RE.match(line)
    )


def _event_status(desc: str) -> Status:
    d = (desc or "").lower()
    if "delivered" in d:
        return Status.DELIVERED
    if "out for delivery" in d or "on the way with your package" in d:
        return Status.OUT_FOR_DELIVERY
    if "exception" in d or "delay" in d or "held" in d or "clearance delay" in d:
        return Status.EXCEPTION
    for label, st in _MILESTONES:
        if label in d:
            return st
    if "shipment information" in d or "label" in d:
        return Status.PENDING
    return Status.IN_TRANSIT


# "Shipment facts" (also under "View more details"): label + value on one line,
# e.g. "SERVICE FedEx International Priority", "WEIGHT 1.54 lbs / 0.7 kgs".
# Longest labels first so "TOTAL SHIPMENT WEIGHT" isn't caught by "WEIGHT".
_FACT_LABELS = [
    ("TOTAL SHIPMENT WEIGHT", None),          # redundant with WEIGHT -> skip
    ("SPECIAL HANDLING SECTION", "Special handling"),
    ("TOTAL PIECES", "pieces"),
    ("DIMENSIONS", "Dimensions"),
    ("PACKAGING", "Packaging"),
    ("SERVICE", "service"),
    ("WEIGHT", "weight"),
    ("TERMS", "Terms"),
]


def _parse_shipment_facts(lines: list[str]) -> dict:
    """Extract service / weight / pieces / dimensions / packaging from facts."""
    out: dict = {"service": None, "weight": None, "pieces": None, "details": {}}
    # Scope to the "Shipment facts" section: start at its heading, stop before
    # the page footer (so e.g. the footer "Terms of Use" link isn't picked up).
    _STOP = {"useful links", "legal", "our company", "more from fedex",
             "follow fedex", "language", "connect with us"}
    region: list[str] = []
    started = any(ln.strip().lower() == "shipment facts" for ln in lines)
    collecting = not started
    for ln in lines:
        low = ln.strip().lower()
        if low == "shipment facts":
            collecting = True
            continue
        if collecting:
            if low in _STOP:
                break
            region.append(ln)

    for i, line in enumerate(region):
        up = line.upper()
        for label, key in _FACT_LABELS:
            # Require an exact label or "LABEL value" — so the "Services" section
            # header doesn't match the "SERVICE" label (giving value "s").
            if up == label or up.startswith(label + " "):
                value = line[len(label):].strip()
                if not value and i + 1 < len(region):
                    value = region[i + 1].strip()
                if key is None or not value or value.lower().startswith("will be updated"):
                    break
                # First match wins — never overwrite a value already captured.
                if key == "service" and out["service"] is None:
                    out["service"] = value
                elif key == "weight" and out["weight"] is None:
                    out["weight"] = value
                elif key == "pieces" and out["pieces"] is None:
                    m = re.search(r"\d+", value)
                    out["pieces"] = int(m.group()) if m else None
                elif key not in ("service", "weight", "pieces") and key not in out["details"]:
                    out["details"][key] = value
                break
    return out


def _parse_travel_history(lines: list[str]) -> list[TrackingEvent]:
    """Parse the expanded Travel history block into events (returned newest-first)."""
    # Isolate the section between the "Travel history" heading and the footer.
    try:
        start = next(i for i, ln in enumerate(lines) if ln.lower() == "travel history")
    except StopIteration:
        return []
    end = len(lines)
    for marker in ("OUR COMPANY", "Site Map", "MORE FROM FEDEX"):
        for i in range(start + 1, len(lines)):
            if lines[i].upper() == marker.upper():
                end = min(end, i)
                break
    section = lines[start + 1 : end]

    events: list[TrackingEvent] = []
    current_date: Optional[str] = None
    i = 0
    while i < len(section):
        line = section[i]
        dm = _TH_DATE_RE.match(line)
        if dm:
            current_date = dm.group(1)
            i += 1
            continue
        if _TH_TIME_RE.match(line):
            time_s = line
            desc = loc = None
            j = i + 1
            if j < len(section) and not _TH_TIME_RE.match(section[j]) and not _TH_DATE_RE.match(section[j]):
                if _is_location(section[j]):
                    loc = section[j]
                else:
                    desc = section[j]
                j += 1
                if (
                    desc is not None
                    and j < len(section)
                    and _is_location(section[j])
                ):
                    loc = section[j]
                    j += 1
            ts = _parse_scan_dt(current_date, time_s) if current_date else None
            events.append(
                TrackingEvent(
                    timestamp=ts,
                    location=loc,
                    description=desc or "Update",
                    status=_event_status(desc or ""),
                )
            )
            i = j
            continue
        i += 1

    events.reverse()  # page lists oldest-first; UI wants newest-first
    return events


class FedExScraper(BaseScraper):
    carrier = Carrier.FEDEX

    def build_url(self, tracking_number: str) -> str:
        return f"https://www.fedex.com/fedextrack/?trknbr={tracking_number}"

    def api_url(self, tracking_number: str) -> Optional[str]:
        return None  # POST-based internal API; DOM parsing is reliable

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        # Wait for async tracking data. Delivered shipments use a different layout
        # (no #statCode / no "ESTIMATED DELIVERY"), so accept several signals.
        for _ in range(18):
            sb.sleep(2)
            if sb.execute_script(
                "const u=document.body.innerText.toUpperCase();"
                "return !!document.querySelector('#statCode') "
                "|| u.includes('ESTIMATED DELIVERY') || u.includes('DELIVERED') "
                "|| u.includes('SIGNED FOR BY')"
            ):
                break

        status_msg = sb.execute_script(
            "const e=document.querySelector('#statCode');return e?e.textContent.trim():null;")
        est_text = sb.execute_script(
            "const e=document.querySelector('[data-test-id=delivery-date-text]');"
            "return e?e.textContent.trim():null;")

        # Read the summary view FIRST — FROM/TO and the latest scan live here and
        # are replaced once we expand the full history.
        try:
            summary = sb.get_text("body")
        except Exception as e:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          f"could not read page: {e}")
        summary_lines = [ln.strip() for ln in summary.split("\n") if ln.strip()]
        summary_upper = [ln.upper() for ln in summary_lines]

        # Origin / destination: the line following FROM / (last) TO. Guard against
        # a status/milestone word leaking in (e.g. "Label created") when the page
        # has no real location there.
        def _place(value: str | None) -> str | None:
            if not value:
                return None
            low = value.lower()
            if any(label in low for label, _ in _MILESTONES) or low in (
                "label created", "delivered", "pending",
            ):
                return None
            return value

        origin = destination = None
        for i, u in enumerate(summary_upper):
            if u == "FROM" and i + 1 < len(summary_lines):
                origin = _place(summary_lines[i + 1])
            if u == "TO" and i + 1 < len(summary_lines):
                destination = _place(summary_lines[i + 1])  # last TO wins

        # Signed-for-by (delivered shipments) and the delivered date/time.
        signed_by = None
        delivered_dt = None
        for ln in summary_lines:
            sm = _SIGNED_RE.search(ln)
            if sm and not signed_by:
                signed_by = sm.group(1).strip()
            am = _DT_AT_RE.search(ln)
            if am and delivered_dt is None:
                delivered_dt = _parse_scan_dt(am.group(1), am.group(2))

        # Valid page? We need at least one real tracking signal; otherwise the
        # number is invalid or FedEx changed the page.
        has_signal = bool(
            status_msg or origin or destination or signed_by
            or any("ESTIMATED DELIVERY" in u or u == "DELIVERED" for u in summary_upper)
        )
        if not has_signal:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          "no tracking data (invalid number or page changed)")

        # Now expand the full scan history ("View more details" -> "Travel history").
        try:
            sb.execute_script(
                "for(const el of document.querySelectorAll('button,a,span')){"
                "if(((el.textContent||'').trim().toLowerCase()).startsWith('view more details'))"
                "{el.click();return;}}")
            sb.sleep(3)
        except Exception:
            pass

        try:
            hist_body = sb.get_text("body")
        except Exception:
            hist_body = summary
        hist_lines = [ln.strip() for ln in hist_body.split("\n") if ln.strip()]

        # Most-recent scan (from the summary view): first date+time; location above it.
        scan_dt = scan_loc = None
        dt_idx = None
        for i, ln in enumerate(summary_lines):
            m = _DT_RE.search(ln)
            if m:
                scan_dt = _parse_scan_dt(m.group(1), m.group(2))
                dt_idx = i
                scan_loc = summary_lines[i - 1] if i > 0 else None
                break

        # Status: prefer explicit message; delivered shipments have signed_by /
        # a delivered date; else nearest milestone above the scan.
        status = _status_from_message(status_msg or "")
        if status is None and (signed_by or delivered_dt or "DELIVERED" in summary_upper):
            status = Status.DELIVERED
        if status is None:
            search_zone = summary_upper[:dt_idx] if dt_idx is not None else summary_upper
            best_rank = None
            for u in search_zone:
                for rank, (label, st) in enumerate(_MILESTONES):
                    if label.upper() in u and (best_rank is None or rank < best_rank):
                        best_rank, status = rank, st
            if status is None:
                status = Status.UNKNOWN

        # Shipment facts (service / weight / dimensions / pieces / packaging).
        facts = _parse_shipment_facts(hist_lines)

        # Prefer the full travel history; fall back to the single latest scan.
        events = _parse_travel_history(hist_lines)
        if not events and (status_msg or scan_loc):
            events.append(TrackingEvent(
                timestamp=scan_dt, location=scan_loc,
                description=status_msg or "In transit", status=status,
            ))
        # Newest event's time is the freshest scan (used for delivered_at).
        latest_dt = events[0].timestamp if events else scan_dt

        est_dt = None
        if est_text:
            md = _DATE_RE.search(est_text)
            if md:
                try:
                    est_dt = datetime.strptime(md.group(1), "%m/%d/%Y")
                except ValueError:
                    est_dt = None

        return TrackingResult(
            tracking_number=tracking_number, carrier=self.carrier,
            status=status, origin=origin, destination=destination,
            estimated_delivery=est_dt,
            signed_by=signed_by,
            service=facts["service"],
            weight=facts["weight"],
            pieces=facts["pieces"],
            details=facts["details"],
            # Prefer the explicit delivered timestamp, else the newest scan.
            delivered_at=(delivered_dt or latest_dt) if status == Status.DELIVERED else None,
            events=events,
        )
