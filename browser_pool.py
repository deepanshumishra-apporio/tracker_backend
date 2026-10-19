"""
Always-on browser workers for the carriers that need a real browser.

FedEx and DHL can't be fetched over plain HTTP (FedEx's Akamai blocks it, DHL
wants a fresh proof-of-work per call), so each lookup drives Chrome. Opening
Chrome is ~10-15s of a ~40s lookup, and a run that opens one browser, works
through its rows and closes again leaves the next run to pay that again — and
does only one page at a time.

The pool keeps N workers alive for the life of the app. Each owns one browser
that stays open between lookups and pulls the next number from a shared queue
the moment it is free, so bulk jobs, the background refresher and single
lookups all feed the same browsers:

  * A browser is recycled every SESSION_MAX_LOOKUPS numbers (BrowserSession
    does that), so no fingerprint is ridden for hundreds of lookups.
  * Each browser waits MIN_DELAY-MAX_DELAY seconds between its own lookups.
  * A worker sticks to the carrier its browser is open for, but a carrier with
    work and no browser pulls a free worker over, so a 1,000-row FedEx backlog
    doesn't starve DHL.
  * Single lookups (priority) go ahead of any backlog.
  * A number already queued is not queued twice; both callers share the answer.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from concurrent.futures import Future
from typing import Any, Iterable, Mapping, Optional

import config
from models import Carrier, TrackingResult

log = logging.getLogger("tracker")

Key = tuple[Carrier, str]


class BrowserPool:
    def __init__(self, scrapers: Mapping[Carrier, Any], size: int, carriers: Iterable[Carrier]):
        # The mapping is read at lookup time, so a scraper swapped into it later
        # (tests) is picked up by the next browser opened.
        self.scrapers = scrapers
        self.size = max(0, size)
        self.carriers = frozenset(carriers)
        self._cond = threading.Condition()
        self._queues: dict[Carrier, deque[str]] = {c: deque() for c in self.carriers}
        self._urgent: deque[Key] = deque()
        self._pending: dict[Key, Future] = {}
        self._serving: dict[Carrier, int] = {c: 0 for c in self.carriers}
        self._busy = 0
        self._lookups = 0
        self._threads: list[threading.Thread] = []
        self._stopped = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        with self._cond:
            if self._threads or not self.size or not self.carriers:
                return
            self._stopped = False
            for i in range(self.size):
                t = threading.Thread(target=self._work, name=f"browser-{i + 1}", daemon=True)
                self._threads.append(t)
                t.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel everything queued and let the workers close their browsers.
        A lookup in progress finishes first; this waits up to `timeout` for it."""
        with self._cond:
            self._stopped = True
            for fut in self._pending.values():
                fut.cancel()
            self._pending.clear()
            self._urgent.clear()
            for q in self._queues.values():
                q.clear()
            self._cond.notify_all()
            threads, self._threads = self._threads, []
        deadline = time.monotonic() + timeout
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))

    def handles(self, carrier: Carrier) -> bool:
        return carrier in self.carriers and bool(self._threads) and not self._stopped

    # -- queue -------------------------------------------------------------
    def submit(self, carrier: Carrier, number: str, *, priority: bool = False) -> Future:
        """Queue one lookup; the Future resolves to its TrackingResult (or the
        exception the browser raised)."""
        key = (carrier, number)
        with self._cond:
            fut = self._pending.get(key)
            if fut is None:
                fut = Future()
                if self._stopped:
                    fut.cancel()
                    return fut
                self._pending[key] = fut
                if priority:
                    self._urgent.append(key)
                else:
                    self._queues[carrier].append(number)
                self._cond.notify()
            elif priority and number in self._queues[carrier]:
                # Already waiting in the backlog: move it up front.
                self._queues[carrier].remove(number)
                self._urgent.append(key)
            return fut

    def cancel(self, futures: Iterable[Future]) -> None:
        """Drop lookups that haven't started. Running ones finish regardless."""
        wanted = set(futures)
        with self._cond:
            for key, fut in list(self._pending.items()):
                if fut in wanted and fut.cancel():
                    del self._pending[key]
                    carrier, number = key
                    if key in self._urgent:
                        self._urgent.remove(key)
                    elif number in self._queues[carrier]:
                        self._queues[carrier].remove(number)

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {
                "browsers": len(self._threads),
                "carriers": sorted(c.value for c in self.carriers),
                "busy": self._busy,
                "queued": {c.value: len(q) for c, q in self._queues.items()}
                | {"priority": len(self._urgent)},
                "lookups": self._lookups,
            }

    def _next(self, current: Optional[Carrier]) -> Optional[Key]:
        """Pick the next lookup for a worker whose browser is open for
        `current`. Caller holds the lock."""
        if self._urgent:
            return self._urgent.popleft()
        waiting = [c for c, q in self._queues.items() if q]
        if not waiting:
            return None
        unserved = [c for c in waiting if self._serving[c] == 0]
        if current in waiting and not (unserved and self._serving[current] > 1):
            carrier = current
        else:
            carrier = max(unserved or waiting, key=lambda c: len(self._queues[c]))
        return carrier, self._queues[carrier].popleft()

    # -- worker ------------------------------------------------------------
    def _work(self) -> None:
        carrier: Optional[Carrier] = None
        scraper: Any = None
        session: Any = None
        last_done = 0.0
        try:
            while True:
                with self._cond:
                    key = self._next(carrier)
                    while key is None and not self._stopped:
                        self._cond.wait()
                        key = self._next(carrier)
                    if self._stopped:
                        return
                    fut = self._pending[key]
                    if not fut.set_running_or_notify_cancel():
                        self._pending.pop(key, None)
                        continue
                    if key[0] != carrier:
                        if carrier is not None:
                            self._serving[carrier] -= 1
                        carrier = key[0]
                        self._serving[carrier] += 1
                    self._busy += 1

                number = key[1]
                try:
                    current = self.scrapers[carrier]
                    if session is not None and current is not scraper:
                        # Switched carrier: free this browser before opening the next.
                        session.close()
                        session = None
                    scraper = current
                    if session is None and hasattr(scraper, "session"):
                        session = scraper.session()
                    elif session is not None and last_done:
                        # Same browser as last time: pace it like a person would.
                        gap = last_done + random.uniform(config.MIN_DELAY, config.MAX_DELAY)
                        time.sleep(max(0.0, gap - time.monotonic()))
                    result: TrackingResult = (session.track(number) if session is not None
                                              else scraper.scrape(number))
                except BaseException as exc:   # handed to the caller, never kills the worker
                    outcome: tuple[Optional[TrackingResult], Optional[BaseException]] = (None, exc)
                else:
                    outcome = (result, None)
                last_done = time.monotonic()
                with self._cond:
                    self._pending.pop(key, None)
                    self._busy -= 1
                    self._lookups += 1
                if outcome[1] is not None:
                    fut.set_exception(outcome[1])
                else:
                    fut.set_result(outcome[0])
        finally:
            with self._cond:
                if carrier is not None:
                    self._serving[carrier] -= 1
            if session is not None:
                session.close()
