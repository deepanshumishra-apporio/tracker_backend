"""
The always-on browser pool: N browsers that stay open and work FedEx/DHL
numbers off one queue in parallel, recycled every SESSION_MAX_LOOKUPS.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import index  # noqa: E402
import tracking_service  # noqa: E402
from browser_pool import BrowserPool  # noqa: E402
from models import Carrier, Status, TrackingResult  # noqa: E402
from scrapers.base import BaseScraper  # noqa: E402


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch):
    monkeypatch.setattr(config, "MIN_DELAY", 0.0)
    monkeypatch.setattr(config, "MAX_DELAY", 0.0)


class _Session:
    def __init__(self, scraper: "_Fake"):
        self.scraper = scraper
        self.closed = False

    def track(self, number: str) -> TrackingResult:
        return self.scraper.lookup(number)

    def close(self) -> None:
        self.closed = True

    # The non-pool path (index.browser_session) opens it with `with`.
    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False


class _Fake:
    """A scraper whose 'browser' is a session object; counts what it's asked."""

    def __init__(self, carrier: Carrier, delay: float = 0.0, boom: set[str] = frozenset()):
        self.carrier, self.delay, self.boom = carrier, delay, boom
        self.sessions: list[_Session] = []
        self.looked_up: list[str] = []
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def session(self) -> _Session:
        s = _Session(self)
        self.sessions.append(s)
        return s

    def lookup(self, number: str) -> TrackingResult:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.looked_up.append(number)
        try:
            time.sleep(self.delay)
            if number in self.boom:
                raise RuntimeError("page exploded")
            return TrackingResult(tracking_number=number, carrier=self.carrier,
                                  status=Status.IN_TRANSIT)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture()
def pool():
    made: list[BrowserPool] = []

    def make(scrapers, size=4, carriers=(Carrier.FEDEX, Carrier.DHL)):
        p = BrowserPool(scrapers, size=size, carriers=carriers)
        p.start()
        made.append(p)
        return p

    yield make
    for p in made:
        p.stop()


def _results(futures, timeout=10):
    return [f.result(timeout=timeout) for f in futures]


def test_four_browsers_work_the_queue_side_by_side(pool):
    fedex = _Fake(Carrier.FEDEX, delay=0.2)
    p = pool({Carrier.FEDEX: fedex}, size=4)
    start = time.monotonic()
    out = _results([p.submit(Carrier.FEDEX, f"F{i}") for i in range(8)])
    took = time.monotonic() - start

    assert all(r.ok for r in out)
    assert fedex.peak == 4, "expected four lookups in flight at once"
    assert took < 0.2 * 8 / 2, "eight 0.2s lookups on four browsers ran nearly serially"


def test_browsers_stay_open_between_runs(pool):
    """A second batch reuses the open browsers instead of launching new ones."""
    fedex = _Fake(Carrier.FEDEX, delay=0.05)
    p = pool({Carrier.FEDEX: fedex}, size=2)
    _results([p.submit(Carrier.FEDEX, f"A{i}") for i in range(6)])
    opened = len(fedex.sessions)
    _results([p.submit(Carrier.FEDEX, f"B{i}") for i in range(6)])

    assert opened <= 2
    assert len(fedex.sessions) == opened, "the second run opened new browsers"
    assert not any(s.closed for s in fedex.sessions), "an idle browser was closed"


def test_stop_closes_every_browser(pool):
    fedex = _Fake(Carrier.FEDEX)
    p = pool({Carrier.FEDEX: fedex}, size=2)
    _results([p.submit(Carrier.FEDEX, f"F{i}") for i in range(4)])
    p.stop()
    assert fedex.sessions and all(s.closed for s in fedex.sessions)


def test_a_single_lookup_jumps_the_backlog(pool):
    fedex = _Fake(Carrier.FEDEX, delay=0.05)
    p = pool({Carrier.FEDEX: fedex}, size=1)
    backlog = [p.submit(Carrier.FEDEX, f"F{i}") for i in range(10)]
    urgent = p.submit(Carrier.FEDEX, "URGENT", priority=True)
    urgent.result(timeout=10)
    _results(backlog)

    assert fedex.looked_up.index("URGENT") <= 1, fedex.looked_up


def test_a_backlog_doesnt_starve_the_other_carrier(pool):
    fedex = _Fake(Carrier.FEDEX, delay=0.1)
    dhl = _Fake(Carrier.DHL, delay=0.01)
    p = pool({Carrier.FEDEX: fedex, Carrier.DHL: dhl}, size=2)
    backlog = [p.submit(Carrier.FEDEX, f"F{i}") for i in range(20)]
    time.sleep(0.15)   # both workers are on FedEx now
    p.submit(Carrier.DHL, "D1").result(timeout=10)

    assert sum(f.done() for f in backlog) < 10, "DHL waited for most of the FedEx backlog"
    _results(backlog)


def test_a_number_already_queued_is_looked_up_once(pool):
    fedex = _Fake(Carrier.FEDEX, delay=0.05)
    p = pool({Carrier.FEDEX: fedex}, size=1)
    first = [p.submit(Carrier.FEDEX, f"F{i}") for i in range(3)]
    again = p.submit(Carrier.FEDEX, "F2")
    _results(first + [again])

    assert again is first[2]
    assert fedex.looked_up.count("F2") == 1


