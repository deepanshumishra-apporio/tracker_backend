"""
DHL scraper — FINALIZED against the live site (2026-07), DHL Express.

Verified stable hooks on www.dhl.com tracking:
  [class*="c-tracking-result--status-shipment-"]  status encoded in class suffix
  .c-tracking-result--status-copy-message         "Delivered, Tracking Code: ..."
  .c-tracking-result--status-copy-date            "Last Update: <date> at <time> (UTC..), <loc>"
  .c-tracking-result--code                         "Tracking Code: 1790531772"
Origin/Destination are plain "Origin: ..." / "Destination: ..." text lines.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.base import BaseScraper

# DHL encodes status in a class suffix, e.g. ...status-shipment-delivered
_CLASS_STATUS = {
    "delivered": Status.DELIVERED,
    "transit": Status.IN_TRANSIT,
    "with-courier": Status.OUT_FOR_DELIVERY,
    "out-for-delivery": Status.OUT_FOR_DELIVERY,
    "pending": Status.PENDING,
    "on-hold": Status.EXCEPTION,
    "failure": Status.EXCEPTION,
    "exception": Status.EXCEPTION,
}
_WORD_STATUS = {
    "delivered": Status.DELIVERED,
    "in transit": Status.IN_TRANSIT,
    "transit": Status.IN_TRANSIT,
    "out for delivery": Status.OUT_FOR_DELIVERY,
    "with delivery courier": Status.OUT_FOR_DELIVERY,
    "processed": Status.IN_TRANSIT,
    "on hold": Status.EXCEPTION,
}

_LASTUPDATE_JS = (
    "const e=document.querySelector('.c-tracking-result--status-copy-date');"
    "return e?e.textContent.replace(/\\s+/g,' ').trim():null;")
_MSG_JS = (
    "const e=document.querySelector('.c-tracking-result--status-copy-message');"
    "return e?e.textContent.replace(/\\s+/g,' ').trim():null;")
_CLASS_JS = (
    "const e=document.querySelector('[class*=\"c-tracking-result--status-shipment-\"]');"
    "return e?e.className:null;")
_DETAILS_JS = (
    "const e=document.querySelector('.c-tracking-result--shipment-details');"
    "return e?e.innerText:null;")


# Full shipment timeline. Each ".c-track-trace-utapi--event-card" holds a title
# (description) and meta ("10:25 AM (UTC +01:00) | CABINTEELY - IRELAND"); date
# group headers (".c-track-trace-utapi--date") carry "June 30, 2026".
_TIMELINE_JS = r"""
const root = document.querySelector('.c-shipment-timeline') || document.body;
const out = [];
let curDate = null;
root.querySelectorAll('*').forEach(e => {
  const cls = '' + (e.className || '');
  if (cls.includes('c-track-trace-utapi--date')) {
    curDate = e.innerText.replace(/\s+/g, ' ').trim();
  }
  if (cls.includes('c-track-trace-utapi--event-card')) {
    const title = e.querySelector('[class*="event-title"]');
    const meta = e.querySelector('[class*="event-meta"]');
    out.push({
      date: curDate,
      title: title ? title.innerText.replace(/\s+/g, ' ').trim() : null,
      meta: meta ? meta.innerText.replace(/\s+/g, ' ').trim() : null,
    });
  }
});
return JSON.stringify(out);
"""

_TIME_RE = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)", re.I)


def _status_from_title(title):
    t = (title or "").lower()
    if "delivered" in t:
        return Status.DELIVERED
    if "out with courier" in t or "out for delivery" in t or "with delivery courier" in t:
        return Status.OUT_FOR_DELIVERY
    if any(k in t for k in ("delivery attempted", "no response", "on hold",
                            "held", "failed", "exception", "returned", "refused")):
        return Status.EXCEPTION
    return Status.IN_TRANSIT


def _parse_timeline(cards):
    """Turn the JS-extracted event cards into events (already newest-first)."""
    events = []
    for c in cards:
        title = (c.get("title") or "").strip()
        meta = (c.get("meta") or "").strip()
        time_part, loc = meta, None
        if "|" in meta:
            time_part, loc = (p.strip() for p in meta.split("|", 1))
        dt = None
        tm = _TIME_RE.search(time_part or "")
        if c.get("date") and tm:
            try:
                dt = datetime.strptime(f"{c['date']} {tm.group(1)}", "%B %d, %Y %I:%M %p")
            except ValueError:
                dt = None
        events.append(TrackingEvent(
            timestamp=dt,
            location=loc or None,
            description=title or "Update",
            status=_status_from_title(title),
        ))
    return events


def _parse_shipment_details(text):
    """
    Parse DHL's 'shipment details' block into structured attributes.

    The block is a flat list of label / value lines, e.g.:
        Service
        EXPRESS WORLDWIDE
        1 Piece ID
        JD014600012674098596
        Waybill Number
        1790531772
    Note: weight and proof-of-delivery are gated behind DHL's identity check on
    public tracking, so `weight` is usually None here.
    """
    service = weight = None
    pieces = None
    details: dict[str, str] = {}
    if not text:
        return service, weight, pieces, details

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        low = line.lower()
        if "protect your privacy" in low:
            continue
        if low == "service" and nxt:
            service = nxt
        elif low.startswith("weight") and nxt:
            weight = nxt if low == "weight" else line.split(":", 1)[-1].strip() or nxt
        elif low == "waybill number" and nxt:
            details["Waybill Number"] = nxt
        else:
            m = re.match(r"(\d+)\s+Piece ID", line, re.I)
            if m:
                pieces = int(m.group(1))
                if nxt:
                    details["Piece ID"] = nxt
    return service, weight, pieces, details


def _status_from_class(cls: Optional[str]) -> Optional[Status]:
    if not cls:
        return None
    m = re.search(r"c-tracking-result--status-shipment-([a-z\-]+)", cls)
    if not m:
        return None
    return _CLASS_STATUS.get(m.group(1).strip("-"))


def _status_from_message(msg: Optional[str]) -> Status:
    word = (msg or "").split(",")[0].strip().lower()
    for key, st in _WORD_STATUS.items():
        if key in word:
            return st
    return Status.UNKNOWN


def _parse_last_update(text: Optional[str]):
    """'Last Update: Tuesday, June 30, 2026 at 10:25 AM (UTC +01:00), CABINTEELY - IRELAND'"""
    if not text:
        return None, None
    dt = None
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})\s+at\s+(\d{1,2}:\d{2}\s*[AP]M)", text)
    if m:
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %d, %Y %I:%M %p")
        except ValueError:
            dt = None
    # Location: the part after the "(UTC ...)," segment.
    loc = None
    lm = re.search(r"\(UTC[^)]*\),\s*(.+)$", text)
    if lm:
        loc = lm.group(1).strip()
    return dt, loc


class DHLScraper(BaseScraper):
    carrier = Carrier.DHL

    def build_url(self, tracking_number: str) -> str:
        return ("https://www.dhl.com/us-en/home/tracking/tracking-express.html"
                f"?submit=1&tracking-id={tracking_number}")

    def api_url(self, tracking_number: str) -> Optional[str]:
        return None

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        # Wait for async result to render.
        for _ in range(18):
            sb.sleep(2)
            if sb.execute_script(
                "return !!document.querySelector('.c-tracking-result--status-copy-message')"):
                break

        msg = sb.execute_script(_MSG_JS)
        cls = sb.execute_script(_CLASS_JS)
        last_update = sb.execute_script(_LASTUPDATE_JS)

        if not msg and not last_update:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          "no tracking result (invalid number or page changed)")

        status = _status_from_class(cls) or _status_from_message(msg)
        when, loc = _parse_last_update(last_update)

        # Origin / Destination from plain text lines.
        origin = destination = None
        body = sb.get_text("body")
        for ln in body.split("\n"):
            s = ln.strip()
            if s.lower().startswith("origin:"):
                origin = s.split(":", 1)[1].strip()
            elif s.lower().startswith("destination:"):
                destination = s.split(":", 1)[1].strip()

        # Full shipment timeline (18+ checkpoints); fall back to the single
        # latest-update line if the timeline isn't present.
        events: list[TrackingEvent] = []
        try:
            import json
            events = _parse_timeline(json.loads(sb.execute_script(_TIMELINE_JS) or "[]"))
        except Exception:
            events = []
        if not events and (msg or loc):
            events.append(TrackingEvent(
                timestamp=when, location=loc,
                description=(msg or "").split(",")[0].strip() or "Update",
                status=status,
            ))

        # Newest event refines status / delivered time when available.
        if events:
            status = events[0].status or status
            when = events[0].timestamp or when

        # Shipment attributes (service, pieces, waybill, ... — weight is gated).
        service, weight, pieces, details = _parse_shipment_details(
            sb.execute_script(_DETAILS_JS))

        return TrackingResult(
            tracking_number=tracking_number, carrier=self.carrier,
            status=status, origin=origin, destination=destination,
            service=service, weight=weight, pieces=pieces, details=details,
            delivered_at=when if status == Status.DELIVERED else None,
            events=events,
        )
