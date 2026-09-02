"""One-off: scrape a single FedEx tracking number and print the result as JSON.

Usage:
    python scrape_one.py 873815709010
"""
import sys, json
import config
from scrapers.fedex import FedExScraper

number = sys.argv[1] if len(sys.argv) > 1 else "873815709010"
print(f"Proxy: {'ON -> ' + (config.PROXY_URL) if config.proxy_or_none() else 'OFF'}")
print(f"Scraping FedEx {number} ...\n")

result = FedExScraper().scrape(number)
print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
