"""
UPS scraper — FINALIZED against the live site (2026-07).

UPS is an Angular SPA that loads tracking data async (poll after page load).
Verified structure (www.ups.com/track):
  #stApp_nameKey                current milestone headline ("On the Way")
  .estimatedDiv                 label "Estimated delivery"; value = next sibling
  text "Last Location: ..."     most recent scan (location + date + time)
  milestone labels + state      Label Created / We Have Your Package / On the Way
                                / Out for Delivery / Delivery, each completed|active|inactive
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Optional

from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.base import BaseScraper

_STATUS_MAP = {
    "delivered": Status.DELIVERED,
    "out for delivery": Status.OUT_FOR_DELIVERY,
    "on the way": Status.IN_TRANSIT,
    "in transit": Status.IN_TRANSIT,
    "we have your package": Status.IN_TRANSIT,
    "arrived at facility": Status.IN_TRANSIT,
    "departed from facility": Status.IN_TRANSIT,
    "processing at ups facility": Status.IN_TRANSIT,
    "origin scan": Status.IN_TRANSIT,
    "label created": Status.PENDING,
    "order processed": Status.PENDING,
    "exception": Status.EXCEPTION,
    "delivery attempt": Status.EXCEPTION,
    "returned": Status.EXCEPTION,
}

# --- Full "Parcel History" parsing (revealed by clicking "Show Details") -------
# The block is flat text repeating: date, time, headline, [detail], location.
_UPS_H_DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_UPS_H_TIME = re.compile(r"^\d{1,2}:\d{2}\s*[AP]\.?M\.?$", re.I)


def _history_status(headline: str) -> Status:
    st = _normalize(headline)
    if st is not Status.UNKNOWN:
        return st
    # Any other scan is movement through the network.
    return Status.IN_TRANSIT


def _parse_ups_hist_dt(date_s: str, time_s: str) -> Optional[datetime]:
    cleaned = f"{date_s} {time_s}".replace(".", "").upper()
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y %I:%M %p")
    except ValueError:
        return None


def _parse_ups_history(lines: list[str]) -> list[TrackingEvent]:
    """Parse the Parcel History flat text into events (kept newest-first)."""
    events: list[TrackingEvent] = []
    n = len(lines)
    i = 0
    while i < n:
        if _UPS_H_DATE.match(lines[i]) and i + 1 < n and _UPS_H_TIME.match(lines[i + 1]):
            date_s, time_s = lines[i], lines[i + 1]
            j = i + 2
            block: list[str] = []
            # An event has at most 3 content lines: headline, [detail], location.
            while j < n and len(block) < 3:
                if _UPS_H_DATE.match(lines[j]) and j + 1 < n and _UPS_H_TIME.match(lines[j + 1]):
                    break
                block.append(lines[j])
                j += 1
            headline = block[0] if block else "Update"
            location = block[-1] if len(block) >= 2 else None
            events.append(TrackingEvent(
                timestamp=_parse_ups_hist_dt(date_s, time_s),
                location=location,
                description=headline,
                status=_history_status(headline),
            ))
            i = j
        else:
            i += 1
    return events

_MILESTONES = ["Label Created", "We Have Your Package", "On the Way",
               "Out for Delivery", "Delivery", "Delivered"]

# Pull status / estimated delivery / last location / milestones in one shot.
_EXTRACT_JS = r"""
// Icon fonts render their glyph from the element's TEXT ("check_circle"), so a
// naive textContent picks it up and we end up with "Delivered check_circle".
//
// Drop those tokens from the STRING rather than removing icon nodes from the
// DOM: an earlier version deleted every <i> in the subtree, and where UPS wraps
// the headline in one that erased the status itself, turning every UPS row into
// "no status found". Filtering snake_case words is purely subtractive — real
// status text ("Delivered", "On the Way", "Label Created") never looks like
// that — and the original string is kept if filtering would empty it.
const LIGATURE = /^[a-z][a-z0-9]*(_[a-z0-9]+)+$/;
const txt = (e) => {
  if (!e) return null;
  const raw = e.textContent.replace(/\s+/g,' ').trim();
  if (!raw) return null;
  const kept = raw.split(' ').filter(w => !LIGATURE.test(w)).join(' ').trim();
  return kept || raw;
};
let out = {};

