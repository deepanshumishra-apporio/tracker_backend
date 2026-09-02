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


# ---------------------------------------------------------------------------
# Status derivation — regression tests for the bulk run of 2026-09.
# ---------------------------------------------------------------------------
import json  # noqa: E402
import re  # noqa: E402

import pytest  # noqa: E402

from models import Carrier  # noqa: E402
from scrapers.ups import UPSScraper, _EXTRACT_JS  # noqa: E402


class _FakeSB:
    """Returns a canned payload for the extract script; no browser."""

    def __init__(self, payload: dict):
        self.payload = payload

    def execute_script(self, script: str):
        # The extract script returns JSON; the later "Show Details" click script
        # returns nothing. Distinguish by looking for the extractor's marker.
        if "stApp_nameKey" in script:
            return json.dumps(self.payload)
        return None

    def sleep(self, _seconds):
        pass

    def get_text(self, _selector):
        return ""


def _scrape(payload: dict):
    return UPSScraper().parse_dom(_FakeSB(payload), "1ZH40B480439305840")


def test_headline_status_is_used_when_it_maps():
    result = _scrape({"status": "Delivered", "steps": [], "last_location": None})
    assert result.status is Status.DELIVERED
    assert result.ok is True


def test_unmappable_headline_falls_back_to_the_newest_event():
    """Row 20 of the production run: headline Unknown, history said Delivered."""
    result = _scrape({
        "status": "Your parcel update",          # maps to nothing
        "last_location": None,
        "steps": [{"label": "Delivered", "state": "completed"}],
    })
    assert result.status is Status.DELIVERED


def test_fallback_prefers_a_known_status_over_unknown_events():
    result = _scrape({
        "status": "Your parcel update",
        "last_location": None,
        "steps": [
            {"label": "Some unmapped label", "state": "completed"},
            {"label": "On the Way", "state": "active"},
        ],
    })
    assert result.status is Status.IN_TRANSIT


def test_status_stays_unknown_when_nothing_maps():
    result = _scrape({
        "status": "Your parcel update",
        "last_location": None,
        "steps": [{"label": "Some unmapped label", "state": "completed"}],
    })
    assert result.status is Status.UNKNOWN


def test_missing_headline_is_still_a_failure():
    result = _scrape({"status": None, "steps": [], "last_location": None})
    assert result.ok is False
    assert "no status found" in result.error


# The extract script runs in the browser, so mirror its txt() filter here and
# test the logic directly. Regression: an earlier version removed every <i> in
# the subtree, which erased the headline itself and made every UPS row fail with
# "no status found".
_LIGATURE = re.compile(
    re.search(r"const LIGATURE = /(.+?)/;", _EXTRACT_JS).group(1)
)


def _txt(raw: str):
    """Python mirror of the extract script's txt() helper."""
    raw = " ".join(raw.split()).strip()
    if not raw:
        return None
    kept = " ".join(w for w in raw.split(" ") if not _LIGATURE.match(w)).strip()
    return kept or raw


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Delivered check_circle", "Delivered"),
        ("check_circle Delivered", "Delivered"),
        ("Out For Delivery Today local_shipping", "Out For Delivery Today"),
        ("Delivered", "Delivered"),
        ("On the Way", "On the Way"),
        ("Label Created", "Label Created"),
        ("Delivered to UPS Access Point", "Delivered to UPS Access Point"),
        ("", None),
    ],
)
def test_icon_ligatures_are_dropped_from_the_status(raw, expected):
    assert _txt(raw) == expected


@pytest.mark.parametrize(
    "raw", ["Delivered", "check_circle", "On the Way", "In Transit", "x"]
)
def test_filtering_never_empties_a_non_empty_status(raw):
    """The regression that broke every UPS row: stripping returned nothing."""
    assert _txt(raw), "a non-empty headline must never filter down to nothing"


def test_the_extractor_no_longer_deletes_dom_nodes():
    """Removing every <i> in the subtree is what erased the headline."""
    assert "querySelectorAll(ICONS)" not in _EXTRACT_JS
    assert "LIGATURE" in _EXTRACT_JS


# ---------------------------------------------------------------------------
# UPS throttling — the page renders, but the lookup is refused.
# ---------------------------------------------------------------------------
from scrapers.ups import _refused, _THROTTLED  # noqa: E402

# Verbatim from the live page while UPS was rate-limiting the server's IP.
_REFUSAL_BODY = """Track a Package
warning
Tracking Error
Toggle Message Content
We are unable to complete your tracking request at this time. Please try again later.
Tracking Number
                                 Invalid
Please provide a tracking number.
0 of 25 tracking numbers entered."""


class _Body:
    def __init__(self, text):
        self.text = text

    def get_text(self, _sel):
        return self.text


def test_recognizes_the_throttle_page():
    assert _refused(_Body(_REFUSAL_BODY)) is True


def test_a_real_shipment_page_is_not_mistaken_for_a_throttle():
    assert _refused(_Body("Delivered\nWATERLOO, CA\nParcel History")) is False


def test_refusal_check_survives_a_dead_browser():
    class _Dead:
        def get_text(self, _sel):
            raise RuntimeError("invalid session id")

    assert _refused(_Dead()) is False


def test_throttle_message_blames_the_ip_not_the_number():
    """The old wording sent us hunting for a parser bug that didn't exist."""
    assert "rate-limiting this IP" in _THROTTLED
    assert "proxy" in _THROTTLED.lower()
    assert "invalid number" not in _THROTTLED.lower()


def test_throttled_lookup_reports_the_real_cause():
    class _Throttled(_FakeSB):
        def __init__(self):
            super().__init__({})

        def execute_script(self, script):
            if "stApp_nameKey" in script:
                return False   # the headline never appears
            return None

        def get_text(self, _sel):
            return _REFUSAL_BODY

    result = UPSScraper().parse_dom(_Throttled(), "1ZH40B480424822345")
    assert result.ok is False
    assert "rate-limiting this IP" in result.error
    assert "no status found" not in result.error
