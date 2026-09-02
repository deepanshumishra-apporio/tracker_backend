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
from scrapers.base import Blocked, BaseScraper, humanize_error  # noqa: E402

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
