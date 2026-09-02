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

import config
from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.base import BaseScraper, Blocked

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

// Shipment fields, read by id. UPS publishes them as label/value element
// pairs that are in the DOM from first render — no clicking, and unaffected by
// the Package History modal, which replaces the page's visible text while it is
// open and so hid these from any text-based parse. The ids were captured off
// the live page (2026-09); the label element supplies the key, so a wording
// change ("Ship To" -> "Delivered To") still lands in `details`.
const FIELD_IDS = [
  ['stApp_lblShipTo', 'stApp_txtAddress', 'stApp_txtCountry'],
  ['stApp_txtReceivedBy', 'stApp_valReceivedBy'],
  ['stApp_lbl_AdditionalInfoService', 'stApp_link_AdditionalInfoService'],
  ['stApp_lblAdditionalInfoShipmentCat', 'stApp_txtAdditionalInfoShipmentCat'],
  ['stApp_lblAdditionalInfoBilledOn', 'stApp_txtAdditionalInfoBilledOn'],
  ['stApp_lblAdditionalInfoWeight', 'stApp_txtAdditionalInfoWeight'],
  // The Proof of Delivery panel uses its own ids, and exists only once that
  // modal has been rendered — read it too when it happens to be there.
  ['stApp_PODlblService', 'stApp_PODtxtService'],
  ['stApp_PODlblDeliveredOn', 'stApp_PODtxtDeliveredOn'],
  ['stApp_PODlblDeliveredTo', 'stApp_PODtxtAddress'],
  ['stApp_PODlblReceivedBy', 'stApp_PODtxtReceivedBy'],
  ['stApp_PODlblBilledOn', 'stApp_PODtxtBilledOn'],
];
const fields = [];
for (const [labelId, ...valueIds] of FIELD_IDS) {
  const label = txt(document.getElementById(labelId));
  if (!label) continue;
  const value = valueIds.map(id => txt(document.getElementById(id)))
      .filter(Boolean).join(', ');
  if (value) fields.push([label, value]);
}
out.fields = fields;

return JSON.stringify(out);
""".replace("%MILESTONES%", json.dumps(_MILESTONES))


# UPS has two "no shipment here" pages and they mean opposite things, so they
# get separate marker sets. Matched against the rendered body, not the HTML
# source: the page is an SPA, so the message only exists after render.
#
# The form's counter ("0 of 25 tracking numbers entered") shows up on BOTH and
# used to be read as throttling — which is how an 8-digit reference number that
# UPS plainly calls invalid got reported as a rate limit. It is not a marker.

# UPS rejects the number itself: nothing about the caller will change this.
_INVALID_MARKERS = (
    "invalid tracking number",
    "may be invalid or not active yet",
)

# UPS won't answer the caller right now — the parcel is not the problem.
_REFUSAL_MARKERS = (
    "unable to complete your tracking request",
)

_INVALID = (
    "UPS says this tracking number is invalid or not active yet — check it "
    "with the sender. (UPS only tracks its own waybills: 1Z…, or a 9-digit "
    "or 12-digit UPS number.)"
)

_THROTTLED = (
    "UPS refused the lookup — it is rate-limiting this IP, not rejecting the "
    "number. A residential proxy (USE_PROXIES/PROXY_URL) is the fix; retrying "
    "from the same IP will not help."
)


def _page_verdict(sb) -> Optional[str]:
    """Which of UPS's dead-end pages we are on: 'invalid', 'refused' or None."""
    try:
        body = (sb.get_text("body") or "").lower()
    except Exception:
        return None
    if any(m in body for m in _INVALID_MARKERS):
        return "invalid"      # checked first: the more specific page
    if any(m in body for m in _REFUSAL_MARKERS):
        return "refused"
    return None


