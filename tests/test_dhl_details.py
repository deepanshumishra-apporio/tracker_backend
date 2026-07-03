"""
Unit test for DHL shipment-details parsing, using the exact text captured from
the live page (2026-07). No browser/network needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapers.dhl import _parse_shipment_details  # noqa: E402

# Verbatim from the live DHL Express page.
_REAL = """Service
EXPRESS WORLDWIDE
To protect your privacy, the Proof of Delivery is available after validation
Electronic Proof of Delivery
1 Piece ID
JD014600012674098596
Waybill Number
1790531772"""


def test_parses_service_pieces_and_details():
    service, weight, pieces, details = _parse_shipment_details(_REAL)
    assert service == "EXPRESS WORLDWIDE"
    assert pieces == 1
    assert details["Piece ID"] == "JD014600012674098596"
    assert details["Waybill Number"] == "1790531772"
    # Weight is gated behind DHL's identity check → not present publicly.
    assert weight is None


def test_parses_weight_when_present():
    _, weight, _, _ = _parse_shipment_details("Weight\n2.5 kg\nService\nEXPRESS")
    assert weight == "2.5 kg"


def test_handles_empty_and_none():
    assert _parse_shipment_details(None) == (None, None, None, {})
    assert _parse_shipment_details("") == (None, None, None, {})
