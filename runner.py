"""
Batch runner — entry point for the "medium volume" job you described.

Reads tracking numbers from a CSV (columns: carrier,tracking_number),
scrapes each with SeleniumBase UC Mode (stealth + proxy + retry), and saves
normalized results to SQLite.

Usage:
    python runner.py numbers.csv

CSV example (numbers.csv):
    carrier,tracking_number
    dhl,1234567890
    fedex,987654321098
    ups,1Z999AA10123456784
    aramex,4567891234
"""
from __future__ import annotations

import csv
import sys

import config
import storage
from models import Carrier, TrackingResult
from scrapers.aramex import AramexScraper
from scrapers.dhl import DHLScraper
from scrapers.fedex import FedExScraper
from scrapers.ups import UPSScraper

# Registry: carrier -> scraper class. Add carriers here as you build them.
SCRAPERS = {
    Carrier.ARAMEX: AramexScraper,
    Carrier.DHL: DHLScraper,
    Carrier.FEDEX: FedExScraper,
    Carrier.UPS: UPSScraper,
}


def load_jobs(path: str) -> list[tuple[Carrier, str]]:
    jobs: list[tuple[Carrier, str]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            carrier_raw = (row.get("carrier") or "").strip().lower()
            number = (row.get("tracking_number") or "").strip()
            if not carrier_raw or not number:
                continue
            try:
                jobs.append((Carrier(carrier_raw), number))
            except ValueError:
                print(f"  ! unknown carrier '{carrier_raw}' — skipping {number}")
    return jobs


def run(path: str) -> None:
    storage.init_db()
    jobs = load_jobs(path)
    print(f"Loaded {len(jobs)} tracking numbers.")
    print(f"Proxies: {'ON' if config.proxy_or_none() else 'OFF (expect blocks at volume)'}\n")

    # One scraper instance per carrier; each scrape() opens its own stealth
    # browser session (fresh fingerprint + rotated IP with a gateway proxy).
    instances = {c: cls() for c, cls in SCRAPERS.items()}

    ok = fail = 0
    for i, (carrier, number) in enumerate(jobs, 1):
        scraper = instances[carrier]
        print(f"[{i}/{len(jobs)}] {carrier.value} {number} ...", end=" ", flush=True)
        try:
            result = scraper.scrape(number)
        except Exception as e:  # blocked after retries, timeout, etc.
            result = TrackingResult.failure(number, carrier, str(e))

        storage.save(result)
        if result.ok:
            ok += 1
            print(f"OK -> {result.status.value} ({len(result.events)} events)")
        else:
            fail += 1
            print(f"FAIL -> {result.error}")

        if i < len(jobs):
            scraper.polite_delay()

    print(f"\nDone. {ok} ok, {fail} failed. Results in {config.DB_PATH}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python runner.py <numbers.csv>")
        sys.exit(1)
    run(sys.argv[1])
