"""
DHL through its official Shipment Tracking - Unified API.

The website can't be sped up: every call to www.dhl.com/utapi answers HTTP 428
with an Akamai proof-of-work challenge, and a solved challenge does not carry
over to the next call — so each lookup costs a real browser ~30-45s. The
official API (developer.dhl.com, free key) answers the same data as JSON in
well under a second, with no bot wall.

Enabled when DHL_API_KEY is set. The free tier is rate limited (by default one
call per 5s and 250 a day); DHL_API_MIN_INTERVAL spaces calls to match, and a
429 leaves the number transient so it falls back to the browser.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import config
from fast.http import TransientError, session
from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.dhl import NOT_FOUND, _status_from_title

API = "https://api-eu.dhl.com/track/shipments"

_CODE_STATUS = {
    "pre-transit": Status.PENDING,
    "transit": Status.IN_TRANSIT,
    "delivered": Status.DELIVERED,
    "failure": Status.EXCEPTION,
}


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _place(loc: Optional[dict[str, Any]]) -> Optional[str]:
    addr = (loc or {}).get("address") or {}
    parts = [addr.get("addressLocality"), addr.get("countryCode")]
    return ", ".join(p for p in parts if p) or None


def _status(item: dict[str, Any]) -> Status:
    """statusCode is coarse ('transit' covers out-for-delivery); the wording
    refines it the same way the browser scraper reads DHL's event titles."""
    code = _CODE_STATUS.get((item.get("statusCode") or "").lower())
    text = item.get("description") or item.get("status") or ""
    if code is Status.IN_TRANSIT or code is None:
        worded = _status_from_title(text) if text else None
        if worded in (Status.OUT_FOR_DELIVERY, Status.EXCEPTION, Status.DELIVERED):
            return worded
    return code or (Status.IN_TRANSIT if text else Status.UNKNOWN)


def parse(number: str, payload: dict[str, Any]) -> TrackingResult:
    """One /track/shipments response -> TrackingResult."""
    shipments = payload.get("shipments") or []
    if not shipments:
        return TrackingResult.failure(number, Carrier.DHL, NOT_FOUND)
    s = shipments[0]

    events = [
        TrackingEvent(
            timestamp=_dt(e.get("timestamp")),
            location=_place(e.get("location")),
            description=(e.get("description") or e.get("status") or "Update").strip(),
            status=_status(e),
        )
        for e in s.get("events") or []
    ]
    events.sort(key=lambda e: e.timestamp or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True)

    status = _status(s.get("status") or {})
    if status is Status.UNKNOWN and events:
        status = events[0].status

    details_in = s.get("details") or {}
    weight = details_in.get("weight") or {}
    weight_s = f"{weight['value']} {weight.get('unitText', '')}".strip() if weight.get("value") else None
    pieces = details_in.get("totalNumberOfPieces")
    try:
        pieces = int(pieces) if pieces is not None else None
    except (TypeError, ValueError):
        pieces = None

    pod = details_in.get("proofOfDelivery") or {}
    signed = (pod.get("signed") or {}).get("name") or None

    details: dict[str, str] = {}
    if s.get("id"):
        details["Waybill Number"] = str(s["id"])
    piece_ids = details_in.get("pieceIds") or []
    if piece_ids:
        details["Piece ID"] = ", ".join(map(str, piece_ids))
    if (s.get("status") or {}).get("status"):
        details["DHL status"] = str(s["status"]["status"])

    delivered_at = None
    if status is Status.DELIVERED:
        delivered_at = _dt((s.get("status") or {}).get("timestamp")) or next(
            (e.timestamp for e in events if e.status is Status.DELIVERED), None)

    return TrackingResult(
        tracking_number=number,
        carrier=Carrier.DHL,
        status=status,
        estimated_delivery=_dt(s.get("estimatedTimeOfDelivery")),
        delivered_at=delivered_at,
        origin=_place(s.get("origin")),
        destination=_place(s.get("destination")),
        service=((details_in.get("product") or {}).get("productName")) or None,
        weight=weight_s,
        pieces=pieces,
        signed_by=signed,
        details=details,
        events=events,
        scraped_at=datetime.now(timezone.utc),
    )


class DHLFast:
    carrier = Carrier.DHL

    def __init__(self, api_key: Optional[str] = None, min_interval: Optional[float] = None):
        self.api_key = api_key or config.DHL_API_KEY
        self.min_interval = config.DHL_API_MIN_INTERVAL if min_interval is None else min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def _pace(self) -> None:
        """Keep calls at least min_interval apart (the API's per-key limit)."""
        with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _call(self, number: str) -> TrackingResult:
        self._pace()
        try:
            resp = session().get(
                API,
                params={"trackingNumber": number},
                headers={"DHL-API-Key": self.api_key, "Accept": "application/json"},
                timeout=config.FAST_TIMEOUT,
            )
        except Exception as exc:
            raise TransientError(f"DHL API did not answer: {exc}") from exc
        if resp.status_code == 404:
            return TrackingResult.failure(number, Carrier.DHL, NOT_FOUND)
        if resp.status_code in (401, 403):
            raise TransientError("DHL API rejected the key (check DHL_API_KEY)")
        if resp.status_code == 429:
            raise TransientError("DHL API rate limit reached")
        if resp.status_code != 200:
            raise TransientError(f"DHL API answered HTTP {resp.status_code}")
        try:
            return parse(number, resp.json())
        except ValueError:
            raise TransientError("DHL API returned non-JSON") from None

    def track_many(self, numbers: list[str]) -> tuple[dict[str, TrackingResult], dict[str, str]]:
        # Sequential on purpose: the key's rate limit, not the network, is the
        # bottleneck, and a 429 on one call means the next would 429 too.
        results: dict[str, TrackingResult] = {}
        transient: dict[str, str] = {}
        for n in dict.fromkeys(numbers):
            try:
                results[n] = self._call(n)
            except TransientError as exc:
                transient[n] = str(exc)
        return results, transient
