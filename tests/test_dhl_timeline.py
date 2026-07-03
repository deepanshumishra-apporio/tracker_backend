"""
Unit test for DHL shipment-timeline parsing, using event cards captured from the
live page (2026-07). No browser/network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Status  # noqa: E402
from scrapers.dhl import _parse_timeline, _status_from_title  # noqa: E402

_CARDS = [
    {"date": "June 30, 2026", "title": "Delivered",
     "meta": "10:25 AM (UTC +01:00) | CABINTEELY - IRELAND, REPUBLIC OF"},
    {"date": "June 30, 2026", "title": "Shipment is out with courier for delivery",
     "meta": "8:28 AM (UTC +01:00) | DUBLIN - IRELAND, REPUBLIC OF"},
    {"date": "June 29, 2026", "title": "Delivery attempted but no response at Consignee address",
     "meta": "9:32 AM (UTC +01:00) | CABINTEELY - IRELAND, REPUBLIC OF"},
    {"date": "June 27, 2026", "title": "Processed at DELHI (NEW DELHI) - INDIA",
     "meta": "12:24 PM (UTC +05:30) | DELHI (NEW DELHI) - INDIA"},
]


def test_parses_all_cards_newest_first():
    events = _parse_timeline(_CARDS)
    assert len(events) == 4
    assert events[0].description == "Delivered"
    assert events[0].location == "CABINTEELY - IRELAND, REPUBLIC OF"
    assert events[0].status == Status.DELIVERED
    assert events[0].timestamp.month == 6 and events[0].timestamp.day == 30
    assert events[0].timestamp.hour == 10 and events[0].timestamp.minute == 25
    # last card
    assert events[-1].description.startswith("Processed at DELHI")
    assert events[-1].status == Status.IN_TRANSIT


def test_status_from_title():
    assert _status_from_title("Delivered") == Status.DELIVERED
    assert _status_from_title("Shipment is out with courier for delivery") == Status.OUT_FOR_DELIVERY
    assert _status_from_title("Delivery attempted but no response") == Status.EXCEPTION
    assert _status_from_title("Arrived at DHL Sort Facility") == Status.IN_TRANSIT


def test_empty_timeline():
    assert _parse_timeline([]) == []
