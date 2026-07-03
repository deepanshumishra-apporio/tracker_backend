"""
Unit test for UPS "Shipment Details" parsing, using text captured from the live
"Show Details" panel (2026-07). No browser/network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Status  # noqa: E402
from scrapers.ups import _parse_ups_details, _parse_ups_history  # noqa: E402

_REAL = """Delivery
inactive
Show Details keyboard_arrow_down
Shipment Details
Ship To
MORGANTON, NC US
Service
UPS Worldwide Express Saver®
Shipment Category
Package
Shipped / Billed On
06/30/2026
Support
Help and Support Center""".split("\n")


def test_parses_service_destination_and_extras():
    out = _parse_ups_details(_REAL)
    assert out["service"] == "UPS Worldwide Express Saver"
    assert out["destination"] == "MORGANTON, NC US"
    assert out["details"]["Shipment Category"] == "Package"
    assert out["details"]["Shipped / Billed On"] == "06/30/2026"
    assert out["weight"] is None  # not shown for this shipment


def test_extracts_weight_when_present():
    out = _parse_ups_details(["Shipment Details", "Weight", "2.0 KGS", "Support"])
    assert out["weight"] == "2.0 KGS"


def test_no_details_block():
    assert _parse_ups_details(["On the Way", "active"])["service"] is None


# Verbatim slice of the live Parcel History (delivered shipment, 2026-07).
_HISTORY = """07/02/2026
5:40 P.M.
Delivered
DELIVERED
EDMONTON, CA
07/02/2026
8:40 A.M.
Out for Delivery
Out For Delivery Today
Edmonton, AB, Canada
07/02/2026
4:45 A.M.
Processing at UPS Facility
Edmonton, AB, Canada
06/30/2026
9:18 P.M.
Departed from Facility
Calgary, AB, Canada""".split("\n")


def test_parses_full_parcel_history():
    events = _parse_ups_history(_HISTORY)
    assert len(events) == 4
    # Newest-first, headline used as description.
    assert events[0].description == "Delivered"
    assert events[0].location == "EDMONTON, CA"
    assert events[0].status == Status.DELIVERED
    assert events[0].timestamp.month == 7 and events[0].timestamp.day == 2
    assert events[0].timestamp.hour == 17 and events[0].timestamp.minute == 40

    # Facility scans (not in the milestone map) map to in-transit, not unknown.
    assert events[2].description == "Processing at UPS Facility"
    assert events[2].status == Status.IN_TRANSIT
    assert events[2].location == "Edmonton, AB, Canada"
    assert events[1].status == Status.OUT_FOR_DELIVERY


def test_history_empty_without_rows():
    assert _parse_ups_history(["Shipment Details", "Service", "UPS Worldwide"]) == []
