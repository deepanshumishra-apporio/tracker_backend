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


def proxy_or_none() -> Optional[str]:
    """What to hand SeleniumBase's proxy= argument."""
    return PROXY_URL if (USE_PROXIES and PROXY_URL) else None


# ---------------------------------------------------------------------------
# RATE LIMITING — human-like, randomized delays (seconds) between requests.
# Slower = less likely to get banned. Do not lower these carelessly.
# ---------------------------------------------------------------------------
MIN_DELAY = float(os.getenv("MIN_DELAY", "5"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "20"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Seconds UC Mode stays "disconnected" after opening a page so it can slip past
# the bot-check before chromedriver re-attaches. 3-6s is typical for CF sites.
RECONNECT_TIME = float(os.getenv("RECONNECT_TIME", "4"))

# UC Mode works BEST headed (headless=False). On a server, run headless2 or use
# a virtual display (Xvfb on Linux). Set HEADLESS=true only if you must.
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"

# Try to auto-click Cloudflare/reCAPTCHA if a challenge appears.
SOLVE_CAPTCHA = os.getenv("SOLVE_CAPTCHA", "true").lower() == "true"

# ---------------------------------------------------------------------------
# STORAGE
# ---------------------------------------------------------------------------
DB_PATH = os.getenv("DB_PATH", "tracking.db")