# Poll script: has the headline actually got TEXT yet, or has UPS already said
# it won't answer? Returns 'ready' | 'invalid' | 'refused' | ''.
#
# Waiting on querySelector() alone is what broke bulk runs: Angular inserts
# #stApp_nameKey empty and fills it a beat later, so the poll returned on the
# empty node, the extractor read "" and the row was reported as
# "no status found (invalid number or page changed)" — blaming the waybill for
# a race in our own wait. Slower the page (proxy, cold cache), the more rows hit
# it, which is why a bulk run failed almost every number while the same numbers
# tracked fine one at a time.
#
# Both dead-end pages are checked in the same script so an invalid or refused
# lookup stops after one poll instead of burning the full 30s wait for a
# headline that is never coming.
_READY_JS = r"""
const el = document.querySelector('#stApp_nameKey');
if (el && el.textContent.trim()) return 'ready';
const body = ((document.body && document.body.innerText) || '').toLowerCase();
if (%INVALID%.some(m => body.includes(m))) return 'invalid';
if (%REFUSED%.some(m => body.includes(m))) return 'refused';
return '';
""".replace("%INVALID%", json.dumps(list(_INVALID_MARKERS))
   ).replace("%REFUSED%", json.dumps(list(_REFUSAL_MARKERS)))


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


# UPS's own label wording -> the field it belongs in. Anything not listed is
# kept verbatim in `details`, so a field we have never seen is still reported.
_FIELD_LABELS = {
    "service": "service",
    "ship to": "destination",
    "delivered to": "destination",
    "received by": "signed_by",
    "delivered on": "delivered_on",
    "weight": "weight",
}


def _fields_to_details(fields: list) -> dict:
    """Fold the label/value pairs read off the page ids into typed fields."""
    out: dict = {"service": None, "weight": None, "destination": None,
                 "signed_by": None, "delivered_on": None, "details": {}}
    for pair in fields or ():
        try:
            label, value = pair[0], pair[1]
        except (TypeError, IndexError):
            continue
        if not label or not value:
            continue
        key = _FIELD_LABELS.get(str(label).strip().lower())
        cleaned = _clean_service(value) if key == "service" else value
        if key and out[key] is None:
            out[key] = cleaned
        # Every field is also kept under UPS's own wording, so nothing we read
        # is dropped just because it has no dedicated column.
        out["details"].setdefault(str(label).strip(), cleaned)
    return out


def _clean_service(value: Optional[str]) -> Optional[str]:
    """Drop a trailing ® or mangled encoding char ("Saver®" / "Saver�")."""
    if not value:
        return None
    return re.sub(r"[^A-Za-z0-9)]+$", "", value).strip() or None


def _parse_pod_dt(text: Optional[str]) -> Optional[datetime]:
    """'07/03/2026 10:05 A.M.' (UPS's Proof of Delivery panel) -> datetime."""
    if not text:
        return None
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})\s*,?\s*(\d{1,2}:\d{2}\s*[AP]\.?M\.?)",
                  text, re.I)
    if not m:
        return None
    cleaned = f"{m.group(1)} {m.group(2)}".replace(".", "").upper()
    try:
        return datetime.strptime(cleaned, "%m/%d/%Y %I:%M %p")
    except ValueError:
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
            out["service"] = _clean_service(value)
        elif low == "weight":
            out["weight"] = value
        elif low in ("ship to", "delivered to"):
            out["destination"] = value
            out["details"][label] = value
        else:
            out["details"][label] = value
    return out


# Open every collapsed panel that holds shipment data.
#
# The old matcher required the text to be EXACTLY "show details" — but UPS
# appends its icon ligature, so the element reads "Show Details
# keyboard_arrow_down" and nothing ever matched. Nothing was clicked, the
# Shipment Details block stayed collapsed, and service / weight / destination
# came back null on every row. Ligature words are stripped before comparing
# (same trick as the headline), and "hide details" is no longer clicked: doing
# both in one pass just closed what the first click opened.
_EXPAND_JS = r"""
const LIGATURE = /^[a-z][a-z0-9]*(_[a-z0-9]+)+$/;
const norm = (el) => (el.textContent || '').replace(/\s+/g, ' ').trim()
    .toLowerCase().split(' ').filter(w => !LIGATURE.test(w)).join(' ').trim();
const WANTED = ['show details', 'shipment details', 'parcel history'];
for (const el of document.querySelectorAll('button,a,span,div,h2,h3')) {
  if (el.children.length > 2) continue;          // containers, not controls
  const t = norm(el);
  if (WANTED.some(w => t === w || t.startsWith(w + ' '))) {
    try { el.click(); } catch (e) {}
  }
}
return true;
"""

# Has the expanded content actually arrived? The only thing worth waiting for
# is the full history: the shipment fields are read by id and are there from
# first paint. Matching on "Ship To" would have returned instantly — that text
# is one of those field labels — so the wait is keyed to the history panel
# itself, which is what the click opens.
_DETAILS_READY_JS = (
    "const b=(document.body&&document.body.innerText)||'';"
    "return /package history|parcel history/i.test(b);"
)


