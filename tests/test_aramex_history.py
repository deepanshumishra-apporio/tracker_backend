"""
Unit test for Aramex history-table parsing, using rows captured from the live
details page (2026-07). No browser/network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models import Status  # noqa: E402
from scrapers.aramex import _parse_history_table, _parse_dt  # noqa: E402

# Verbatim cell arrays from the live table (note the wrapped whitespace in dates).
_ROWS = [
    ["", "Date", "Location", "Activity"],
    ["", "03 Jul 26\n   \n   04:19", "BOM - HUB, India", "There's a delay at the origin facility"],
    ["", "01 Jul 26\n   \n   13:54", "Surat, India", "Shipment collected from the shipper"],
    ["", "30 Jun 26\n   \n   19:31", "Gurgaon, India", "Shipper generated a new shipment label"],
]


def test_parses_history_rows_and_skips_header():
    events = _parse_history_table(_ROWS)
    assert len(events) == 3  # header skipped
    assert events[0].location == "BOM - HUB, India"
    assert events[0].description.startswith("There's a delay")
    assert events[0].status == Status.EXCEPTION
    assert events[-1].status == Status.PENDING  # "generated a new shipment label"


def test_date_with_wrapped_time_parses():
    dt = _parse_dt("03 Jul 26\n   \n   04:19")
    assert dt is not None
    assert dt.day == 3 and dt.hour == 4 and dt.minute == 19


def test_empty_table_returns_empty():
    assert _parse_history_table([]) == []
    assert _parse_history_table([["", "Date", "Location", "Activity"]]) == []
