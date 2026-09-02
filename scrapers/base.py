"""
BaseScraper — shared machinery, now powered by SeleniumBase UC Mode.

Why SeleniumBase UC Mode (vs plain Selenium/Playwright stealth):
  * Renames the CDP console vars anti-bot scripts scan for
  * Launches Chrome first, THEN attaches chromedriver (automation flag unseen)
  * Disconnects chromedriver during page loads/clicks (looks human at inspection)
  * uc_gui_click_captcha() auto-clicks Cloudflare Turnstile / reCAPTCHA
This is what actually gets you past UPS/FedEx/DHL/Aramex bot walls.

Each carrier subclass only decides:
  * build_url()   -> the public tracking page
  * api_url()     -> (optional) the internal JSON endpoint; if given, we open it
                     INSIDE the already-authenticated browser session and read JSON
  * parse_json()  -> map that JSON to a normalized TrackingResult
  * parse_dom()   -> fallback: extract from the rendered page
"""
from __future__ import annotations

import json
import random
import re
import sys
import threading
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from selenium.common.exceptions import WebDriverException
from seleniumbase import SB
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
from models import Carrier, TrackingResult


class Blocked(Exception):
    """Raised when we detect a block/CAPTCHA wall so tenacity retries (new IP)."""


class PageLoadFailed(Exception):
    """Raised when the navigation never reached the carrier at all.

    Chrome answers a failed connection with its own error page, whose document
    is empty. Parsing on regardless is what turned a dead proxy into a flood of
    "no status found (invalid number or page changed)" rows — the tracking
    numbers were fine; nothing had loaded. Transient by nature (a rotating proxy
    hands out a new exit IP on the next launch), so the row is retried.
    """


# Serializes browser STARTUP across threads (the bulk runner scrapes rows in
# parallel). Two SB() instances booting at once race on shared state that
# SeleniumBase writes per-process — most visibly the ad-block extension it
# unpacks into downloaded_files/ad_block, which fails the loser with
# "[Errno 17] File exists". Launching is also the memory spike, so staggering it
# keeps two Chromes from allocating simultaneously and OOM-killing each other.
# Only startup is serialized; page work still overlaps.
_BROWSER_START_LOCK = threading.Lock()


def _cap(text: str) -> str:
    """One readable line: drop Selenium's stack frames and cap the length."""
    first = re.split(r"\bStacktrace\b|\n", text, maxsplit=1)[0].strip()
    return (first[:197] + "…") if len(first) > 200 else first


def humanize_error(exc: BaseException) -> str:
    """A short, actionable message for the UI in place of a Selenium stack dump.

    Selenium raises multi-kilobyte messages with hex stack traces that tell the
    user nothing and swamp the results table. Map the ones we actually see to
    plain language and truncate anything unrecognized.
    """
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()

    if isinstance(exc, PageLoadFailed):
        # Already written for the user, and it names the setting to change.
        return _cap(text)
    if isinstance(exc, Blocked) or "captcha" in lowered or "bot-check" in lowered:
        # A block that already carries its own diagnosis (UPS's rate-limit
        # message, raised so the row retries on a fresh IP) keeps it — the
        # generic wording would throw away the one line that says what to do.
        if "rate-limiting" in lowered:
            return _cap(text)
        return "The carrier's bot-check blocked us. Retrying with a new IP may help."
    if (
        "invalid session id" in lowered
        or "active window was already closed" in lowered
        or "session deleted" in lowered
        or "connection refused" in lowered
        or "unable to receive message from renderer" in lowered
        or "chrome not reachable" in lowered
    ):
        return (
            "The browser crashed mid-scrape — usually the server running out of "
            "memory. Lower the concurrency or give the container more RAM."
        )
    if "errno 17" in lowered or isinstance(exc, FileExistsError):
        return "Two browsers started at once and clashed over the same profile."
    if "timed out" in lowered or "timeout" in lowered:
        return "The carrier's page didn't finish loading in time."

    # Unknown: keep the first line only, and cap it. Selenium appends
    # "Stacktrace:" followed by dozens of hex frames — never useful here.
    return _cap(text) or "Unknown error."


# Text markers that mean "we got walled, not real content".
_BLOCK_MARKERS = (
    "just a moment", "checking your browser", "access denied",
    "unusual traffic", "are you a robot", "verify you are human",
    "attention required",
)