out.status = txt(document.querySelector('#stApp_nameKey'));

const estDiv = document.querySelector('.estimatedDiv');
out.estimated = (estDiv && estDiv.nextElementSibling)
    ? txt(estDiv.nextElementSibling) : null;

// Last location line
let last = null;
document.querySelectorAll('*').forEach(el => {
  if (el.children.length === 0 && /Last Location/i.test(el.textContent) && !last)
    last = txt(el.parentElement);
});
out.last_location = last;

// Milestones: find each known label span and read its state sibling text.
const wanted = %MILESTONES%;
const steps = [];
document.querySelectorAll('*').forEach(el => {
  if (el.children.length !== 0) return;
  if (el.closest('#stApp_nameKey')) return;  // skip headline; it shadows a milestone label
  const t = el.textContent.trim();
  if (!wanted.includes(t)) return;
  // state usually a nearby element with text completed/active/inactive
  let state = '';
  let p = el.parentElement;
  for (let i = 0; i < 4 && p; i++) {
    const m = (p.textContent||'').match(/\b(completed|active|inactive)\b/i);
    if (m) { state = m[1].toLowerCase(); break; }
    p = p.parentElement;
  }
  if (!steps.find(s => s.label === t)) steps.push({label: t, state});
});
out.steps = steps;
return JSON.stringify(out);
""".replace("%MILESTONES%", json.dumps(_MILESTONES))


def _normalize(text: str) -> Status:
    t = (text or "").lower()
    for key, status in _STATUS_MAP.items():
        if key in t:
            return status
    return Status.UNKNOWN


def _parse_est(text: Optional[str]) -> Optional[datetime]:
    """'Friday, July 03 by End of Day' -> a date (year assumed current)."""
    if not text:
        return None
    m = re.search(r"([A-Z][a-z]+ \d{1,2})", text)
    if not m:
        return None
    for fmt in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(f"{m.group(1)} {datetime.now().year}", fmt)
        except ValueError:
            continue
    return None


def _parse_last_location(text: Optional[str]):
    """'Last Location: New Delhi Airport, India, 07/02/2026, 2:06 P.M.'"""
    if not text:
        return None, None
    body = re.sub(r"^.*Last Location:\s*", "", text).strip()
    dt = None
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}),?\s*(\d{1,2}:\d{2}\s*[AP]\.?M\.?)", body)
    if m:
        cleaned = f"{m.group(1)} {m.group(2)}".replace(".", "").upper()
        try:
            dt = datetime.strptime(cleaned, "%m/%d/%Y %I:%M %p")
        except ValueError:
            dt = None
        body = body[:m.start()].rstrip(", ").strip()
    return (body or None), dt


# Labels shown in UPS's "Shipment Details" block (revealed by "Show Details").
_UPS_STOP = {
    "support", "help and support center", "our company", "change my delivery",
    "notify me", "products & services",
}


def _parse_ups_details(lines: list[str]) -> dict:
    """
    Parse the "Shipment Details" label/value block, e.g.:
        Shipment Details
        Ship To            -> MORGANTON, NC US
        Service            -> UPS Worldwide Express Saver
        Shipment Category  -> Package
        Shipped / Billed On-> 06/30/2026
        Weight             -> 2.0 KGS   (when present)
    Returns {service, weight, destination, details{}}.
    """
    out: dict = {"service": None, "weight": None, "destination": None, "details": {}}
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip().lower() == "shipment details")
    except StopIteration:
        return out

    block: list[str] = []
    for ln in lines[start + 1 :]:
        if ln.strip().lower() in _UPS_STOP:
            break
        block.append(ln.strip())

    for i in range(0, len(block) - 1, 2):
        label, value = block[i], block[i + 1]
        low = label.lower()
        if not value:
            continue
        if low == "service":
            # Drop a trailing ® / mangled encoding char (e.g. "Saver®" or "Saver�").
            out["service"] = re.sub(r"[^A-Za-z0-9)]+$", "", value).strip()
        elif low == "weight":
            out["weight"] = value
        elif low in ("ship to", "delivered to"):
            out["destination"] = value
            out["details"][label] = value
        else:
            out["details"][label] = value
    return out


class UPSScraper(BaseScraper):
    carrier = Carrier.UPS

    def build_url(self, tracking_number: str) -> str:
        return (f"https://www.ups.com/track?tracknum={tracking_number}"
                f"&loc=en_US&requester=ST/trackdetails")

    def api_url(self, tracking_number: str) -> Optional[str]:
        return None  # UPS uses a POST API; DOM parsing is reliable

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        # UPS loads data async — poll until the status headline appears.
        for _ in range(15):
            sb.sleep(2)
            if sb.execute_script("return !!document.querySelector('#stApp_nameKey')"):
                break

        try:
            data = json.loads(sb.execute_script(_EXTRACT_JS) or "{}")
        except Exception as e:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          f"extract script failed: {e}")

        if not data.get("status"):
            return TrackingResult.failure(tracking_number, self.carrier,
                                          "no status found (invalid number or page changed)")

        status = _normalize(data["status"])
        loc, loc_dt = _parse_last_location(data.get("last_location"))

        events: list[TrackingEvent] = []
        if data.get("last_location"):
            events.append(TrackingEvent(
                timestamp=loc_dt, location=loc,
                description=data["status"], status=status,
            ))
        for s in data.get("steps", []):
            if s.get("state") in ("completed", "active"):
                events.append(TrackingEvent(
                    description=s["label"],
                    status=_normalize(s["label"]),
                ))

        # Expand "Show Details" for the full Parcel History + shipment details.
        details: dict = {"service": None, "weight": None, "destination": None, "details": {}}
        history: list[TrackingEvent] = []
        try:
            # Click every "Show/Hide Details" toggle (there can be more than one:
            # delivery proof + parcel history) and scroll to trigger lazy loads.
            sb.execute_script(
                "for(const el of document.querySelectorAll('button,a,span,div')){"
                "const t=(el.textContent||'').trim().toLowerCase();"
                "if(t==='show details'||t==='hide details'||t.includes('parcel history'))"
                "{try{el.click();}catch(e){}}}")
            sb.sleep(2)
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            sb.sleep(1.5)
            lines = [ln.strip() for ln in sb.get_text("body").split("\n") if ln.strip()]
            details = _parse_ups_details(lines)
            history = _parse_ups_history(lines)
        except Exception:
            pass

        # Prefer the full parcel history; fall back to the milestone summary.
        if history:
            events = history
        elif not events:
            # Never leave a valid result event-less: record the headline status.
            events.append(TrackingEvent(description=data["status"], status=status))

        # The headline occasionally renders as something we can't map (a promo
        # banner, a wording change), leaving UNKNOWN while the parcel history we
        # just parsed plainly says "Delivered". Trust the freshest scan in that
        # case rather than reporting Unknown over data we already hold.
        if status == Status.UNKNOWN:
            newest = next((e.status for e in events if e.status != Status.UNKNOWN), None)
            if newest is not None:
                status = newest

        # Freshest scan drives delivered_at (history is newest-first).
        latest_dt = events[0].timestamp if events and events[0].timestamp else loc_dt

        return TrackingResult(
            tracking_number=tracking_number, carrier=self.carrier,
            status=status,
            estimated_delivery=_parse_est(data.get("estimated")),
            destination=details["destination"],
            service=details["service"],
            weight=details["weight"],
            details=details["details"],
            delivered_at=latest_dt if status == Status.DELIVERED else None,
            events=events,
        )
