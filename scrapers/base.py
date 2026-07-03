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
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

from seleniumbase import SB
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import config
from models import Carrier, TrackingResult


class Blocked(Exception):
    """Raised when we detect a block/CAPTCHA wall so tenacity retries (new IP)."""


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

    @retry(
        retry=retry_if_exception_type(Blocked),
        stop=stop_after_attempt(config.MAX_RETRIES),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        reraise=True,
    )
    def scrape(self, tracking_number: str) -> TrackingResult:
        with SB(
            uc=True,
            headless=config.HEADLESS,
            # On Linux servers, run headed inside a virtual display (Xvfb) instead
            # of headless — UC Mode is far harder to detect this way (needed for
            # Akamai-protected FedEx). Ignored on Windows/macOS.
            xvfb=config.USE_XVFB,
            proxy=config.proxy_or_none(),
            locale_code="en",
            ad_block=True,
            # Required for Chrome inside containers (runs as root, no /dev/shm).
            # Harmless on desktop; makes the Docker/Render deploy work.
            chromium_arg="--no-sandbox,--disable-dev-shm-usage",
        ) as sb:
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

    def polite_delay(self) -> None:
        """Random human-like pause between tracking numbers."""
        time.sleep(random.uniform(config.MIN_DELAY, config.MAX_DELAY))
