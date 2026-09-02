"""
Regression tests for the bulk-run failure mode seen in production: one memory
spike killed a browser and ~130 of 288 rows died with raw Selenium stack traces
that were never retried.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from selenium.common.exceptions import (
    InvalidSessionIdException,
    WebDriverException,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from models import Carrier, Status, TrackingResult  # noqa: E402
from scrapers.base import (  # noqa: E402
    BaseScraper,
    Blocked,
    PageLoadFailed,
    humanize_error,
)

# The real message UPS rows failed with, trimmed of most frames.
SELENIUM_NOISE = (
    "Message: invalid session id: session deleted as the browser has closed the "
    "connection from disconnected: Unable to receive message from renderer "
    "(Session info: chrome=152.0.7977.75); For documentation on this error, "
    "please visit: https://www.selenium.dev/documentation/webdriver/"
    "troubleshooting/errors#invalidsessionidexception Stacktrace: "
    "#0 0x5650dffa179a <unknown> #1 0x5650df904cc9 <unknown>"
)


# ---------------------------------------------------------------------------
# humanize_error — the table must never show a hex stack dump
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exc,expected",
    [
        (InvalidSessionIdException(SELENIUM_NOISE), "browser crashed"),
        (WebDriverException("Message: Active window was already closed!"), "browser crashed"),
        (
            WebDriverException(
                "HTTPConnectionPool(host='localhost', port=49863): Max retries "
                "exceeded ... Connection refused"
            ),
            "browser crashed",
        ),
        (FileExistsError(17, "File exists", "/app/downloaded_files/ad_block"), "clashed"),
        (Blocked("bot-check / CAPTCHA wall detected"), "bot-check"),
        (TimeoutError("timed out waiting for page"), "didn't finish loading"),
    ],
)
def test_known_failures_become_readable(exc, expected):
    message = humanize_error(exc)
    assert expected in message
    assert "Stacktrace" not in message
    assert "0x" not in message


def test_stack_traces_are_stripped_and_capped():
    message = humanize_error(InvalidSessionIdException(SELENIUM_NOISE))
    assert len(message) <= 200


def test_unrecognized_error_keeps_its_first_line_only():
    message = humanize_error(RuntimeError("something odd\nStacktrace:\n#0 0xdead <unknown>"))
    assert message == "something odd"


def test_unrecognized_error_is_truncated():
    message = humanize_error(RuntimeError("x" * 500))
    assert len(message) <= 200
    assert message.endswith("…")


def test_empty_error_still_says_something():
    assert humanize_error(RuntimeError("")) == "RuntimeError"


# ---------------------------------------------------------------------------
# Retry policy — a crashed browser must not kill the row.
# These drive the REAL decorated BaseScraper.scrape with a fake browser, so the
# retry wiring itself is under test rather than a copy of it.
# ---------------------------------------------------------------------------
class _FakeBrowser:
    """Stands in for a SeleniumBase SB() session."""

    def __init__(self):
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.exited = True
        return False

    def uc_open_with_reconnect(self, *a, **k):
        pass

    def uc_gui_click_captcha(self):
        pass

    def get_title(self):
        return "Tracking | UPS"

    def get_page_source(self):
        return "<html>tracking details</html>"

    def execute_script(self, script):
        # What the load guard asks for: the URL we actually ended up on.
        return "https://www.ups.com/track?tracknum=1ZH40B480424822345"


class _Scraper(BaseScraper):
    carrier = Carrier.UPS

    def build_url(self, tracking_number: str) -> str:
        return f"https://example.invalid/{tracking_number}"

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        return TrackingResult(
            tracking_number=tracking_number,
            carrier=self.carrier,
            status=Status.DELIVERED,
        )


@pytest.fixture()
def no_backoff(monkeypatch):
    """Strip tenacity's exponential wait so tests don't sleep for a minute."""
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda *_: None)


@pytest.fixture()
def browsers(monkeypatch):
    """Install a fake SB() factory; returns a list of the sessions it handed out."""

    def install(fail_times: int, exc: BaseException):
        made: list[_FakeBrowser] = []
        calls = {"n": 0}

        def factory(**kwargs):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise exc
            browser = _FakeBrowser()
            made.append(browser)
            return browser

        monkeypatch.setattr("scrapers.base.SB", factory)
        return calls, made

    return install


TRANSIENT = [
    InvalidSessionIdException(SELENIUM_NOISE),
    WebDriverException("Message: Active window was already closed!"),
    WebDriverException(
        "HTTPConnectionPool(host='localhost', port=49863): Connection refused"
    ),
    FileExistsError(17, "File exists", "/app/downloaded_files/ad_block"),
    Blocked("bot-check / CAPTCHA wall detected"),
]


@pytest.mark.parametrize("exc", TRANSIENT, ids=lambda e: type(e).__name__)
def test_a_crashed_browser_is_retried_and_the_row_recovers(exc, browsers, no_backoff):
    calls, _ = browsers(fail_times=1, exc=exc)

    result = _Scraper().scrape("1ZH40B480424822345")

    assert calls["n"] == 2, "the crash should have been retried on a fresh browser"
    assert result.ok is True
    assert result.status is Status.DELIVERED


def test_retry_gives_up_after_max_attempts(browsers, no_backoff):
    calls, _ = browsers(fail_times=99, exc=WebDriverException("invalid session id"))

    with pytest.raises(WebDriverException):
        _Scraper().scrape("1ZH40B480424822345")

    assert calls["n"] == config.MAX_RETRIES


def test_a_successful_scrape_launches_exactly_one_browser(browsers, no_backoff):
    calls, made = browsers(fail_times=0, exc=RuntimeError("unused"))

    _Scraper().scrape("1ZH40B480424822345")

    assert calls["n"] == 1
    assert made[0].exited is True, "the browser must be torn down"


def test_the_browser_is_torn_down_even_when_the_scrape_raises(browsers, no_backoff):
    """A leaked Chrome keeps its memory — which is what starved the next row."""

    class _Boom(_Scraper):
        def parse_dom(self, sb, tracking_number):
            raise WebDriverException("invalid session id")

    calls, made = browsers(fail_times=0, exc=RuntimeError("unused"))

    with pytest.raises(WebDriverException):
        _Boom().scrape("1ZH40B480424822345")

    assert calls["n"] == config.MAX_RETRIES
    assert made, "no browser was created"
    assert all(b.exited for b in made), "a crashed scrape leaked its browser"


def test_a_not_found_number_is_not_retried(browsers, no_backoff):
    """Not-found is returned, never raised — retrying it would waste a browser."""

    class _NotFound(_Scraper):
        def parse_dom(self, sb, tracking_number):
            return TrackingResult.failure(tracking_number, self.carrier, "no status found")

    calls, _ = browsers(fail_times=0, exc=RuntimeError("unused"))

    result = _NotFound().scrape("55124671")

    assert result.ok is False
    assert calls["n"] == 1, "a genuine not-found must not burn retries"


# ---------------------------------------------------------------------------
# A page that never loaded must not be reported as a bad tracking number.
# The 2026-09 bulk run failed 11 of 12 UPS rows with "no status found (invalid
# number or page changed)" while the proxy was refusing every connection —
# Chrome was serving its own error page and we parsed it as if it were the
# carrier's. The same numbers tracked fine with USE_PROXIES=false.
# ---------------------------------------------------------------------------
class _DeadPageBrowser(_FakeBrowser):
    """A browser whose navigation lands on Chrome's network-error page."""

    def execute_script(self, script):
        return "chrome-error://chromewebdata/"

    def get_title(self):
        return ""

    def get_page_source(self):
        return ""


