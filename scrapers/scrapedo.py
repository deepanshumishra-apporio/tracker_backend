"""
Scrape.do fetch helper — residential/mobile proxy + JS rendering via API.

FedEx sits behind Akamai, which blocks datacenter IPs (Azure VM, Webshare
datacenter) and serves a fake "can't find that tracking number" / system-error
page. Instead of driving a local browser through a residential proxy, we hand
the URL to Scrape.do: it renders the page (runs its JavaScript) from a
residential/mobile IP and returns the final HTML, which we parse like the DOM.

Akamai blocking is probabilistic, so fetch() retries with a fresh IP when it
detects the block/error page.
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Optional

import requests
from bs4 import BeautifulSoup

import config

_API = "http://api.scrape.do/"

# Wait for the async tracking data, then click "View more details" so the Travel
# history + Shipment facts render, then a short wait for that content. This is
# the ONLY wait when clicking (no separate customWait) — keeps each retry fast.
_EXPAND_ACTIONS = [
    {"Action": "Wait", "Timeout": 8000},
    {"Action": "Execute", "Execute":
        "for(const el of document.querySelectorAll('button,a,span')){"
        "if(((el.textContent||'').trim().toLowerCase())"
        ".startsWith('view more details')){el.click();break;}}"},
    {"Action": "Wait", "Timeout": 3000},
]

# FedEx Akamai blocks most rendered requests (redirect to /system-error) and is
# not reliably beatable via Scrape.do, so we try only a few times and then let
# the caller fall back to a direct carrier-site link — rather than making the
# user wait minutes on retries that usually fail anyway.
_MAX_TRIES = 3

# Markers that mean Akamai bounced us to an error/challenge page (NOT the same as
# FedEx's legitimate "can't find that tracking number" — that's handled by the
# parser as a real not-found result, so it must NOT appear here).
_BLOCK_MARKERS = (
    "system-error",
    "we can't process your request",
    "you don't have permission to view this webpage",
    "access denied",
)


class ScrapeDoError(RuntimeError):
    """Raised when Scrape.do keeps returning an Akamai block after retries."""


def fetch(target_url: str, *, click_view_more: bool = False,
          max_tries: int = _MAX_TRIES) -> str:
    """Return rendered HTML for target_url, retrying past Akamai's system-error.

    Each FedEx render only clears Akamai ~1/3 of the time (else it redirects to
    /system-error), so we retry with a fresh render until one lands on the real
    tracking page.
    """
    params = {
        "token": config.SCRAPEDO_TOKEN,
        "url": target_url,
        "super": "true",            # residential/mobile exit IP
        "render": "true",           # run the page's JavaScript (FedEx is an SPA)
        "geoCode": config.SCRAPEDO_GEO,
        "blockResources": "false",  # let Akamai's own sensor JS load (avoids flag)
        "customWait": "10000",      # wait for FedEx's async tracking data
    }
    if click_view_more:
        params["playWithBrowser"] = json.dumps(_EXPAND_ACTIONS, separators=(",", ":"))

    url = _API + "?" + urllib.parse.urlencode(params)
    last = ""
    for attempt in range(max(1, max_tries)):
        try:
            resp = requests.get(url, timeout=200)
        except requests.RequestException as e:
            last = f"request error: {e}"
            continue
        html = resp.text or ""
        resolved = resp.headers.get("Scrape.do-Resolved-Url", "")  # case-insensitive
        blocked = (
            resp.status_code >= 400
            or "system-error" in resolved.lower()
            or any(m in html.lower()[:8000] for m in _BLOCK_MARKERS)
        )
        if not blocked:
            return html
        last = resolved or f"HTTP {resp.status_code}"
        # Space out retries — rapid-fire renders to the same domain make Akamai
        # flag the pattern and block more; a short pause improves the pass rate.
        time.sleep(3)
    raise ScrapeDoError(
        f"FedEx Akamai kept blocking via Scrape.do after {max_tries} tries "
        f"(last: {last})")


def text_lines(html: str) -> list[str]:
    """innerText-like list of non-empty lines, mirroring sb.get_text('body')."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    body = soup.body or soup
    return [ln.strip() for ln in body.get_text("\n").split("\n") if ln.strip()]


def select_text(html: str, css: str) -> Optional[str]:
    """Text content of the first element matching a CSS selector, or None."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.select_one(css)
    return el.get_text(strip=True) if el else None
