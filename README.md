# Multi-Carrier Delivery Tracker (UPS / FedEx / DHL / Aramex)

Scrapes delivery/tracking status from four carriers and stores normalized
results in SQLite. Engine is **SeleniumBase UC Mode** — purpose-built to beat
Cloudflare / DataDome / Imperva bot-detection — with proxy rotation, human-like
delays, retries, and auto-CAPTCHA clicking.

## Why SeleniumBase UC Mode
Plain Selenium/Playwright get flagged instantly by these carriers. UC Mode:
- renames the CDP console variables anti-bot scripts scan for
- launches Chrome first, THEN attaches chromedriver (automation flag unseen)
- disconnects chromedriver during page loads/clicks (looks human at inspection)
- `uc_gui_click_captcha()` auto-clicks Cloudflare Turnstile / reCAPTCHA
- built-in proxy support incl. authenticated proxies: `USER:PASS@HOST:PORT`

## Architecture

```
runner.py            batch job: read CSV -> scrape each -> save to DB
  |
  ├─ scrapers/base.py   shared machinery: SeleniumBase UC Mode, proxy rotation,
  |                     random delays, retry/backoff, internal-JSON fetch
  ├─ scrapers/ups.py    ┐
  ├─ scrapers/fedex.py  │ one adapter per carrier; each returns the SAME
  ├─ scrapers/dhl.py    │ normalized TrackingResult (models.py)
  ├─ scrapers/aramex.py ┘
  ├─ storage.py         SQLite: latest status + full event history
  ├─ models.py          the ONE normalized data shape (pydantic)
  └─ config.py          the ONE file you edit to enable proxies/CAPTCHA
```

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env               # then edit .env for production
```
SeleniumBase auto-downloads the matching chromedriver on first run — no manual
browser/driver install needed. (You do need Google Chrome installed.)

## Run the scraper (batch job)

```bash
python runner.py numbers.csv
```

`numbers.csv` columns: `carrier,tracking_number` (carrier = ups|fedex|dhl|aramex).

## Run the Web API (live, no storage)

`index.py` (FastAPI) scrapes a carrier live on request and returns a normalized
result. **Nothing is persisted** — it creates no database.

```bash
uvicorn index:app --reload --port 8000
# or: python index.py   (honors HOST/PORT/RELOAD env vars)
```

Interactive docs at `http://127.0.0.1:8000/docs`.

### Endpoints
| Method | Path                                    | Purpose                                            |
| ------ | --------------------------------------- | -------------------------------------------------- |
| `GET`  | `/api/health`                           | Liveness check.                                    |
| `GET`  | `/api/track?carrier=&tracking_number=`  | Live scrape; returns the normalized result. No storage. |

`CORS_ORIGINS` (comma-separated) controls allowed frontend origins
(default `http://localhost:3000,http://127.0.0.1:3000`).

> The optional batch scraper (`runner.py` + `storage.py`) DOES persist to SQLite
> (`tracking.db`) and is independent of this web API. `tracking.db` is a runtime
> artifact — it is git-ignored and never committed.

## Tests

```bash
python -m pytest -q      # scraper parsers + /api/track (mocked scraper, no network)
```

## Deploying (important)

The scrapers drive a real Chrome via SeleniumBase UC Mode, so the server needs:
- **Google Chrome installed** (SeleniumBase auto-manages the driver).
- **A display**: UC Mode is strongest *headed*. On a Linux server run under a
  virtual display (`xvfb-run ...`) rather than `HEADLESS=true` where possible.
- **Residential proxies at volume** — set `USE_PROXIES=true` + `PROXY_URL`.
  Without them you *will* get blocked beyond low volumes (single lookups are fine).
- Each request launches a browser (~20–40s). Size concurrency accordingly.

## IMPORTANT — two things you must do before this returns real data

1. **Confirm each carrier's data source.**
   Each scraper has `api_url()` (returns `None` by default → DOM parsing) and a
   `parse_json()`/`parse_dom()` that are *templates*. Open the carrier's tracking
   page in Chrome DevTools > Network > XHR, find the call returning tracking JSON.
   If it's a GET, return its URL from `api_url()` and align `parse_json()` field
   names. If it's a POST (FedEx/UPS), leave `api_url()` as `None` and refine the
   `parse_dom()` CSS selectors instead. One-time step per carrier.

2. **Enable residential proxies** (`USE_PROXIES=true` + `PROXY_URL` in `.env`).
   At 100–2000/day these sites WILL block a single IP. Datacenter proxies are
   pre-banned — use **residential/mobile** (Bright Data, Oxylabs, Smartproxy,
   IPRoyal). This is the #1 factor in not getting banned.

## How the anti-ban strategy works
- **SeleniumBase UC Mode** — beats Cloudflare/DataDome/Imperva fingerprinting
- **Residential proxy rotation** — different real IP per session (biggest factor)
- **Randomized delays** (`MIN_DELAY`/`MAX_DELAY`) — no robotic cadence
- **Retry + backoff** — on a detected block, wait longer and rotate IP
- **Auto-CAPTCHA** — `uc_gui_click_captcha()` clicks the challenge if it appears
- **Trusted-session JSON fetch** — after clearing the bot-check, reuse the same
  session to hit the internal JSON endpoint (more accurate than HTML parsing)

## Scaling to production (medium volume)
- Add a scheduler (cron / Windows Task Scheduler) to re-poll active shipments
  every few hours; stop polling once `status = delivered`.
- Run **headed** where possible (UC Mode is weakest headless) — on a Linux
  server use a virtual display (Xvfb) rather than `HEADLESS=true`.
- Consider a job queue (RQ/Celery) if you parallelize across many proxies.

## Honest caveat
Even SeleniumBase UC Mode does **not guarantee success at scale** — good
residential proxies are still required for 100–2000/day, and carriers keep
tightening defenses. This is a maintenance commitment.

## Honest note
Scraping these carriers is a maintenance commitment (they change their sites and
bot defenses) and has ongoing proxy/CAPTCHA costs. All four also offer **free
official tracking APIs**, which are more reliable and never get IP-banned. If
reliability matters more than avoiding signup, revisit the API route — the
`models.py` / `storage.py` / `runner.py` layers here work unchanged with APIs;
only the per-carrier adapter internals would change.
```
