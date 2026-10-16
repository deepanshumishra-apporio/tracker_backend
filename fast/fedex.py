"""
FedEx through its official Track API (developer.fedex.com).

fedex.com cannot be fetched from a server: Akamai answers the tracking page and
the JSON API behind it (api.fedex.com/track/v2) with "Access Denied" or a fake
"can't find that tracking number" page unless the call comes from a browser
that has passed its sensor checks on a residential IP — even Scrape.do clears
it only now and then. The official API (apis.fedex.com) has no bot wall: one
OAuth token, then up to 30 numbers per POST, answered in a second or two.

Enabled when FEDEX_API_KEY and FEDEX_SECRET_KEY are set (a free FedEx developer
project with the "Track API" added).
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

import config
from fast.http import TransientError, session
from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.fedex import _event_status

CHUNK = 30   # the Track API's limit per request
NOT_FOUND = ("FedEx has no record of this number (it may be invalid, or aged "
             "out after delivery).")

# latestStatusDetail.derivedCode / code -> our bucket. Anything else that has
# scans is in transit.
_CODE_STATUS = {
    "DL": Status.DELIVERED,
    "OD": Status.OUT_FOR_DELIVERY,
    "DE": Status.EXCEPTION, "SE": Status.EXCEPTION, "DY": Status.EXCEPTION,
    "CA": Status.EXCEPTION, "RS": Status.EXCEPTION, "HL": Status.IN_TRANSIT,
    "OC": Status.PENDING, "IN": Status.PENDING,
}


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _place(addr: Optional[dict[str, Any]]) -> Optional[str]:
    if not addr:
        return None
    parts = [addr.get("city"), addr.get("stateOrProvinceCode"), addr.get("countryCode")]
    return ", ".join(p for p in parts if p) or None


def _when(track: dict[str, Any], kind: str) -> Optional[datetime]:
    for d in track.get("dateAndTimes") or []:
        if d.get("type") == kind:
            return _dt(d.get("dateTime"))
    return None


def parse_one(number: str, track: dict[str, Any]) -> TrackingResult:
    """One trackResults[] entry -> TrackingResult."""
    err = track.get("error") or {}
    if err:
        if "NOTFOUND" in (err.get("code") or "").upper():
            return TrackingResult.failure(number, Carrier.FEDEX, NOT_FOUND)
        return TrackingResult.failure(
            number, Carrier.FEDEX, err.get("message") or err.get("code") or "FedEx error")

    events = [
        TrackingEvent(
            timestamp=_dt(e.get("date")),
            location=_place(e.get("scanLocation")),
            description=(e.get("eventDescription") or "Update").strip(),
            status=_CODE_STATUS.get(e.get("derivedStatusCode") or "")
            or _event_status(e.get("eventDescription") or ""),
        )
        for e in track.get("scanEvents") or []
    ]

    latest = track.get("latestStatusDetail") or {}
    code = latest.get("derivedCode") or latest.get("code") or ""
    status = _CODE_STATUS.get(code)
    if status is None:
        status = Status.IN_TRANSIT if events else Status.UNKNOWN

    pkg = track.get("packageDetails") or {}
    weights = (pkg.get("weightAndDimensions") or {}).get("weight") or []
    weight = f"{weights[0]['value']} {weights[0].get('unit', '')}".strip() if weights else None
    try:
        pieces = int(pkg["count"]) if pkg.get("count") else None
    except (TypeError, ValueError):
        pieces = None

    delivery = track.get("deliveryDetails") or {}
    origin = _place(((track.get("originLocation") or {}).get("locationContactAndAddress") or {}).get("address")) \
        or _place((track.get("shipperInformation") or {}).get("address"))
    destination = _place(((track.get("destinationLocation") or {}).get("locationContactAndAddress") or {}).get("address")) \
        or _place((track.get("recipientInformation") or {}).get("address"))

    estimated = _when(track, "ESTIMATED_DELIVERY") or _dt(
        ((track.get("estimatedDeliveryTimeWindow") or {}).get("window") or {}).get("ends"))

    details: dict[str, str] = {}
    if latest.get("statusByLocale") or latest.get("description"):
        details["FedEx status"] = latest.get("statusByLocale") or latest["description"]
    if delivery.get("receivedByName"):
        details["Received by"] = delivery["receivedByName"]
    ship = _when(track, "SHIP") or _when(track, "ACTUAL_PICKUP")
    if ship:
        details["Shipped on"] = ship.date().isoformat()

    return TrackingResult(
        tracking_number=number,
        carrier=Carrier.FEDEX,
        status=status,
        estimated_delivery=estimated if status is not Status.DELIVERED else None,
        delivered_at=(_when(track, "ACTUAL_DELIVERY")
                      or next((e.timestamp for e in events if e.status is Status.DELIVERED), None))
        if status is Status.DELIVERED else None,
        origin=origin,
        destination=destination,
        service=(track.get("serviceDetail") or {}).get("description") or None,
        weight=weight,
        pieces=pieces,
        signed_by=delivery.get("signedByName") or delivery.get("receivedByName") or None,
        details=details,
        events=events,
        scraped_at=datetime.now(timezone.utc),
    )


def parse(payload: dict[str, Any]) -> dict[str, TrackingResult]:
    """A whole /track/v1/trackingnumbers response -> {number: result}."""
    out: dict[str, TrackingResult] = {}
    for complete in (payload.get("output") or {}).get("completeTrackResults") or []:
        number = complete.get("trackingNumber")
        results = complete.get("trackResults") or []
        if number and results:
            out[number] = parse_one(number, results[0])
    return out


class FedExFast:
    carrier = Carrier.FEDEX

    def __init__(self, key: Optional[str] = None, secret: Optional[str] = None,
                 base: Optional[str] = None, workers: Optional[int] = None):
        self.key = key or config.FEDEX_API_KEY
        self.secret = secret or config.FEDEX_SECRET_KEY
        self.base = (base or config.FEDEX_API_URL).rstrip("/")
        self.workers = workers or config.FAST_WORKERS
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires = 0.0

    def _auth(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or time.monotonic() > self._expires:
                try:
                    resp = session().post(
                        f"{self.base}/oauth/token",
                        data={"grant_type": "client_credentials",
                              "client_id": self.key, "client_secret": self.secret},
                        timeout=config.FAST_TIMEOUT,
                    )
                except Exception as exc:
                    raise TransientError(f"FedEx OAuth did not answer: {exc}") from exc
                if resp.status_code != 200:
                    raise TransientError(
                        f"FedEx OAuth answered HTTP {resp.status_code} "
                        "(check FEDEX_API_KEY / FEDEX_SECRET_KEY)")
                body = resp.json()
                self._token = body["access_token"]
                # Renew a minute early so a call never carries a dying token.
                self._expires = time.monotonic() + float(body.get("expires_in", 3600)) - 60
            return self._token

    def _call(self, chunk: list[str]) -> dict[str, TrackingResult]:
        body = {"includeDetailedScans": True,
                "trackingInfo": [{"trackingNumberInfo": {"trackingNumber": n}} for n in chunk]}
        for attempt in range(2):
            token = self._auth(force=attempt > 0)
            try:
                resp = session().post(
                    f"{self.base}/track/v1/trackingnumbers", json=body,
                    headers={"Authorization": f"Bearer {token}",
                             "Content-Type": "application/json", "X-locale": "en_US"},
                    timeout=config.FAST_TIMEOUT,
                )
            except Exception as exc:
                raise TransientError(f"FedEx API did not answer: {exc}") from exc
            if resp.status_code == 401 and attempt == 0:
                continue   # token revoked early: get a new one once
            if resp.status_code != 200:
                raise TransientError(f"FedEx API answered HTTP {resp.status_code}")
            try:
                return parse(resp.json())
            except ValueError:
                raise TransientError("FedEx API returned non-JSON") from None
        raise TransientError("FedEx API kept refusing a fresh token")

    def track_many(self, numbers: list[str]) -> tuple[dict[str, TrackingResult], dict[str, str]]:
        numbers = list(dict.fromkeys(numbers))
        chunks = [numbers[i:i + CHUNK] for i in range(0, len(numbers), CHUNK)]
        results: dict[str, TrackingResult] = {}
        transient: dict[str, str] = {}

        def one(chunk: list[str]):
            try:
                return chunk, self._call(chunk), None
            except TransientError as exc:
                return chunk, {}, str(exc)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for chunk, got, err in pool.map(one, chunks):
                for n in chunk:
                    if err:
                        transient[n] = err
                    elif n in got:
                        results[n] = got[n]
                    else:
                        transient[n] = "FedEx API left this number out of its answer"
        return results, transient
