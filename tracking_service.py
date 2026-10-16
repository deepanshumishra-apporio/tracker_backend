"""
One entry point for tracking: cache -> fast HTTP fetcher -> browser scraper.

  * Cache: every result is saved to SQLite. A lookup whose stored result is
    younger than its status's max age (config.CACHE_MAX_AGE) is answered from
    the database in milliseconds without touching the carrier.
  * Fast path: carriers in FAST (Aramex, UPS) are fetched over plain HTTPS,
    many numbers in parallel.
  * Fallback: anything the fast path could not get an answer for (blocked,
    timeout, no fast fetcher for that carrier) goes through the browser
    scrapers, one shared browser per carrier.

The background Refresher keeps undelivered shipments current so that most
lookups are cache hits.
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import config
import storage
from models import Carrier, TrackingResult
from scrapers.base import humanize_error

log = logging.getLogger("tracker")

Key = tuple[Carrier, str]
# carrier -> context manager yielding track(number) -> TrackingResult (browser)
SessionFactory = Callable[[Carrier], AbstractContextManager]
OnResult = Callable[[Carrier, str, TrackingResult, bool], None]


def _build_fast() -> dict[Carrier, Any]:
    if not config.FAST_ENABLED:
        return {}
    from fast.aramex import AramexFast
    from fast.ups import UPSFast
    return {Carrier.ARAMEX: AramexFast(), Carrier.UPS: UPSFast()}


# Tests replace this with fakes (or {} to force the browser path).
FAST: dict[Carrier, Any] = _build_fast()

_db_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
def _age(scraped_at: Optional[str]) -> float:
    if not scraped_at:
        return float("inf")
    try:
        when = datetime.fromisoformat(scraped_at)
        return (datetime.now(timezone.utc) - when).total_seconds()
    except (ValueError, TypeError):
        return float("inf")


def cached(carrier: Carrier, number: str) -> Optional[TrackingResult]:
    """The stored result if it is still fresh enough to serve, else None."""
    storage.ensure_db()
    row = storage.get_shipment(carrier.value, number)
    if not row:
        return None
    key = row["status"] if row["ok"] else "unknown"
    if _age(row["scraped_at"]) >= config.CACHE_MAX_AGE.get(key, 0):
        return None
    return TrackingResult(**row)


def _store(result: TrackingResult) -> TrackingResult:
    """Save a fresh result — unless it is a failure and we already hold good
    data, in which case keep (and return) the good data."""
    if result.scraped_at is None:
        result.scraped_at = datetime.now(timezone.utc)
    with _db_lock:
        storage.ensure_db()
        if not result.ok:
            prev = storage.get_shipment(result.carrier.value, result.tracking_number)
            if prev and prev["ok"]:
                storage.touch(result.carrier.value, result.tracking_number)
                kept = TrackingResult(**prev)
                kept.details = {**kept.details,
                                "Last refresh failed": result.error or "unknown error"}
                return kept
        storage.save(result)
    return result


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
def _browser(carrier: Carrier, numbers: list[str], session_factory: SessionFactory,
             cancelled: Callable[[], bool]) -> dict[str, TrackingResult]:
    out: dict[str, TrackingResult] = {}
    try:
        with session_factory(carrier) as track:
            for n in numbers:
                if cancelled():
                    break
                try:
                    out[n] = track(n)
                except Exception as exc:
                    out[n] = TrackingResult.failure(n, carrier, humanize_error(exc))
    except Exception as exc:  # the browser itself would not start
        msg = humanize_error(exc)
        for n in numbers:
            out.setdefault(n, TrackingResult.failure(n, carrier, msg))
    return out


def track_many(
    items: list[Key],
    session_factory: SessionFactory,
    *,
    on_result: Optional[OnResult] = None,
    cancelled: Callable[[], bool] = lambda: False,
    use_cache: bool = True,
    fallback: bool = True,
) -> dict[Key, TrackingResult]:
    """Track every (carrier, number). Results are saved as they arrive and
    reported through on_result(carrier, number, result, from_cache)."""
    results: dict[Key, TrackingResult] = {}
    lock = threading.Lock()

    def emit(carrier: Carrier, number: str, result: TrackingResult, from_cache: bool) -> None:
        if not from_cache:
            result = _store(result)
        with lock:
            results[(carrier, number)] = result
        if on_result:
            on_result(carrier, number, result, from_cache)

    groups: dict[Carrier, list[str]] = defaultdict(list)
    for carrier, number in dict.fromkeys(items):
        hit = cached(carrier, number) if use_cache else None
        if hit is not None:
            emit(carrier, number, hit, True)
        else:
            groups[carrier].append(number)

    def run(carrier: Carrier, numbers: list[str]) -> None:
        leftover = numbers
        fetcher = FAST.get(carrier)
        if fetcher is not None and not cancelled():
            try:
                got, transient = fetcher.track_many(numbers)
            except Exception as exc:   # a fetcher bug must not lose the rows
                log.exception("fast %s failed", carrier.value)
                got, transient = {}, {n: humanize_error(exc) for n in numbers}
            for n, r in got.items():
                emit(carrier, n, r, False)
            leftover = [n for n in numbers if n in transient or n not in got]
            if leftover:
                log.info("fast %s: %d/%d need the browser", carrier.value,
                         len(leftover), len(numbers))
        if leftover and fallback and not cancelled():
            for n, r in _browser(carrier, leftover, session_factory, cancelled).items():
                emit(carrier, n, r, False)

    # Carriers run side by side: Aramex's HTTP calls don't wait behind a UPS
    # browser warm-up, nor DHL's browser behind either.
    with ThreadPoolExecutor(max_workers=max(1, len(groups))) as pool:
        list(pool.map(lambda kv: run(*kv), groups.items()))
    return results


def track_one(carrier: Carrier, number: str, session_factory: SessionFactory,
              *, use_cache: bool = True) -> tuple[TrackingResult, bool]:
    """-> (result, from_cache)."""
    if use_cache:
        hit = cached(carrier, number)
        if hit is not None:
            return hit, True
    out = track_many([(carrier, number)], session_factory, use_cache=False)
    return out[(carrier, number)], False


# ---------------------------------------------------------------------------
# Background refresh
# ---------------------------------------------------------------------------
class Refresher:
    """Re-tracks stale, undelivered shipments every REFRESH_INTERVAL seconds,
    so lookups and searches hit a warm cache instead of the carriers."""

    def __init__(self, session_factory: SessionFactory):
        self.session_factory = session_factory
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_sweep: dict[str, Any] = {}

    def sweep(self) -> dict[str, Any]:
        storage.ensure_db()
        due = storage.stale_shipments(config.CACHE_MAX_AGE, config.REFRESH_BATCH)
        started = datetime.now(timezone.utc)
        out = track_many([(Carrier(c), n) for c, n in due], self.session_factory,
                         use_cache=False, cancelled=self._stop.is_set)
        self.last_sweep = {
            "at": started.isoformat(),
            "checked": len(due),
            "ok": sum(1 for r in out.values() if r.ok),
            "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        }
        return self.last_sweep

    def _loop(self) -> None:
        while not self._stop.wait(config.REFRESH_INTERVAL):
            try:
                self.sweep()
            except Exception:
                log.exception("refresh sweep failed")

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="refresher", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
