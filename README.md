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

index.py             web API: /api/track (one shipment) + /api/batch (upload)
  └─ batch.py          spreadsheet upload: parse company/awb -> background job
                       -> poll for progress -> download results as .xlsx
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
| `GET`  | `/api/track?carrier=&tracking_number=`  | Live scrape of one shipment; returns the normalized result. No storage. |
| `POST` | `/api/batch`                            | Upload an Excel/CSV of `company` + `awb` rows. Returns a queued job (202). |
| `GET`  | `/api/batch`                            | Recent jobs, newest first (no rows).               |
| `GET`  | `/api/batch/{job_id}`                   | Job progress + every row's live state and result. Poll this. |
| `POST` | `/api/batch/{job_id}/cancel`            | Stop a run; in-flight rows finish, the rest are skipped. |
| `GET`  | `/api/batch/{job_id}/export.xlsx`       | Results workbook (summary sheet + full event history). |
| `GET`  | `/api/batch/template.xlsx`              | Blank two-column upload template.                  |

### Bulk tracking from a spreadsheet

Upload a sheet with a **`company`** and an **`awb`** column:

| company | awb                |
| ------- | ------------------ |
| UPS     | 1Z999AA10123456784 |
| FedEx   | 987654321098       |
| DHL     | 1234567890         |
| Aramex  | 4567891234         |

* Header names are matched loosely — `carrier`, `courier`, `AWB No.`,
  `Tracking Number` and friends all work, and extra columns are ignored.
* Company values are resolved to a carrier case-insensitively, including
  variants like `DHL Express` and `Federal Express`.
* Rows that can't be tracked (unknown company, missing/illegal AWB, duplicates)
  are kept and reported with a per-row error rather than failing the upload.
* Limits: 5 MB, 500 rows per file, `.xlsx`/`.xlsm`/`.csv` only.

Scraping N rows takes minutes, so the upload only parses and queues. Rows are
scraped by a small background pool (`?concurrency=`, default 2 — each worker
drives its own Chrome) and the client polls the job. **Jobs live in memory only:
restarting the API clears them.**

```bash
curl -F file=@shipments.xlsx http://127.0.0.1:8000/api/batch      # -> {"id": "...", ...}
curl http://127.0.0.1:8000/api/batch/<id>                          # progress
curl -O -J http://127.0.0.1:8000/api/batch/<id>/export.xlsx        # results
```

`CORS_ORIGINS` (comma-separated) controls allowed frontend origins
(default `http://localhost:3000,http://127.0.0.1:3000`).

> The optional batch scraper (`runner.py` + `storage.py`) DOES persist to SQLite
> (`tracking.db`) and is independent of this web API. `tracking.db` is a runtime
> artifact — it is git-ignored and never committed.

## Tests

```bash
python -m pytest -q      # scraper parsers + /api/track + /api/batch
                         # (scrapers mocked — no browser, no network)
```

## Deploy on an Azure VM

The scrapers drive a real Chrome via SeleniumBase UC Mode, so it runs on a Linux
VM (not a serverless host). Everything lives in `deploy/`:

| File                        | Purpose                                             |
| --------------------------- | --------------------------------------------------- |
| `deploy/setup.sh`           | One-time provisioning: Chrome + Xvfb + Python venv. |
| `deploy/update.sh`          | Redeploy: pull + deps + verify + restart, with rollback. |
| `deploy/tracker.env.example`| systemd env template (bind, display, **proxy**, CORS). |
| `deploy/tracker.service`    | systemd unit — auto-start + auto-restart.           |

**Steps (Ubuntu 22.04/24.04 VM):**

```bash
# 1. Clone the repo on the VM, then from backend/:
bash deploy/setup.sh                       # installs Chrome, Xvfb, deps into .venv

# 2. Configure runtime env (bind, proxy, CORS):
cp deploy/tracker.env.example deploy/tracker.env
nano deploy/tracker.env                     # set a REAL PROXY_URL (see below)
sudo cp deploy/tracker.env /etc/tracker.env

# 3. Install + start the service (edit User/paths in the unit first if not azureuser):
sudo cp deploy/tracker.service /etc/systemd/system/tracker.service
sudo systemctl daemon-reload
sudo systemctl enable --now tracker
systemctl status tracker                    # should be active (running)
journalctl -u tracker -f                    # live logs

# 4. Open the port in the Azure Network Security Group (inbound: TCP 8000),
#    or put Nginx/Caddy in front for TLS on 443.
```

**Redeploy after a code change:**

```bash
cd ~/tracker/backend && bash deploy/update.sh
```

`deploy/update.sh` pulls, installs `requirements.txt` into the venv, verifies
the app still imports, restarts the service, and waits for `/api/health` —
rolling back to the previous commit if either check fails.

> Do **not** just `git pull && systemctl restart`. A release that adds a Python
> dependency (the bulk-upload feature added `openpyxl` and `python-multipart`)
> will import-crash on restart and take the whole API down, not just the new
> endpoints.

**Notes:**
- The VM runs Chrome **headed inside Xvfb** (`HEADLESS=false`, `USE_XVFB=true`) —
  SeleniumBase manages the virtual display itself, so no separate Xvfb process.
- ⚠️ **An Azure VM IP is a datacenter IP** and gets blocked by the carriers'
  anti-bot just like any cloud host. A **residential/mobile proxy**
  (`USE_PROXIES=true` + `PROXY_URL`) is still required — this is the single
  biggest factor. `config.py` warns loudly on startup if `USE_PROXIES=true` but
  `PROXY_URL` is empty.
- Each request launches a browser (~20–40s) and is memory-hungry — use a VM with
  **≥2 GB RAM** and keep concurrency low.

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
