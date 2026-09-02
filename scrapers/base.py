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


# Serializes browser STARTUP across threads (the bulk runner scrapes rows in
# parallel). Two SB() instances booting at once race on shared state that
# SeleniumBase writes per-process — most visibly the ad-block extension it
# unpacks into downloaded_files/ad_block, which fails the loser with
# "[Errno 17] File exists". Launching is also the memory spike, so staggering it
# keeps two Chromes from allocating simultaneously and OOM-killing each other.
# Only startup is serialized; page work still overlaps.
_BROWSER_START_LOCK = threading.Lock()


def humanize_error(exc: BaseException) -> str:
    """A short, actionable message for the UI in place of a Selenium stack dump.

    Selenium raises multi-kilobyte messages with hex stack traces that tell the
    user nothing and swamp the results table. Map the ones we actually see to
    plain language and truncate anything unrecognized.
    """
    text = str(exc) or exc.__class__.__name__
    lowered = text.lower()

    if isinstance(exc, Blocked) or "captcha" in lowered or "bot-check" in lowered:
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
    first = re.split(r"\bStacktrace\b|\n", text, maxsplit=1)[0].strip()
    return (first[:197] + "…") if len(first) > 200 else (first or "Unknown error.")


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
    def _check_block(self, sb) -> None:
        title = (sb.get_title() or "").lower()
        # Only sniff a small slice of source to keep it cheap.
        try:
            src = (sb.get_page_source() or "").lower()[:4000]
        except Exception:
            src = ""
        if any(m in title or m in src for m in _BLOCK_MARKERS):
            raise Blocked("bot-check / CAPTCHA wall detected")

    # Retry a blocked scrape (new IP may get through) AND a crashed browser.
    # WebDriverException covers the whole family of "the browser died under us"
    # failures — invalid session id, active window already closed, chromedriver
    # connection refused — which are transient and almost always succeed on a
    # fresh browser. Before this they were fatal for the row, which is what
    # turned one memory spike into ~130 failed rows in a single bulk run.
    # OSError catches the startup profile clash. A legitimately-not-found number
    # returns TrackingResult.failure() rather than raising, so it is never
    # retried and costs nothing.
    @retry(
        retry=retry_if_exception_type((Blocked, WebDriverException, OSError)),
        stop=stop_after_attempt(config.MAX_RETRIES),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        reraise=True,
    )
    def scrape(self, tracking_number: str) -> TrackingResult:
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

        try:
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

            self._check_block(sb)

            api = self.api_url(tracking_number)
            if api:
                # Reuse the now-trusted session (cookies/fingerprint) to hit the
                # internal JSON endpoint directly — clean data, no HTML parsing.
                sb.uc_open_with_reconnect(api, reconnect_time=2)
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
        finally:
            # Always tear the browser down, including when a retry is about to
            # relaunch one. A leaked Chrome keeps its memory, and enough of them
            # is exactly what starves the next scrape into "invalid session id".
            try:
                browser.__exit__(*sys.exc_info())
            except Exception:
                pass  # teardown of an already-dead browser must not mask the real error

    def polite_delay(self) -> None:
        """Random human-like pause between tracking numbers."""
        time.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))
