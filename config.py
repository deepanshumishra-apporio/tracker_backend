"""
Central configuration. This is the ONE file you edit to go from
local-testing mode to production (proxies + tuning).
"""
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# PROXY SETTINGS  (SeleniumBase format — NO http:// prefix)
# ---------------------------------------------------------------------------
# Leave USE_PROXIES = False for local testing (you WILL get blocked at volume).
# When you buy a residential proxy plan (Bright Data / Oxylabs / Smartproxy /
# IPRoyal), set USE_PROXIES = True and fill PROXY_URL in ONE of these forms:
#     "USER:PASS@gate.smartproxy.com:7000"     <- authenticated (most common)
#     "IP:PORT"                                 <- open proxy
#     "socks5://USER:PASS@IP:PORT"              <- socks
# A rotating-gateway URL rotates the exit IP automatically each new session.
USE_PROXIES = os.getenv("USE_PROXIES", "false").lower() == "true"
PROXY_URL = os.getenv("PROXY_URL", "")

# Loud warning: USE_PROXIES=true but no PROXY_URL means we'd silently scrape
# proxy-less from a datacenter IP and get blocked by every carrier — the exact
# failure this setting is meant to prevent. Surface it in the deploy logs.
if USE_PROXIES and not PROXY_URL:
    import warnings
    warnings.warn(
        "USE_PROXIES=true but PROXY_URL is empty — falling back to NO proxy. "
        "Set PROXY_URL (e.g. in the Render dashboard) or scrapes will be blocked.",
        stacklevel=2,
    )


def proxy_or_none() -> Optional[str]:
    """What to hand SeleniumBase's proxy= argument."""
    return PROXY_URL if (USE_PROXIES and PROXY_URL) else None


# ---------------------------------------------------------------------------
# SCRAPE.DO  (residential proxy + JS rendering via API — used for FedEx)
# ---------------------------------------------------------------------------
# FedEx is behind Akamai and blocks datacenter IPs. When SCRAPEDO_TOKEN is set,
# the FedEx scraper fetches the rendered page through Scrape.do (residential IP +
# headless render) instead of driving a local browser. Get the token from your
# Scrape.do dashboard. Leave empty to use the SeleniumBase browser path instead.
SCRAPEDO_TOKEN = os.getenv("SCRAPEDO_TOKEN", "")
# Country to route through (Scrape.do geoCode). FedEx is US-facing → "us".
SCRAPEDO_GEO = os.getenv("SCRAPEDO_GEO", "us")


# Local gost forwarder (started by entrypoint.sh) that injects Scrape.do's
# proxy auth for Chrome. Chrome talks to this; gost forwards to proxy.scrape.do.
SCRAPEDO_FORWARDER = os.getenv("SCRAPEDO_FORWARDER", "127.0.0.1:8899")


def scrapedo_proxy() -> Optional[str]:
    """Address Chrome uses for Scrape.do proxy mode: the LOCAL gost forwarder.

    Used for Akamai-protected carriers (FedEx): the real UC Mode Chrome renders
    the page (so Akamai's bot sensor sees a real browser) while Scrape.do supplies
    a residential exit IP — proven to load the real FedEx page where Scrape.do's
    render API (a detectable headless browser) gets bounced to system-error.

    Chrome can't auth to Scrape.do directly (the "super=true" password), so it
    talks to gost on 127.0.0.1:8899, which injects the credentials upstream.
    """
    return SCRAPEDO_FORWARDER if SCRAPEDO_TOKEN else None


# ---------------------------------------------------------------------------
# RATE LIMITING — human-like, randomized delays (seconds) between requests.
# Slower = less likely to get banned. Do not lower these carelessly.
# ---------------------------------------------------------------------------
# How many spreadsheet rows a bulk run scrapes at once. Each one drives its own
# real Chrome (~0.5-1 GB with a heavy carrier page), so this multiplies memory,
# not just CPU. Default 1: a 2 GB VM cannot hold two, and when it OOMs the
# kernel kills Chrome mid-scrape — which surfaces as a flood of "invalid session
# id" rows. Raise it only on a box with RAM to spare.
BATCH_CONCURRENCY = max(1, int(os.getenv("BATCH_CONCURRENCY", "1")))

# How many tracking numbers one browser handles before it is recycled. Reusing
# a browser is the whole speed win (Chrome startup is ~10-15s of a ~40s lookup),
# but riding one fingerprint for hundreds of lookups is exactly what anti-bot
# systems watch for — so trade a launch every N numbers for a fresh identity.
SESSION_MAX_LOOKUPS = max(1, int(os.getenv("SESSION_MAX_LOOKUPS", "25")))

MIN_DELAY = float(os.getenv("MIN_DELAY", "5"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "20"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Seconds UC Mode stays "disconnected" after opening a page so it can slip past
# the bot-check before chromedriver re-attaches. 3-6s is typical for CF sites.
RECONNECT_TIME = float(os.getenv("RECONNECT_TIME", "4"))

# UC Mode works BEST headed (headless=False). On a server, run headless2 or use
# a virtual display (Xvfb on Linux). Set HEADLESS=true only if you must.
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Run the browser headed inside a virtual display (Xvfb) — Linux servers only.
# Much harder for anti-bot (Akamai/DataDome) to detect than headless. The Docker
# image sets this true; ignored on Windows/macOS.
USE_XVFB = os.getenv("USE_XVFB", "false").lower() == "true"

# Try to auto-click Cloudflare/reCAPTCHA if a challenge appears.
SOLVE_CAPTCHA = os.getenv("SOLVE_CAPTCHA", "true").lower() == "true"

# ---------------------------------------------------------------------------
# STORAGE
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "tracking.db")