@pytest.fixture()
def dead_pages(monkeypatch):
    """Install a fake SB() that always fails to load; returns the call counter."""
    calls = {"n": 0}

    def factory(**kwargs):
        calls["n"] += 1
        return _DeadPageBrowser()

    monkeypatch.setattr("scrapers.base.SB", factory)
    return calls


@pytest.fixture()
def proxied(monkeypatch):
    monkeypatch.setattr(config, "USE_PROXIES", True)
    monkeypatch.setattr(config, "PROXY_URL", "user:pass@gate.example:7000")


@pytest.fixture()
def unproxied(monkeypatch):
    monkeypatch.setattr(config, "USE_PROXIES", False)
    monkeypatch.setattr(config, "PROXY_URL", "")


def test_a_page_that_never_loaded_is_not_blamed_on_the_number(
    dead_pages, proxied, no_backoff
):
    with pytest.raises(PageLoadFailed) as caught:
        _Scraper().scrape("1ZH40B480424822345")

    message = str(caught.value)
    assert "never loaded" in message
    assert "invalid" not in message.lower(), "must not blame the waybill"
    assert dead_pages["n"] == config.MAX_RETRIES, "a dead load deserves a retry"


def test_the_failure_names_the_proxy_when_one_is_configured(
    dead_pages, proxied, no_backoff
):
    with pytest.raises(PageLoadFailed) as caught:
        _Scraper().scrape("1ZH40B480424822345")

    message = str(caught.value)
    assert "proxy" in message
    assert "USE_PROXIES=false" in message, "say which setting to change"