class BaseScraper(ABC):
    carrier: Carrier

    # ---- subclass hooks --------------------------------------------------
    @abstractmethod
    def build_url(self, tracking_number: str) -> str:
        """Public tracking page URL for this number."""

    def api_url(self, tracking_number: str) -> Optional[str]:
        """Optional internal JSON endpoint. Return None to parse the DOM instead."""
        return None

    def parse_json(self, data: dict[str, Any], tracking_number: str) -> TrackingResult:
        raise NotImplementedError

    def parse_dom(self, sb, tracking_number: str) -> TrackingResult:
        raise NotImplementedError

    def get_proxy(self) -> Optional[str]:
        """Proxy to route this scraper's browser through. Subclasses override
        (e.g. FedEx uses Scrape.do proxy mode for a residential IP)."""
        return config.proxy_or_none()

    # ---- shared machinery ------------------------------------------------
    def _require_loaded(self, sb) -> None:
        """Raise unless the browser is actually sitting on the carrier's page.

        Chrome swaps the URL for ``chrome-error://chromewebdata/`` and serves an
        empty document when the connection fails — a proxy that refuses to
        tunnel being the usual reason. Every scraper downstream then sees a page
        with no shipment on it and reports the tracking number as invalid, which
        is the wrong diagnosis and the expensive kind: 11 of 12 UPS rows in a
        bulk run "failed" that way while the same numbers tracked fine with
        USE_PROXIES=false. Check once, here, for all four carriers.
        """
        try:
            href = sb.execute_script("return location.href") or ""
        except Exception:
            return  # can't ask the page; let the normal parse path report
        if href.startswith("chrome-error://"):
            raise PageLoadFailed(self._load_failure_message())

    def _load_failure_message(self) -> str:
        """Why the page didn't load, phrased as something the user can act on."""
        if self.get_proxy():
            return (
                "The carrier's page never loaded — the proxy refused to connect. "
                "Check PROXY_URL's credentials, or set USE_PROXIES=false in .env "
                "to scrape without one."
            )
        return (
            "The carrier's page never loaded — Chrome couldn't reach the site. "
            "Check this machine's internet connection, then try again."
        )

    def _check_block(self, sb) -> None:
        title = (sb.get_title() or "").lower()
        # Only sniff a small slice of source to keep it cheap.
        try:
            src = (sb.get_page_source() or "").lower()[:4000]
        except Exception:
            src = ""
        if any(m in title or m in src for m in _BLOCK_MARKERS):
            raise Blocked("bot-check / CAPTCHA wall detected")

    @property
    def uses_shared_browser(self) -> bool:
        """Whether this carrier's lookups can share one long-lived browser.

        False for a scraper that fetches over plain HTTP instead of driving
        Chrome (FedEx via Scrape.do), where there is no browser to reuse.
        """
        return True

    def session(self, max_lookups: Optional[int] = None) -> "BrowserSession":
        """A browser held open across many tracking numbers for this carrier.

        Launching Chrome costs ~10-15s of the ~40s a lookup takes, and a bulk
        run paid it once per row — 288 launches for 288 parcels. Opening once
        and feeding numbers through it removes nearly all of that, without
        adding any concurrency: still one page at a time.

        Use it as a context manager; the browser is always torn down on exit.
        """
        return BrowserSession(self, max_lookups)

    def _launch(self):
        """Start one stealth browser. Returns (context_manager, live session)."""
        proxy = self.get_proxy()
        # Required for Chrome inside containers (runs as root, no /dev/shm).
        # Harmless on desktop; makes the Docker/Render deploy work.
        chromium_arg = "--no-sandbox,--disable-dev-shm-usage"
        # Scrape.do proxy mode intercepts HTTPS with its own cert, so the browser
        # must not reject it. Only added when routing through Scrape.do (directly
        # or via the local gost forwarder).
        if proxy and ("scrape.do" in proxy or proxy == config.SCRAPEDO_FORWARDER):
            chromium_arg += ",--ignore-certificate-errors"

        # Serialize the launch itself (see _BROWSER_START_LOCK). The lock is
        # released as soon as the browser is up, so scrapes still overlap.
        with _BROWSER_START_LOCK:
            browser = SB(
                uc=True,
                headless=config.HEADLESS,
                # On Linux servers, run headed inside a virtual display (Xvfb)
                # instead of headless — UC Mode is far harder to detect this way
                # (needed for Akamai-protected FedEx). Ignored on Windows/macOS.
                xvfb=config.USE_XVFB,
                proxy=proxy,
                locale_code="en",
                ad_block=True,
                chromium_arg=chromium_arg,
            )
            sb = browser.__enter__()
        return browser, sb

    def _fetch(self, sb, tracking_number: str) -> TrackingResult:
        """Drive an ALREADY-OPEN browser to one result.

        No launching and no retrying here — those belong to BrowserSession, so
        this same body serves both a one-shot lookup and the 200th number fed
        through a reused browser.
        """
        # Open the public page in stealth mode; this clears most bot checks.
        sb.uc_open_with_reconnect(self.build_url(tracking_number),
                                  reconnect_time=config.RECONNECT_TIME)

        # If a Cloudflare/Turnstile checkbox appears, try to click it.
        # Catch BaseException: headless servers have no display and pyautogui
        # raises SystemExit (missing tkinter) — must not kill the scrape.
        if config.SOLVE_CAPTCHA:
            try:
                sb.uc_gui_click_captcha()
            except BaseException:
                pass  # no captcha present, or GUI-click unavailable

        self._require_loaded(sb)
        self._check_block(sb)

        api = self.api_url(tracking_number)
        if api:
            # Reuse the now-trusted session (cookies/fingerprint) to hit the
            # internal JSON endpoint directly — clean data, no HTML parsing.
            sb.uc_open_with_reconnect(api, reconnect_time=2)
            self._require_loaded(sb)
            self._check_block(sb)
            body = sb.get_text("body")
            try:
                data = json.loads(body)
            except (ValueError, TypeError):
                return TrackingResult.failure(
                    tracking_number, self.carrier,
                    "internal API did not return JSON (verify api_url)")
            result = self.parse_json(data, tracking_number)
        else:
            result = self.parse_dom(sb, tracking_number)

        result.scraped_at = datetime.now(timezone.utc)
        return result

    def scrape(self, tracking_number: str) -> TrackingResult:
        """Look up one number in its own browser (opened and closed here).

        The single-shipment endpoint's path. Bulk runs should use session()
        instead so one browser serves many numbers.
        """
        with self.session(max_lookups=1) as s:
            return s.track(tracking_number)

    def polite_delay(self) -> None:
        """Random human-like pause between tracking numbers."""
        time.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))


