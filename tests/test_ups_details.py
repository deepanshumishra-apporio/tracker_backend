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
from scrapers.ups import (  # noqa: E402
    UPSScraper,
    _EXPAND_JS,
    _EXTRACT_JS,
    _clean_service,
    _parse_pod_dt,
)


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
import config  # noqa: E402
from scrapers.base import Blocked, humanize_error  # noqa: E402
from scrapers.ups import (  # noqa: E402
    _INVALID,
    _READY_JS,
    _THROTTLED,
    _page_verdict,
)

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
    assert _page_verdict(_Body(_REFUSAL_BODY)) == "refused"


def test_a_real_shipment_page_is_not_mistaken_for_a_throttle():
    assert _page_verdict(_Body("Delivered\nWATERLOO, CA\nParcel History")) is None


def test_refusal_check_survives_a_dead_browser():
    class _Dead:
        def get_text(self, _sel):
            raise RuntimeError("invalid session id")

    assert _page_verdict(_Dead()) is None


def test_throttle_message_blames_the_ip_not_the_number():
    """The old wording sent us hunting for a parser bug that didn't exist."""
    assert "rate-limiting this IP" in _THROTTLED
    assert "proxy" in _THROTTLED.lower()
    assert "invalid number" not in _THROTTLED.lower()


@pytest.fixture()
def no_proxy(monkeypatch):
    """Pin config to proxy-less, where a refusal is reported, not retried."""
    monkeypatch.setattr(config, "USE_PROXIES", False)
    monkeypatch.setattr(config, "PROXY_URL", "")


@pytest.fixture()
def with_proxy(monkeypatch):
    """Pin config to a rotating gateway, where a retry gets a new exit IP."""
    monkeypatch.setattr(config, "USE_PROXIES", True)
    monkeypatch.setattr(config, "PROXY_URL", "user:pass@gate.example:7000")


def test_throttled_lookup_reports_the_real_cause(no_proxy):
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


# ---------------------------------------------------------------------------
# The wait itself. Rows 2-26 of the 2026-09 bulk run all failed with
# "no status found (invalid number or page changed)" on well-formed waybills,
# one row after an identical number tracked fine. Two causes, both here:
# the wait returned on an empty headline, and a refusal reached that same
# branch and was reported as a bad waybill.
# ---------------------------------------------------------------------------
class _RenderingSB:
    """A page whose headline node exists before its text does.

    UPS's Angular app inserts #stApp_nameKey and fills it a beat later; the old
    wait broke on the node's mere existence and read it empty.
    """

    def __init__(self, headline: str, *, empty_polls: int, body: str = ""):
        self.headline = headline
        self.empty_polls = empty_polls
        self.body = body
        self.polls = 0
        self.sleeps: list[float] = []

    # The extractor reads the headline; the ready-check reports whether it has
    # text yet. Distinguish by the ready script's own marker.
    def execute_script(self, script: str):
        if "'ready'" in script:
            self.polls += 1
            if self.polls <= self.empty_polls:
                return ""              # node there, textContent still empty
            return "ready"
        if "stApp_nameKey" in script:
            # Mirror the page: the headline reads empty until it has filled in.
            filled = self.polls > self.empty_polls
            return json.dumps({
                "status": (self.headline or None) if filled else None,
                "steps": [], "last_location": None,
            })
        return None

    def sleep(self, seconds):
        self.sleeps.append(seconds)

    def get_text(self, _selector):
        return self.body


def test_the_wait_holds_out_for_the_headline_text():
    """The regression: an empty headline is not "no status found"."""
    sb = _RenderingSB("Delivered", empty_polls=3)
    result = UPSScraper().parse_dom(sb, "1ZH40B480424822345")
    assert result.ok is True
    assert result.status is Status.DELIVERED
    assert sb.polls == 4, "should keep polling until the headline has text"


def test_the_wait_polls_often_enough_not_to_sleep_through_the_headline():
    """2s per poll meant every row paid for the interval, not the page."""
    sb = _RenderingSB("Delivered", empty_polls=1)
    UPSScraper().parse_dom(sb, "1ZH40B480424822345")
    assert sb.sleeps and max(sb.sleeps) <= 0.5


def test_the_ready_check_looks_at_text_not_the_node():
    """Guard the fix at the source: presence alone must not end the wait."""
    assert "textContent.trim()" in _READY_JS
    assert "!!document.querySelector" not in _READY_JS


def test_a_headline_that_never_fills_is_still_a_failure(no_proxy):
    sb = _RenderingSB("Delivered", empty_polls=999)
    result = UPSScraper().parse_dom(sb, "1ZH40B480424822345")
    assert result.ok is False
    assert sb.polls == 60, "the wait should time out, not loop forever"