def test_without_a_proxy_it_points_at_the_connection(
    dead_pages, unproxied, no_backoff
):
    with pytest.raises(PageLoadFailed) as caught:
        _Scraper().scrape("1ZH40B480424822345")

    assert "internet connection" in str(caught.value)
    assert "proxy" not in str(caught.value)


def test_the_table_shows_the_load_failure_verbatim(proxied):
    """humanize_error must keep the sentence that names the fix."""
    message = humanize_error(PageLoadFailed(_Scraper()._load_failure_message()))
    assert "never loaded" in message
    assert "USE_PROXIES=false" in message
    assert len(message) <= 200


def test_a_loaded_page_is_left_alone(browsers, no_backoff):
    """The guard must not fire on a real page — that would fail every row."""
    calls, _ = browsers(fail_times=0, exc=RuntimeError("unused"))

    result = _Scraper().scrape("1ZH40B480424822345")

    assert result.ok is True
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Browser startup is serialized
# ---------------------------------------------------------------------------
def test_browser_startup_lock_serializes_launches():
    """Two threads must not be inside the launch section at once.

    The ad_block "[Errno 17] File exists" failure was exactly this race.
    """
    from scrapers import base

    overlap = []
    inside = []

    def launch():
        with base._BROWSER_START_LOCK:
            inside.append(1)
            overlap.append(len(inside))
            time.sleep(0.02)
            inside.pop()

    threads = [threading.Thread(target=launch) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap, "the lock body never ran"
    assert max(overlap) == 1, "two launches overlapped — the ad_block race is back"


# ---------------------------------------------------------------------------
# Concurrency default
# ---------------------------------------------------------------------------
def test_batch_concurrency_defaults_to_one_browser():
    """Two concurrent Chromes is what OOM-killed the production run."""
    import batch

    assert config.BATCH_CONCURRENCY >= 1
    assert batch.DEFAULT_CONCURRENCY == config.BATCH_CONCURRENCY


def test_batch_concurrency_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("BATCH_CONCURRENCY", "4")
    import importlib

    reloaded = importlib.reload(config)
    try:
        assert reloaded.BATCH_CONCURRENCY == 4
    finally:
        monkeypatch.delenv("BATCH_CONCURRENCY", raising=False)
        importlib.reload(config)


def test_batch_concurrency_never_drops_below_one(monkeypatch):
    monkeypatch.setenv("BATCH_CONCURRENCY", "0")
    import importlib

    reloaded = importlib.reload(config)
    try:
        assert reloaded.BATCH_CONCURRENCY == 1
    finally:
        monkeypatch.delenv("BATCH_CONCURRENCY", raising=False)
        importlib.reload(config)


# ---------------------------------------------------------------------------
# Browser reuse — the speed fix. Chrome startup was ~10-15s of every ~40s row.
# ---------------------------------------------------------------------------
def test_one_browser_serves_many_numbers(browsers, no_backoff):
    """The whole point: 20 lookups must not cost 20 Chrome launches."""
    calls, made = browsers(fail_times=0, exc=RuntimeError("unused"))
    scraper = _Scraper()

    with scraper.session(max_lookups=100) as s:
        for i in range(20):
            assert s.track(f"1Z{i}").ok is True

    assert calls["n"] == 1, "each lookup launched its own browser again"
    assert s.launches == 1
    assert made[0].exited is True, "the session must close its browser on exit"


def test_the_browser_is_recycled_after_the_reuse_budget(browsers, no_backoff):
    """Riding one fingerprint forever is what anti-bot systems look for."""
    calls, _ = browsers(fail_times=0, exc=RuntimeError("unused"))
    scraper = _Scraper()

    with scraper.session(max_lookups=5) as s:
        for i in range(12):
            s.track(f"1Z{i}")

    assert calls["n"] == 3, "expected a fresh browser every 5 lookups"


def test_a_crash_mid_session_relaunches_and_keeps_going(no_backoff, monkeypatch):
    """One dead browser must cost one row a retry, not the rest of the run."""
    launches = {"n": 0}
    made: list[_FakeBrowser] = []

    def factory(**kwargs):
        launches["n"] += 1
        b = _FakeBrowser()
        made.append(b)
        return b

    monkeypatch.setattr("scrapers.base.SB", factory)

    class _DiesOnce(_Scraper):
        seen = 0

        def parse_dom(self, sb, tracking_number):
            _DiesOnce.seen += 1
            if _DiesOnce.seen == 3:
                raise InvalidSessionIdException(SELENIUM_NOISE)
            return super().parse_dom(sb, tracking_number)

    scraper = _DiesOnce()
    with scraper.session(max_lookups=100) as s:
        results = [s.track(f"1Z{i}") for i in range(5)]

    assert all(r.ok for r in results), "a mid-run crash lost a row"
    assert launches["n"] == 2, "the dead browser should have been replaced once"
    assert all(b.exited for b in made), "the crashed browser leaked"


def test_session_closes_its_browser_even_when_the_body_raises(browsers, no_backoff):
    calls, made = browsers(fail_times=0, exc=RuntimeError("unused"))
    scraper = _Scraper()

    with pytest.raises(ValueError):
        with scraper.session() as s:
            s.track("1Z1")
            raise ValueError("caller blew up")

    assert made[0].exited is True


def test_single_lookup_still_opens_and_closes_one_browser(browsers, no_backoff):
    """scrape() is now a one-shot session; the single-shipment path is unchanged."""
    calls, made = browsers(fail_times=0, exc=RuntimeError("unused"))

    result = _Scraper().scrape("1ZH40B480424822345")

    assert result.status is Status.DELIVERED
    assert calls["n"] == 1
    assert made[0].exited is True


def test_a_carrier_without_a_browser_is_delegated(no_backoff, monkeypatch):
    """FedEx over Scrape.do is a plain HTTP fetch — nothing to reuse."""

    class _NoBrowser(_Scraper):
        carrier = Carrier.FEDEX
        scraped: list[str] = []

        @property
        def uses_shared_browser(self):
            return False

        def scrape(self, tracking_number):
            _NoBrowser.scraped.append(tracking_number)
            return TrackingResult(
                tracking_number=tracking_number,
                carrier=self.carrier,
                status=Status.DELIVERED,
            )

    def boom(**kwargs):  # a browser must never be launched
        raise AssertionError("launched a browser for a non-browser carrier")

    monkeypatch.setattr("scrapers.base.SB", boom)

    scraper = _NoBrowser()
    with scraper.session() as s:
        assert s.track("873815709010").ok is True
    assert _NoBrowser.scraped == ["873815709010"]