# Exceptions that mean "try again on a fresh browser" rather than "this number
# has no data". A not-found number is RETURNED as TrackingResult.failure(), so
# it never lands here and never burns a retry.
TRANSIENT = (Blocked, PageLoadFailed, WebDriverException, OSError)


class BrowserSession:
    """One Chrome, many tracking numbers.

    Opening Chrome costs ~10-15s of the ~40s a lookup takes. A bulk run used to
    pay that per row — 288 launches for 288 parcels — so a session keeps the
    browser open and feeds numbers through it. Still strictly one page at a
    time: this removes wasted startup, it does not add concurrency.

    The browser is recycled every `max_lookups` numbers so a long run doesn't
    ride one fingerprint forever, and it is relaunched automatically whenever it
    dies, so one crash costs the row a retry rather than killing the run.
    """

    def __init__(self, scraper: BaseScraper, max_lookups: Optional[int] = None):
        self.scraper = scraper
        self.max_lookups = max_lookups or config.SESSION_MAX_LOOKUPS
        self._browser = None
        self._sb = None
        self._used = 0
        self.launches = 0   # for logging/tests: how many Chromes this cost

    # -- lifecycle ---------------------------------------------------------
    def __enter__(self) -> "BrowserSession":
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def _ensure_browser(self) -> None:
        """Open a browser if we don't have a usable one."""
        if self._sb is not None and self._used < self.max_lookups:
            return
        self.close()  # recycle: past the reuse budget, or nothing open yet
        self._browser, self._sb = self.scraper._launch()
        self._used = 0
        self.launches += 1

    def close(self) -> None:
        """Tear the browser down. Safe to call repeatedly."""
        browser, self._browser, self._sb = self._browser, None, None
        self._used = 0
        if browser is None:
            return
        try:
            browser.__exit__(*sys.exc_info())
        except Exception:
            # A browser that already died can't be closed cleanly, and saying so
            # would mask the real error we're probably unwinding from.
            pass

    # -- work --------------------------------------------------------------
    def track(self, tracking_number: str) -> TrackingResult:
        """Look up one number, relaunching and retrying if the browser dies."""
        # A carrier that doesn't drive Chrome (FedEx over Scrape.do) has no
        # browser to share; hand straight to its own implementation.
        if not self.scraper.uses_shared_browser:
            return self.scraper.scrape(tracking_number)

        last: BaseException | None = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                self._ensure_browser()
                self._used += 1
                return self.scraper._fetch(self._sb, tracking_number)
            except TRANSIENT as exc:
                last = exc
                # Whatever went wrong, this browser is no longer trustworthy —
                # drop it so the next attempt starts clean (and, for a block, on
                # a fresh fingerprint/IP).
                self.close()
                if attempt < config.MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 30) + random.uniform(0, 1))
        assert last is not None
        raise last
