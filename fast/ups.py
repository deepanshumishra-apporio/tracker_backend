"""
UPS without a browser per number.

ups.com's tracking page calls a JSON API (webapis.ups.com .../Track/GetStatus)
that answers in ~2-4s with everything the page shows. Akamai guards it: a call
needs the cookies (_abck, bm_*) and XSRF token a real browser earns by loading
ups.com. So we pay for ONE stealth-browser visit, lift its cookies, and reuse
them for many plain HTTPS calls in parallel until UPS stops accepting them
(401/403) or they reach UPS_COOKIE_TTL — then warm up again.

The API takes one number per call (a list returns 402 "Invalid Request").
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
from fast.http import TransientError, session
from models import Carrier, Status, TrackingEvent, TrackingResult
from scrapers.ups import _INVALID, _clean_service, _history_status, _normalize

API = "https://webapis.ups.com/track/api/Track/GetStatus?loc=en_US"
WARM_URL = "https://www.ups.com/track?loc=en_US&requester=ST/"


class CookiesExpired(Exception):
    pass


def _dt(date_s: Optional[str], time_s: Optional[str]) -> Optional[datetime]:
    """'06/26/2026' + '4:42 P.M.' -> naive local datetime."""
    if not date_s:
        return None
    text = f"{date_s} {time_s or ''}".replace(".", "").upper().strip()
    for fmt in ("%m/%d/%Y %I:%M %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _gmt(act: dict[str, Any]) -> Optional[datetime]:
    """gmtDate 20260626 + gmtTime 15:42:41 (UTC) + gmtOffset +01:00 -> aware
    local time. Falls back to the display date/time."""
    try:
        utc = datetime.strptime(f"{act['gmtDate']} {act['gmtTime']}", "%Y%m%d %H:%M:%S")
        sign = -1 if act["gmtOffset"].startswith("-") else 1
        hh, mm = act["gmtOffset"].lstrip("+-").split(":")
        offset = timedelta(hours=int(hh), minutes=int(mm)) * sign
        return (utc + offset).replace(tzinfo=timezone(offset))
    except (KeyError, ValueError, AttributeError, TypeError):
        return _dt(act.get("date"), act.get("time"))


def _place(addr: Optional[dict[str, Any]]) -> Optional[str]:
    if not addr:
        return None
    parts = [addr.get(k) for k in ("city", "state", "country")]
    return ", ".join(p for p in parts if p) or None


def parse(number: str, payload: dict[str, Any]) -> TrackingResult:
    """One GetStatus response -> TrackingResult."""
    details = payload.get("trackDetails") or []
    d = details[0] if details else None
    if not d or d.get("errorCode") or not (d.get("packageStatus") or d.get("shipmentProgressActivities")):
        return TrackingResult.failure(number, Carrier.UPS, _INVALID)

    events = [
        TrackingEvent(
            timestamp=_gmt(a),
            location=a.get("location") or None,
            description=(a.get("activityScan") or "").strip() or "Update",
            status=_history_status(a.get("activityScan") or ""),
        )
        for a in d.get("shipmentProgressActivities") or []
    ]

    if d.get("isDelivered") or d.get("packageStatusType") == "D":
        status = Status.DELIVERED
    else:
        status = _normalize(d.get("packageStatus") or "")
        if status is Status.UNKNOWN:
            status = _normalize((d.get("currentMilestone") or {}).get("name") or "")
        if status is Status.UNKNOWN and events:
            status = Status.IN_TRANSIT

    delivered_at = None
    if status is Status.DELIVERED:
        delivered_at = next((e.timestamp for e in events if e.status is Status.DELIVERED), None)

    info = d.get("additionalInformation") or {}
    weight = info.get("weight") or None
    if weight and info.get("weightUnit"):
        weight = f"{weight} {info['weightUnit']}"
    pieces = info.get("numberOfPieces")
    try:
        pieces = int(pieces) if pieces else None
    except (TypeError, ValueError):
        pieces = None

    milestones = d.get("milestones") or []
    origin = _place(d.get("shipFromAddress")) or (milestones[0].get("location") if milestones else None)

    extra: dict[str, str] = {}
    if d.get("leftAt"):
        extra["Left at"] = d["leftAt"]
    if info.get("shippedOrBilledDate"):
        extra["Shipped/Billed on"] = info["shippedOrBilledDate"]
    for ref in info.get("referenceNumbers") or []:
        if isinstance(ref, dict) and ref.get("number"):
            extra.setdefault("Reference", str(ref["number"]))
    if d.get("packageStatus"):
        extra["UPS status"] = str(d["packageStatus"])

    return TrackingResult(
        tracking_number=number,
        carrier=Carrier.UPS,
        status=status,
        estimated_delivery=_dt(d.get("scheduledDeliveryDate"), None),
        delivered_at=delivered_at,
        origin=origin,
        destination=_place(d.get("shipToAddress")) or _place(d.get("deliveryAddress")),
        service=_clean_service((info.get("serviceInformation") or {}).get("serviceName")),
        weight=weight,
        pieces=pieces,
        signed_by=d.get("receivedBy") or None,
        details=extra,
        events=events,
        scraped_at=datetime.now(timezone.utc),
    )


class UPSFast:
    carrier = Carrier.UPS

    def __init__(self, workers: Optional[int] = None):
        self.workers = workers or config.FAST_WORKERS
        self._lock = threading.Lock()
        self._cookies: dict[str, str] = {}
        self._ua: Optional[str] = None
        self._warmed_at = 0.0

    # -- cookies -----------------------------------------------------------
    def _warm(self) -> None:
        """Open ups.com once in the stealth browser and keep its cookies."""
        from scrapers.ups import UPSScraper  # browser stack only when needed

        browser, sb = UPSScraper()._launch()
        try:
            sb.uc_open_with_reconnect(WARM_URL, reconnect_time=config.RECONNECT_TIME)
            cookies: dict[str, str] = {}
            for _ in range(30):
                cookies = {c["name"]: c["value"] for c in sb.driver.get_cookies()}
                if cookies.get("X-XSRF-TOKEN-ST") and cookies.get("_abck"):
                    break
                sb.sleep(1)
            ua = sb.execute_script("return navigator.userAgent")
        finally:
            try:
                browser.__exit__(None, None, None)
            except Exception:
                pass
        if not cookies.get("X-XSRF-TOKEN-ST"):
            raise TransientError("UPS did not hand out a session token (blocked page?)")
        self._cookies, self._ua, self._warmed_at = cookies, ua, time.monotonic()

    def _ensure_warm(self, force: bool = False, seen_at: Optional[float] = None) -> float:
        """Warm cookies if missing/old. `seen_at` lets the many threads that hit
        an expiry together trigger ONE re-warm: whoever sees an older stamp than
        the current one knows someone else already refreshed."""
        with self._lock:
            stale = time.monotonic() - self._warmed_at > config.UPS_COOKIE_TTL
            if not self._cookies or stale or (force and seen_at == self._warmed_at):
                self._warm()
            return self._warmed_at

    # -- calls -------------------------------------------------------------
    def _call(self, number: str) -> TrackingResult:
        s = session()
        s.cookies.update(self._cookies)
        try:
            resp = s.post(
                API,
                json={"Locale": "en_US", "TrackingNumber": [number],
                      "Requester": "st/trackdetails", "returnToValue": ""},
                headers={
                    "X-XSRF-TOKEN": self._cookies.get("X-XSRF-TOKEN-ST", ""),
                    "Origin": "https://www.ups.com",
                    "Referer": f"https://www.ups.com/track?loc=en_US&tracknum={number}",
                    "User-Agent": self._ua or "",
                    "Content-Type": "application/json",
                },
                timeout=config.FAST_TIMEOUT,
            )
        except Exception as exc:
            raise TransientError(f"UPS did not answer: {exc}") from exc
        if resp.status_code in (401, 403):
            raise CookiesExpired()
        if resp.status_code != 200:
            raise TransientError(f"UPS answered HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise TransientError("UPS returned a non-JSON page (bot check?)") from None
        return parse(number, payload)

    def _track(self, number: str) -> TrackingResult:
        for attempt in range(2):
            stamp = self._ensure_warm()
            try:
                return self._call(number)
            except CookiesExpired:
                if attempt == 0:
                    self._ensure_warm(force=True, seen_at=stamp)
        raise TransientError("UPS kept refusing fresh session cookies")

    def track_many(self, numbers: list[str]) -> tuple[dict[str, TrackingResult], dict[str, str]]:
        numbers = list(dict.fromkeys(numbers))
        results: dict[str, TrackingResult] = {}
        transient: dict[str, str] = {}
        if not numbers:
            return results, transient
        try:
            self._ensure_warm()
        except Exception as exc:
            return {}, {n: f"UPS warm-up failed: {exc}" for n in numbers}

        def one(n: str):
            try:
                return n, self._track(n), None
            except Exception as exc:
                return n, None, str(exc)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for n, result, err in pool.map(one, numbers):
                if err:
                    transient[n] = err
                else:
                    results[n] = result
        return results, transient