def test_a_refusal_that_renders_the_headline_still_blames_the_ip(no_proxy):
    """The case the old code missed: refusal reached the no-status branch."""
    sb = _RenderingSB("", empty_polls=999, body=_REFUSAL_BODY)
    result = UPSScraper().parse_dom(sb, "1ZH40B480424822345")
    assert result.ok is False
    assert "rate-limiting this IP" in result.error
    assert "invalid number" not in result.error


def test_the_wait_gives_up_as_soon_as_the_page_refuses(no_proxy):
    """A refused lookup must not burn the full 30s wait for a headline."""

    class _RefusingSB(_RenderingSB):
        def execute_script(self, script):
            if "'ready'" in script:
                self.polls += 1
                return "refused"
            return super().execute_script(script)

    sb = _RefusingSB("", empty_polls=999, body=_REFUSAL_BODY)
    result = UPSScraper().parse_dom(sb, "1ZH40B480424822345")
    assert result.ok is False
    assert "rate-limiting this IP" in result.error
    assert sb.polls == 1


def test_a_refusal_is_retried_when_a_proxy_can_change_the_ip(with_proxy):
    """Raising hands the row back to BrowserSession, which relaunches."""
    sb = _RenderingSB("", empty_polls=999, body=_REFUSAL_BODY)
    with pytest.raises(Blocked) as caught:
        UPSScraper().parse_dom(sb, "1ZH40B480424822345")
    assert "rate-limiting this IP" in str(caught.value)


# ---------------------------------------------------------------------------
# UPS's invalid-number page is NOT its throttle page. Verbatim from the live
# site for 55124671 — an 8-digit reference number the sheet listed under UPS.
# Both pages carry the form's "0 of 25 tracking numbers entered" counter, which
# is why reading that as a rate limit told the user to buy a proxy for a
# waybill UPS had simply rejected.
# ---------------------------------------------------------------------------
_INVALID_BODY = """Track a Package
warning
Invalid tracking number
expand_less
Toggle Message Content
This tracking number may be invalid or not active yet. Please check with the sender.
Tracking Number
                                 photo_camera
Scan Barcode
Invalid
Please provide a tracking number.
0 of 25 tracking numbers entered."""


def test_the_two_dead_end_pages_are_told_apart():
    assert _page_verdict(_Body(_INVALID_BODY)) == "invalid"
    assert _page_verdict(_Body(_REFUSAL_BODY)) == "refused"
    assert _page_verdict(_Body("Delivered\nWATERLOO, CA\nParcel History")) is None


def test_the_form_counter_alone_is_not_a_throttle():
    """It shows on every UPS error page — the marker that caused the mixup."""
    assert _page_verdict(_Body("0 of 25 tracking numbers entered.")) is None


def test_an_invalid_number_is_reported_as_invalid(with_proxy):
    """Even with a proxy configured: UPS rejected the number, not the caller."""
    sb = _RenderingSB("", empty_polls=999, body=_INVALID_BODY)
    result = UPSScraper().parse_dom(sb, "55124671")
    assert result.ok is False
    assert result.error == _INVALID
    assert "rate-limiting" not in result.error
    assert "proxy" not in result.error.lower()


def test_the_invalid_message_says_what_to_check():
    assert "check it with the sender" in _INVALID
    assert "1Z" in _INVALID


def test_an_invalid_page_ends_the_wait_immediately(no_proxy):
    class _InvalidSB(_RenderingSB):
        def execute_script(self, script):
            if "'ready'" in script:
                self.polls += 1
                return "invalid"
            return super().execute_script(script)

    sb = _InvalidSB("", empty_polls=999, body=_INVALID_BODY)
    result = UPSScraper().parse_dom(sb, "55124671")
    assert result.error == _INVALID
    assert sb.polls == 1


def test_the_ready_check_screens_for_both_pages():
    assert "'invalid'" in _READY_JS and "'refused'" in _READY_JS
    assert "0 of 25" not in _READY_JS


def test_a_retried_refusal_keeps_its_explanation_in_the_table():
    """humanize_error must not flatten it to the generic bot-check line."""
    message = humanize_error(Blocked(_THROTTLED))
    assert "rate-limiting this IP" in message
    assert len(message) <= 200


# ---------------------------------------------------------------------------
# Shipment details. Every successful row came back with service, destination,
# weight and signed_by = null: the "Show Details" toggle reads "Show Details
# keyboard_arrow_down", so a matcher demanding exactly "show details" never
# clicked it, and the panel it opens was the only source we read.
# ---------------------------------------------------------------------------
def test_the_expander_matches_ups_wording_with_its_icon():
    """The exact-match bug: the label always carries a ligature suffix."""
    assert "startsWith" in _EXPAND_JS
    assert "shipment details" in _EXPAND_JS
    assert "'hide details'" not in _EXPAND_JS, "clicking hide undoes the show"