class UPSScraper(BaseScraper):
    carrier = Carrier.UPS

    def build_url(self, tracking_number: str) -> str:
        return (f"https://www.ups.com/track?tracknum={tracking_number}"
                f"&loc=en_US&requester=ST/trackdetails")

    def api_url(self, tracking_number: str) -> Optional[str]:
        return None  # UPS uses a POST API; DOM parsing is reliable

    def _dead_end(self, tracking_number: str, verdict: str) -> TrackingResult:
        """Turn one of UPS's dead-end pages into the right answer."""
        if verdict == "invalid":
            # UPS's own words about the number — a verdict on the data, not on
            # us, so it is returned (never retried) and says what to check.
            return TrackingResult.failure(tracking_number, self.carrier, _INVALID)
        return self._refusal(tracking_number)

    def _refusal(self, tracking_number: str) -> TrackingResult:
        """What to do when UPS refuses the lookup instead of answering it.

        The refusal is about the caller, not the parcel, so it must never be
        reported as a bad waybill. With a proxy configured we raise instead of
        returning: BrowserSession drops the browser and retries the row on a
        fresh one, which on a rotating gateway means a fresh exit IP — the only
        thing that actually clears this. Proxy-less, every retry would come
        from the same blocked IP, so don't spend the time.
        """
        if config.proxy_or_none():
            raise Blocked(_THROTTLED)
        return TrackingResult.failure(tracking_number, self.carrier, _THROTTLED)

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        # UPS loads data async — poll until the status headline has TEXT (see
        # _READY_JS: waiting for the node alone returns mid-render and loses the
        # status), or until the page turns out to be one of UPS's dead ends,
        # which no amount of waiting will turn into a shipment.
        # Half-second interval, not two: the headline usually lands within a
        # second of the page settling, and the old loop slept through it — 288
        # rows paid up to 2s each for nothing. Same 30s ceiling either way.
        for attempt in range(60):
            state = sb.execute_script(_READY_JS)
            if state == "ready":
                break
            if state in ("invalid", "refused"):
                return self._dead_end(tracking_number, state)
            if attempt < 59:
                sb.sleep(0.5)

        try:
            data = json.loads(sb.execute_script(_EXTRACT_JS) or "{}")
        except Exception as e:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          f"extract script failed: {e}")

        if not data.get("status"):
            # No headline after the full wait. Ask the page why before falling
            # back to the catch-all: UPS says outright when it considers the
            # number invalid, and it renders the same tracking page when it is
            # refusing the caller. The bare "no status found" wording pointed
            # at the waybill or our parser either way, and cost real time
            # chasing a bug that wasn't there.
            verdict = _page_verdict(sb)
            if verdict:
                return self._dead_end(tracking_number, verdict)
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
            sb.execute_script(_EXPAND_JS)
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
            # Poll for the expanded content instead of sleeping a flat 3.5s:
            # it is usually there within half a second, and when it never comes
            # we stop after 4 rather than parsing a half-open accordion.
            for _ in range(10):
                if sb.execute_script(_DETAILS_READY_JS):
                    break
                sb.sleep(0.4)
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

        # Shipment fields come from the page ids (see _EXTRACT_JS): they are
        # there from first render, so service / destination / signatory no
        # longer depend on a panel being expanded — which is why they used to
        # come back null on every row. The text-panel parse stays as a fallback
        # for the older page variant that publishes a "Shipment Details" block.
        by_id = _fields_to_details(data.get("fields") or [])
        extras = {**by_id["details"], **details["details"]}

        delivered_at = _parse_pod_dt(by_id["delivered_on"])
        if delivered_at is None and status == Status.DELIVERED:
            delivered_at = latest_dt

        return TrackingResult(
            tracking_number=tracking_number, carrier=self.carrier,
            status=status,
            estimated_delivery=_parse_est(data.get("estimated")),
            destination=by_id["destination"] or details["destination"],
            service=by_id["service"] or details["service"],
            weight=by_id["weight"] or details["weight"],
            signed_by=by_id["signed_by"],
            details=extras,
            delivered_at=delivered_at if status == Status.DELIVERED else None,
            events=events,
        )