def test_a_failed_lookup_doesnt_kill_its_worker(pool):
    fedex = _Fake(Carrier.FEDEX, boom={"BAD"})
    p = pool({Carrier.FEDEX: fedex}, size=1)
    bad = p.submit(Carrier.FEDEX, "BAD")
    good = p.submit(Carrier.FEDEX, "GOOD")

    with pytest.raises(RuntimeError):
        bad.result(timeout=10)
    assert good.result(timeout=10).ok


def test_each_browser_paces_its_own_lookups(pool, monkeypatch):
    monkeypatch.setattr(config, "MIN_DELAY", 0.2)
    monkeypatch.setattr(config, "MAX_DELAY", 0.2)
    fedex = _Fake(Carrier.FEDEX)
    p = pool({Carrier.FEDEX: fedex}, size=1)
    start = time.monotonic()
    _results([p.submit(Carrier.FEDEX, f"F{i}") for i in range(3)])

    assert time.monotonic() - start >= 0.4, "two gaps of 0.2s expected between three lookups"


def test_cancelled_numbers_are_dropped_from_the_queue(pool):
    fedex = _Fake(Carrier.FEDEX, delay=0.05)
    p = pool({Carrier.FEDEX: fedex}, size=1)
    futures = [p.submit(Carrier.FEDEX, f"F{i}") for i in range(20)]
    time.sleep(0.02)
    p.cancel(futures)
    time.sleep(0.2)

    assert len(fedex.looked_up) <= 3
    assert p.status()["queued"]["fedex"] == 0


# ---------------------------------------------------------------------------
# A real BrowserSession underneath: recycled every SESSION_MAX_LOOKUPS
# ---------------------------------------------------------------------------
class _FakeSB:
    def __init__(self, launches: list):
        launches.append(self)
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def uc_open_with_reconnect(self, *a, **k):
        pass

    def get_title(self):
        return "Track"

    def get_page_source(self):
        return "<html>tracking details</html>"

    def execute_script(self, script):
        return "https://www.fedex.com/fedextrack/?trknbr=1"


class _BrowserFedEx(BaseScraper):
    carrier = Carrier.FEDEX
    uses_shared_browser = True

    def build_url(self, tracking_number: str) -> str:
        return f"https://example.invalid/{tracking_number}"

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        return TrackingResult(tracking_number=tracking_number, carrier=self.carrier,
                              status=Status.DELIVERED)


def test_the_browser_is_recycled_every_session_max_lookups(pool, monkeypatch):
    launches: list[_FakeSB] = []
    monkeypatch.setattr("scrapers.base.SB", lambda **kw: _FakeSB(launches))
    monkeypatch.setattr(config, "SESSION_MAX_LOOKUPS", 25)
    p = pool({Carrier.FEDEX: _BrowserFedEx()}, size=1)
    _results([p.submit(Carrier.FEDEX, f"F{i}") for i in range(60)])

    assert len(launches) == 3, "60 lookups at 25 per browser = 3 browsers"
    assert [b.exited for b in launches] == [True, True, False], \
        "the current browser must stay open after the work runs out"
    p.stop()
    assert launches[-1].exited


# ---------------------------------------------------------------------------
# Wired into the app
# ---------------------------------------------------------------------------
def test_the_app_runs_fedex_through_the_pool_and_ups_does_not(monkeypatch):
    fedex = _Fake(Carrier.FEDEX)
    ups = _Fake(Carrier.UPS)
    monkeypatch.setitem(index.SCRAPERS, Carrier.FEDEX, fedex)
    monkeypatch.setitem(index.SCRAPERS, Carrier.UPS, ups)
    monkeypatch.setattr(config, "BROWSER_POOL_SIZE", 2)

    with TestClient(index.create_app()) as c:
        health = c.get("/api/health").json()
        assert health["browser_pool"]["browsers"] == 2
        assert health["browser_pool"]["carriers"] == ["dhl", "fedex"]
        assert tracking_service.POOL is not None

        assert c.get("/api/track", params={"carrier": "fedex", "tracking_number": "F1"}).json()["ok"]
        assert c.get("/api/track", params={"carrier": "ups", "tracking_number": "1Z1"}).json()["ok"]
        assert c.get("/api/health").json()["browser_pool"]["lookups"] == 1, \
            "UPS must not go through the FedEx/DHL browsers"

    assert tracking_service.POOL is None
    assert all(s.closed for s in fedex.sessions)


def test_bulk_rows_come_back_as_each_finishes(monkeypatch):
    """track_many reports pooled rows one by one, not all at the end."""
    fedex = _Fake(Carrier.FEDEX, delay=0.05)
    p = BrowserPool({Carrier.FEDEX: fedex}, size=2, carriers=[Carrier.FEDEX])
    p.start()
    monkeypatch.setattr(tracking_service, "POOL", p)
    seen: list[float] = []
    try:
        out = tracking_service.track_many(
            [(Carrier.FEDEX, f"F{i}") for i in range(6)], index.browser_session,
            on_result=lambda *_: seen.append(time.monotonic()), use_cache=False)
    finally:
        p.stop()

    assert len(out) == 6 and all(r.ok for r in out.values())
    assert seen[-1] - seen[0] > 0.05, "every row was reported in one burst at the end"
