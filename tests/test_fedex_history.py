"""
Unit test for FedEx travel-history parsing, using text captured from the live
"View more details" panel (2026-07). No browser/network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Status  # noqa: E402
from scrapers.fedex import (  # noqa: E402
    _DT_AT_RE,
    _SIGNED_RE,
    _event_status,
    _parse_scan_dt,
    _parse_shipment_facts,
    _parse_travel_history,
)

_REAL = """Tracking ID:
873607673938
Local Scan Time
Travel history
Sort by:
Ascending
Descending
Friday, 6/26/26
7:17 AM
Shipment information sent to FedEx
Saturday, 6/27/26
7:46 PM
Picked up
NEW DELHI IN
9:13 PM
Left FedEx origin facility
NEW DELHI IN
Wednesday, 7/1/26
5:02 AM
At destination sort facility
BEN GURION AIRPORT IL
OUR COMPANY
About FedEx""".split("\n")


def test_parses_full_history_newest_first():
    events = _parse_travel_history(_REAL)
    # 4 scans parsed, newest first.
    assert len(events) == 4
    assert events[0].description == "At destination sort facility"
    assert events[0].location == "BEN GURION AIRPORT IL"
    assert events[-1].description == "Shipment information sent to FedEx"

    # Date headers propagate into timestamps.
    assert events[0].timestamp is not None
    assert events[0].timestamp.month == 7 and events[0].timestamp.day == 1
    assert events[-1].timestamp.month == 6 and events[-1].timestamp.day == 26

    # Location-less first scan is handled.
    assert events[-1].location is None
    # Status is inferred from the description.
    assert events[-1].status == Status.PENDING


def test_returns_empty_without_history_section():
    assert _parse_travel_history(["Tracking ID:", "123", "ESTIMATED DELIVERY"]) == []


def test_delivered_layout_signals():
    # Delivered shipments render "Tuesday, 6/09/2026 at 11:23 am" + "Signed for by:".
    m = _DT_AT_RE.search("Tuesday, 6/09/2026 at 11:23 am")
    assert m is not None
    dt = _parse_scan_dt(m.group(1), m.group(2))
    assert dt is not None and dt.month == 6 and dt.day == 9 and dt.hour == 11

    sm = _SIGNED_RE.search("Signed for by: X.HAFED")
    assert sm and sm.group(1).strip() == "X.HAFED"


# Verbatim from the live "Shipment facts" panel.
_FACTS = """Shipment facts
Shipment overview
TRACKING NUMBER 873671747151
SHIP DATE
Will be updated soon
Services
SERVICE FedEx International Priority
TERMS Shipper
SPECIAL HANDLING SECTION Deliver Weekday
Package details
WEIGHT 1.54 lbs / 0.7 kgs
DIMENSIONS 23x23x12 cms
TOTAL PIECES 1
TOTAL SHIPMENT WEIGHT 1.54 lbs / 0.7 kgs
PACKAGING Your Packaging""".split("\n")


def test_parses_shipment_facts():
    f = _parse_shipment_facts(_FACTS)
    assert f["service"] == "FedEx International Priority"
    assert f["weight"] == "1.54 lbs / 0.7 kgs"
    assert f["pieces"] == 1
    assert f["details"]["Dimensions"] == "23x23x12 cms"
    assert f["details"]["Packaging"] == "Your Packaging"
    assert f["details"]["Terms"] == "Shipper"
    assert f["details"]["Special handling"] == "Deliver Weekday"


def test_facts_skips_placeholder_and_missing():
    f = _parse_shipment_facts(["Shipment facts", "SHIP DATE", "Will be updated soon"])
    assert f["service"] is None and f["weight"] is None and f["pieces"] is None


def test_event_status_mapping():
    assert _event_status("Delivered") == Status.DELIVERED
    assert _event_status("Delivery exception") == Status.EXCEPTION
    assert _event_status("On the way") == Status.IN_TRANSIT
    assert _event_status("Shipment information sent to FedEx") == Status.PENDING