# Captured off the live page for 1ZH40B480424822345 (2026-09): the label/value
# element pairs UPS renders from first paint.
_LIVE_FIELDS = [
    ["Delivered To", "WATERLOO, CA"],
    ["Received By", "BOSS"],
    ["Service", "UPS Worldwide Express Saver®"],
    ["Shipment Category", "Package"],
    ["Shipped / Billed On", "07/01/2026"],
]


def test_the_page_ids_fill_the_fields_that_used_to_come_back_null():
    result = _scrape({
        "status": "Delivered",
        "steps": [],
        "last_location": None,
        "fields": _LIVE_FIELDS,
    })
    assert result.service == "UPS Worldwide Express Saver"
    assert result.destination == "WATERLOO, CA"
    assert result.signed_by == "BOSS"
    assert result.details["Shipment Category"] == "Package"
    # The raw value keeps UPS's mangled ® — the details map gets it cleaned too.
    assert result.details["Service"] == "UPS Worldwide Express Saver"
    assert result.details["Shipped / Billed On"] == "07/01/2026"


def test_a_field_we_have_no_column_for_is_still_reported():
    result = _scrape({
        "status": "Delivered", "steps": [], "last_location": None,
        "fields": [["Some New UPS Field", "42"]],
    })
    assert result.details["Some New UPS Field"] == "42"


def test_the_proof_of_delivery_panel_supplies_the_delivery_time():
    result = _scrape({
        "status": "Delivered", "steps": [], "last_location": None,
        "fields": [["Delivered On", "07/03/2026 10:05 A.M."],
                   ["Service", "UPS Worldwide Express Saver"]],
    })
    assert result.delivered_at.month == 7 and result.delivered_at.day == 3
    assert result.delivered_at.hour == 10 and result.delivered_at.minute == 5


def test_ship_to_and_delivered_to_both_mean_destination():
    for label in ("Ship To", "Delivered To"):
        result = _scrape({
            "status": "On the Way", "steps": [], "last_location": None,
            "fields": [[label, "MORGANTON, NC US"]],
        })
        assert result.destination == "MORGANTON, NC US"


def test_the_details_panel_still_wins_when_it_is_open():
    """The older "Shipment Details" text block remains a working fallback."""
    class _WithPanel(_FakeSB):
        def get_text(self, _sel):
            return chr(10).join([
                "Shipment Details", "Ship To", "MORGANTON, NC US",
                "Service", "UPS Worldwide Express Saver", "Weight", "2.0 KGS",
                "Support",
            ])

    sb = _WithPanel({
        "status": "Delivered", "steps": [], "last_location": None,
        "fields": [],   # the older page variant exposes no field ids
    })
    result = UPSScraper().parse_dom(sb, "1ZH40B480439305840")
    assert result.destination == "MORGANTON, NC US"
    assert result.weight == "2.0 KGS"
    assert result.service == "UPS Worldwide Express Saver"


def test_the_extractor_reads_the_ids_the_live_page_actually_has():
    for element_id in ("stApp_lblShipTo", "stApp_txtAddress", "stApp_txtCountry",
                       "stApp_valReceivedBy", "stApp_link_AdditionalInfoService",
                       "stApp_txtAdditionalInfoBilledOn",
                       "stApp_PODtxtService", "stApp_PODtxtDeliveredOn"):
        assert element_id in _EXTRACT_JS


def test_the_details_wait_recognises_the_history_modal():
    """Opening it replaces the page text, so the old marker never matched."""
    from scrapers.ups import _DETAILS_READY_JS
    assert "package history" in _DETAILS_READY_JS


def test_a_shipment_with_no_fields_at_all_is_unharmed():
    result = _scrape({"status": "On the Way", "steps": [], "last_location": None})
    assert result.ok is True
    assert result.signed_by is None
    assert result.delivered_at is None


@pytest.mark.parametrize(
    "raw,expected",
    [("UPS Worldwide Express Saver®", "UPS Worldwide Express Saver"),
     ("UPS Worldwide Express Saver�", "UPS Worldwide Express Saver"),
     ("UPS Ground", "UPS Ground"), ("", None), (None, None)],
)
def test_service_names_lose_their_trademark_tail(raw, expected):
    assert _clean_service(raw) == expected


@pytest.mark.parametrize(
    "raw", ["07/03/2026 10:05 A.M.", "07/03/2026, 10:05 AM", "07/03/2026 10:05 a.m."]
)
def test_pod_timestamps_parse(raw):
    assert _parse_pod_dt(raw).hour == 10


def test_pod_timestamp_shrugs_at_junk():
    assert _parse_pod_dt("Delivered") is None
    assert _parse_pod_dt(None) is None
