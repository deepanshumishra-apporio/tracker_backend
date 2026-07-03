"""
Aramex scraper — FINALIZED against the live site (2026-07).

Verified DOM structure (www.aramex.com/us/en/track/results):
  a.shipment-card                       one per tracking number
    .shipment-tag                       service type ("International Shipment")
    .shipment-num h5                    the tracking number
    .shipment-update-descp              latest status text
    .shipment-update-datetime           "02 Jul 26 05:04"
    .orgin-info .country / .city        origin (note: site's own typo "orgin")
    .dest-info  .country / .city        destination
    .shipment-progess-point             one per stage; class "done"/"current"
                                        (note: site's own typo "progess")
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.base import BaseScraper

# Map the active stage label -> normalized status.
_STAGE_STATUS = {
    "created": Status.PENDING,
    "collected": Status.PENDING,
    "departed": Status.IN_TRANSIT,
    "in transit": Status.IN_TRANSIT,
    "arrived at destination": Status.IN_TRANSIT,
    "out for delivery": Status.OUT_FOR_DELIVERY,
    "delivered": Status.DELIVERED,
    "held for pickup": Status.EXCEPTION,
    "returned": Status.EXCEPTION,
}

# JS that pulls one structured record per shipment card on the page.
_EXTRACT_JS = r"""
const cards = document.querySelectorAll('a.shipment-card');
const out = [];
for (const card of cards) {
  const q = (s) => { const e = card.querySelector(s); return e ? e.textContent.trim() : null; };
  const steps = [...card.querySelectorAll('.shipment-progess-point')].map(p => ({
    label: (p.querySelector('.track-check-info > span') || {}).textContent
             ? p.querySelector('.track-check-info > span').textContent.trim() : '',
    done: p.className.includes('done'),
    current: p.className.includes('current'),
  }));
  out.push({
    number: q('.shipment-num h5'),
    tag: q('.shipment-tag'),
    update: q('.shipment-update-descp'),
    datetime: q('.shipment-update-datetime'),
    origin_country: q('.orgin-info .country'),
    origin_city: q('.orgin-info .city'),
    dest_country: q('.dest-info .country'),
    dest_city: q('.dest-info .city'),
    steps: steps,
  });
}
return JSON.stringify(out);
"""


def _join(country: Optional[str], city: Optional[str]) -> Optional[str]:
    parts = [p for p in (city, country) if p]
    return ", ".join(parts) if parts else None


def _parse_history_table(rows: list[list[str]]) -> list[TrackingEvent]:
    """Rows come as [icon, Date, Location, Activity]; already newest-first."""
    events: list[TrackingEvent] = []
    for row in rows:
        cells = list(row)
        if len(cells) == 4:
            _, date_s, loc, act = cells
        elif len(cells) == 3:
            date_s, loc, act = cells
        else:
            continue
        if date_s.strip().lower() == "date":  # header row
            continue
        if not (date_s or loc or act):
            continue
        events.append(TrackingEvent(
            timestamp=_parse_dt(date_s),
            location=loc or None,
            description=act or "Update",
            status=_activity_status(act),
        ))
    return events


def _parse_dt(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    # Collapse the whitespace/newlines the details table wraps around the time.
    cleaned = " ".join((text or "").split())
    for fmt in ("%d %b %y %H:%M", "%d %b %y"):
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


def _activity_status(desc: str) -> Status:
    """Map a free-text activity line to a normalized status."""
    d = (desc or "").lower()
    if "delivered" in d:
        return Status.DELIVERED
    if "out for delivery" in d or "with the courier" in d:
        return Status.OUT_FOR_DELIVERY
    if "delay" in d or "held" in d or "returned" in d or "customs" in d and "clear" not in d:
        return Status.EXCEPTION
    if "collected" in d or "label" in d or "generated a new shipment" in d:
        return Status.PENDING
    return Status.IN_TRANSIT


# JS that reads the Shipment History table (Date | Location | Activity) plus the
# "Tracking Details" fields (Shipment Type, Number of Items, Weight) on the
# Aramex details page (reached by clicking a shipment card).
_HISTORY_JS = r"""
// Pick ONLY the Shipment History table (header contains Date + Activity) so we
// don't scrape unrelated tables (surcharges, footer) as bogus events.
let rows = [];
for (const tbl of document.querySelectorAll('table')) {
  const first = tbl.querySelector('tr');
  const head = first ? [...first.querySelectorAll('th,td')].map(c => c.innerText.trim().toLowerCase()) : [];
  if (head.includes('date') && head.includes('activity')) {
    rows = [...tbl.querySelectorAll('tr')].map(tr =>
      [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim()));
    break;
  }
}
const body = document.body.innerText.split('\n').map(s => s.trim()).filter(Boolean);
const labels = {'weight':'weight', 'number of items':'items', 'shipment type':'type'};
const fields = {};
for (let i = 0; i < body.length - 1; i++) {
  const key = labels[body[i].toLowerCase()];
  const val = body[i + 1];
  // Skip when a label has no value (its next line is another known label),
  // otherwise we'd store e.g. "Shipment Type" -> "Number of Items".
  if (key && !(key in fields) && val && !(val.toLowerCase() in labels)) {
    fields[key] = val;
  }
}
return JSON.stringify({rows, fields});
"""


class AramexScraper(BaseScraper):
    carrier = Carrier.ARAMEX

    def build_url(self, tracking_number: str) -> str:
        return (f"https://www.aramex.com/us/en/track/results"
                f"?ShipmentNumber={tracking_number}")

    def api_url(self, tracking_number: str) -> Optional[str]:
        return None  # DOM parsing is reliable and works without auth

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        try:
            records = json.loads(sb.execute_script(_EXTRACT_JS) or "[]")
        except Exception as e:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          f"extract script failed: {e}")

        # Pick the card matching our number (fall back to the first card).
        rec = next((r for r in records if (r.get("number") or "").strip() == tracking_number),
                   records[0] if records else None)
        if not rec:
            return TrackingResult.failure(tracking_number, self.carrier,
                                          "no shipment card found (bad number or page changed)")

        # Status: prefer the "current" stage; else the last "done" stage.
        current = next((s["label"] for s in rec["steps"] if s.get("current")), None)
        if not current:
            done = [s["label"] for s in rec["steps"] if s.get("done")]
            current = done[-1] if done else None
        status = _STAGE_STATUS.get((current or "").lower(), Status.UNKNOWN)

        when = _parse_dt(rec.get("datetime"))

        # Drill into the details page for the FULL history + shipment fields.
        hist_events, fields = self._fetch_details(sb)
        weight = fields.get("weight")
        # "Number of Items" -> pieces (integer when parseable).
        pieces = None
        if fields.get("items"):
            m = re.search(r"\d+", fields["items"])
            pieces = int(m.group()) if m else None
        # Prefer the specific "Shipment Type" (e.g. "Parcel Express") over the
        # generic card tag ("International Shipment") for the service field.
        service = fields.get("type") or rec.get("tag") or None

        # Prefer the full history; fall back to the summary card's stages.
        if hist_events:
            events = hist_events
        else:
            events = []
            if rec.get("update"):
                events.append(TrackingEvent(
                    timestamp=when, description=rec["update"], status=status,
                    location=_join(rec.get("dest_country"), rec.get("dest_city")),
                ))
            for s in rec["steps"]:
                if s.get("done") or s.get("current"):
                    events.append(TrackingEvent(
                        description=s["label"],
                        status=_STAGE_STATUS.get(s["label"].lower(), Status.UNKNOWN),
                    ))

        return TrackingResult(
            tracking_number=rec.get("number") or tracking_number,
            carrier=self.carrier,
            status=status,
            origin=_join(rec.get("origin_country"), rec.get("origin_city")),
            destination=_join(rec.get("dest_country"), rec.get("dest_city")),
            service=service,
            weight=weight,
            pieces=pieces,
            delivered_at=when if status == Status.DELIVERED else None,
            events=events,
        )

    def _fetch_details(self, sb):
        """Click into the details page and parse its history table + fields.

        Returns (events, fields_dict). Best-effort: returns ([], {}) if the
        details page doesn't load, so the caller falls back to the summary card.
        """
        try:
            sb.execute_script(
                "const c=document.querySelector('a.shipment-card');if(c)c.click();")
            for _ in range(8):
                sb.sleep(1)
                if sb.execute_script("return document.querySelectorAll('table tr').length > 1"):
                    break
            data = json.loads(sb.execute_script(_HISTORY_JS))
        except Exception:
            return [], {}
        return _parse_history_table(data.get("rows") or []), (data.get("fields") or {})
