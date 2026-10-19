"""
Load test for the browserless fast path (UPS, Aramex): how many lookups an
hour does one machine get through, and when does the carrier start blocking?

Two modes:

  direct  (run ON the VM, from backend/ with the venv active)
      Calls the fast fetchers in this process, wave by wave, with no cache and
      no database. Numbers are repeated to reach the target count, so 1,000
      lookups are 1,000 real requests to the carrier even from 681 numbers.

          python loadtest.py direct loadtest_numbers.csv --ups 1000 --aramex 1000

  api     (run from anywhere; needs only the standard library)
      Uploads the numbers to a deployed backend as a bulk job, so the lookups
      happen on that server exactly as production does them. A bulk job drops
      duplicate rows and answers cached numbers instantly, so only numbers the
      server has never seen measure anything.

          python loadtest.py api loadtest_numbers.csv --url https://HOST

The numbers file is any CSV with a carrier column (carrier/company/Courier)
and a number column (tracking_number/awb/AWBNumber); the order export works.
A JSON report is written next to it (--out to change).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
import urllib.request
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

CARRIERS = ("ups", "aramex")
# Lookups a day each carrier has to absorb at 50k/day (share of the order export).
DAILY = {"ups": 26_500, "aramex": 7_400}


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
def load_numbers(path: str) -> dict[str, list[str]]:
    """carrier -> unique numbers, in file order."""
    with open(path, encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip(): c for c in reader.fieldnames or []}
        ccol = next((cols[k] for k in ("carrier", "company", "courier") if k in cols), None)
        ncol = next((cols[k] for k in ("tracking_number", "awb", "awbnumber") if k in cols), None)
        if not ccol or not ncol:
            sys.exit(f"{path}: need a carrier and a tracking number column, got {reader.fieldnames}")
        out: dict[str, dict[str, None]] = {c: {} for c in CARRIERS}
        for row in reader:
            name, number = (row[ccol] or "").lower(), (row[ncol] or "").strip()
            carrier = next((c for c in CARRIERS if c in name), None)
            if carrier and number and number != "--":
                out[carrier][number] = None
    return {c: list(v) for c, v in out.items()}


def take(numbers: list[str], count: int, repeat: bool) -> list[str]:
    if not repeat or len(numbers) >= count:
        return numbers[:count]
    return [numbers[i % len(numbers)] for i in range(count)]


def kind(error: str) -> str:
    """Group error messages: drop numbers and ids so repeats collapse."""
    return re.sub(r"\b[0-9A-Z]{8,}\b", "#", error)[:90]


def projection(carrier: str, rate: float) -> dict:
    per_day = rate * 86_400
    need = DAILY[carrier]
    return {
        "lookups_per_hour": round(rate * 3600),
        "daily_target": need,
        "hours_for_daily_target": round(need / rate / 3600, 2) if rate else None,
        "fits_in_a_day": per_day >= need,
    }


# ---------------------------------------------------------------------------
# direct: call the fetchers in-process
# ---------------------------------------------------------------------------
def run_direct(carrier: str, numbers: list[str], args) -> dict:
    from fast.aramex import AramexFast
    from fast.ups import UPSFast

    fetcher = UPSFast(workers=args.workers) if carrier == "ups" else AramexFast(workers=args.workers)
    warms = []
    if carrier == "ups":   # count cookie warm-ups: each one is a browser visit to ups.com
        real_warm = fetcher._warm

        def counted_warm():
            t = time.monotonic()
            real_warm()
            warms.append(round(time.monotonic() - t, 1))
        fetcher._warm = counted_warm

    waves, errors = [], Counter()
    ok = not_found = failed = 0
    first_block = None
    start = time.monotonic()
    for w, i in enumerate(range(0, len(numbers), args.wave), 1):
        # A wave never holds the same number twice (track_many would merge them).
        wave = list(dict.fromkeys(numbers[i:i + args.wave]))
        t = time.monotonic()
        try:
            got, transient = fetcher.track_many(wave)
        except Exception as exc:
            got, transient = {}, {n: f"fetcher crashed: {exc}" for n in wave}
        took = time.monotonic() - t
        w_ok = sum(1 for r in got.values() if r.ok)
        w_nf = len(got) - w_ok
        w_err = len(wave) - len(got)
        ok, not_found, failed = ok + w_ok, not_found + w_nf, failed + w_err
        errors.update(kind(e) for e in transient.values())
        if w_err and first_block is None:
            first_block = {"wave": w, "after_lookups": i, "minutes_in": round((t - start) / 60, 1),
                           "error": kind(next(iter(transient.values())))}
        waves.append({"wave": w, "size": len(wave), "seconds": round(took, 1),
                      "ok": w_ok, "not_found": w_nf, "failed": w_err})
        print(f"  {carrier} wave {w:>3}: {len(wave):>3} in {took:6.1f}s "
              f"({len(wave) / took:5.2f}/s)  ok {w_ok:>3}  not-found {w_nf:>3}  failed {w_err:>3}"
              + (f"  <- {kind(next(iter(transient.values())))}" if w_err else ""), flush=True)
        if args.stop_on_block and w_err > len(wave) / 2:
            print(f"  {carrier}: more than half the wave failed -- stopping (--stop-on-block)")
            break
        if args.pause:
            time.sleep(args.pause)

    elapsed = time.monotonic() - start
    done = ok + not_found + failed
    # Rate over the time actually spent fetching, so --pause doesn't skew it.
    fetching = sum(x["seconds"] for x in waves) or 1e-9
    return {
        "mode": "direct", "carrier": carrier, "lookups": done,
        "unique_numbers": len(set(numbers)), "workers": args.workers or "FAST_WORKERS",
        "seconds": round(elapsed, 1), "fetch_seconds": round(fetching, 1),
        "per_second": round(done / fetching, 2),
        "ok": ok, "not_found": not_found, "failed": failed,
        "failed_pct": round(100 * failed / done, 1) if done else 0,
        "ups_cookie_warmups": warms if carrier == "ups" else None,
        "first_block": first_block, "errors": dict(errors.most_common()),
        **projection(carrier, done / fetching), "waves": waves,
    }


# ---------------------------------------------------------------------------
# api: drive a deployed backend's bulk job
# ---------------------------------------------------------------------------
def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _ts(s: str | None) -> float | None:
    return datetime.fromisoformat(s).timestamp() if s else None


def run_api(carrier: str, numbers: list[str], args) -> dict:
    base = args.url.rstrip("/") + "/api/batch"
    sheet = io.StringIO()
    w = csv.writer(sheet)
    w.writerow(["company", "awb"])
    for n in numbers:
        w.writerow([carrier, n])
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"loadtest-{carrier}.csv\"\r\nContent-Type: text/csv\r\n\r\n"
            ).encode() + sheet.getvalue().encode() + f"\r\n--{boundary}--\r\n".encode()
    job = _http("POST", base, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    jid = job["id"]
    print(f"  {carrier}: job {jid}, {len(numbers)} rows", flush=True)

    start = time.monotonic()
    cancelled = False
    while job["state"] not in ("completed", "cancelled"):
        time.sleep(5)
        job = _http("GET", f"{base}/{jid}")
        c = job["counts"]
        print(f"  {carrier}: {time.monotonic() - start:6.0f}s  finished {c['finished']}/{c['total']}  "
              f"done {c['done']}  failed {c['failed']}  running {c['running']}", flush=True)
        # Numbers the fast path can't answer fall back to a browser per number
        # (~1 min each), which would tie the server up for hours: stop there.
        if time.monotonic() - start > args.timeout * 60 and job["state"] == "running":
            print(f"  {carrier}: still running after {args.timeout} min -- cancelling the job")
            job = _http("POST", f"{base}/{jid}/cancel")
            cancelled = True

    rows = job["rows"]
    ok = sum(1 for r in rows if r["state"] == "done" and (r["result"] or {}).get("ok"))
    not_found = sum(1 for r in rows if r["state"] == "done" and not (r["result"] or {}).get("ok"))
    failed = sum(1 for r in rows if r["state"] == "failed")
    cached = sum(1 for r in rows if (r["result"] or {}).get("from_cache"))
    errors = Counter(kind(r["error"] or (r["result"] or {}).get("error") or "")
                     for r in rows if r["state"] == "failed")
    # A row that took over 30s went through the browser fallback: the fast path
    # was refused for it (blocked) or timed out.
    durations = [(_ts(r["finished_at"]) or 0) - (_ts(r["started_at"]) or 0)
                 for r in rows if r["started_at"] and r["finished_at"]]
    slow = sum(1 for d in durations if d > 30)
    seconds = (_ts(job["finished_at"]) or time.time()) - (_ts(job["started_at"]) or time.time())
    fetched = len(rows) - cached
    rate = fetched / seconds if seconds > 0 else 0.0
    return {
        "mode": "api", "carrier": carrier, "url": args.url, "job": jid,
        "lookups": len(rows), "from_cache": cached, "cancelled": cancelled,
        "seconds": round(seconds, 1), "per_second": round(rate, 2),
        "ok": ok, "not_found": not_found, "failed": failed,
        "failed_pct": round(100 * failed / len(rows), 1) if rows else 0,
        "browser_fallback_rows": slow, "errors": dict(errors.most_common()),
        **projection(carrier, rate),
    }


# ---------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=("direct", "api"))
    p.add_argument("numbers", help="CSV of carrier + tracking number")
    p.add_argument("--ups", type=int, default=1000, help="UPS lookups (0 to skip)")
    p.add_argument("--aramex", type=int, default=1000, help="Aramex lookups (0 to skip)")
    p.add_argument("--wave", type=int, default=50, help="direct: lookups per wave")
    p.add_argument("--pause", type=float, default=0, help="direct: seconds between waves")
    p.add_argument("--workers", type=int, default=0, help="direct: parallel requests (default FAST_WORKERS)")
    p.add_argument("--stop-on-block", action="store_true", help="direct: stop when a wave mostly fails")
    p.add_argument("--url", help="api: backend base URL")
    p.add_argument("--timeout", type=float, default=20, help="api: minutes before cancelling a job")
    p.add_argument("--out", help="JSON report path")
    args = p.parse_args()
    if args.mode == "api" and not args.url:
        p.error("api mode needs --url")

    pool = load_numbers(args.numbers)
    report = {"started": datetime.now().isoformat(timespec="seconds"), "mode": args.mode, "carriers": {}}
    for carrier in CARRIERS:
        count = getattr(args, carrier)
        if not count:
            continue
        numbers = take(pool[carrier], count, repeat=args.mode == "direct")
        if not numbers:
            print(f"{carrier}: no numbers in {args.numbers}, skipped")
            continue
        print(f"\n{carrier.upper()}: {len(numbers)} lookups over {len(set(numbers))} unique numbers")
        run = run_direct if args.mode == "direct" else run_api
        report["carriers"][carrier] = r = run(carrier, numbers, args)
        print(f"{carrier.upper()}: {r['lookups']} in {r['seconds']}s = {r['per_second']}/s "
              f"(~{r['lookups_per_hour']}/h)  ok {r['ok']}  not-found {r['not_found']}  "
              f"failed {r['failed']} ({r['failed_pct']}%)")
        print(f"  daily target {r['daily_target']}: "
              + (f"~{r['hours_for_daily_target']} h of fetching" if r["hours_for_daily_target"] else "n/a"))
        for e, n in list(r["errors"].items())[:5]:
            print(f"  {n:>5} x {e}")

    out = Path(args.out or Path(args.numbers).with_name(
        f"loadtest-{args.mode}-{datetime.now():%Y%m%d-%H%M}.json"))
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport: {out}")


if __name__ == "__main__":
    main()
