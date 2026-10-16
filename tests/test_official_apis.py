"""
Tests for the DHL / FedEx official-API fetchers (fast/dhl.py, fast/fedex.py):
parsing saved responses, HTTP status handling, and that they only switch on
when keyed. No network: the curl_cffi session is faked.
"""
from __future__ import annotations

from datetime import datetime

import pytest

import config
import tracking_service
from fast import dhl as fast_dhl
from fast import fedex as fast_fedex
from fast.http import TransientError
from models import Carrier, Status

DHL_PAYLOAD = {
    "shipments": [{
        "id": "1790531772",
        "service": "express",
        "origin": {"address": {"addressLocality": "NEW DELHI - INDIA", "countryCode": "IN"}},
        "destination": {"address": {"addressLocality": "CABINTEELY - IRELAND", "countryCode": "IE"}},
        "status": {
            "timestamp": "2026-06-30T10:25:00+01:00",
            "location": {"address": {"addressLocality": "CABINTEELY - IRELAND"}},
            "statusCode": "delivered", "status": "DELIVERED",
            "description": "Delivered",
        },
        "details": {
            "product": {"productName": "EXPRESS WORLDWIDE"},
            "totalNumberOfPieces": 1,
            "pieceIds": ["JD014600012674098596"],
            "weight": {"value": 0.5, "unitText": "kg"},
            "proofOfDelivery": {"signed": {"name": "MURPHY"}},
        },
        "events": [
            {"timestamp": "2026-06-29T08:00:00+01:00", "statusCode": "transit",
             "description": "Shipment is out with courier for delivery",
             "location": {"address": {"addressLocality": "DUBLIN - IRELAND"}}},
            {"timestamp": "2026-06-30T10:25:00+01:00", "statusCode": "delivered",
             "description": "Delivered",
             "location": {"address": {"addressLocality": "CABINTEELY - IRELAND"}}},
        ],
    }]
}

FEDEX_PAYLOAD = {
    "output": {"completeTrackResults": [
        {"trackingNumber": "382195828808", "trackResults": [{
            "latestStatusDetail": {"code": "DL", "derivedCode": "DL",
                                   "statusByLocale": "Delivered", "description": "Delivered"},
            "dateAndTimes": [
                {"type": "ACTUAL_DELIVERY", "dateTime": "2026-07-04T11:23:00-04:00"},
                {"type": "SHIP", "dateTime": "2026-07-01T00:00:00+05:30"},
            ],
            "originLocation": {"locationContactAndAddress": {"address": {
                "city": "NEW DELHI", "countryCode": "IN"}}},
            "recipientInformation": {"address": {
                "city": "NEW YORK", "stateOrProvinceCode": "NY", "countryCode": "US"}},
            "serviceDetail": {"description": "FedEx International Priority"},
            "packageDetails": {"count": "1", "weightAndDimensions": {
                "weight": [{"value": "1.5", "unit": "LB"}]}},
            "deliveryDetails": {"receivedByName": "J.SMITH"},
            "scanEvents": [
                {"date": "2026-07-04T11:23:00-04:00", "eventDescription": "Delivered",
                 "derivedStatusCode": "DL",
                 "scanLocation": {"city": "NEW YORK", "stateOrProvinceCode": "NY", "countryCode": "US"}},
                {"date": "2026-07-04T08:00:00-04:00", "eventDescription": "On FedEx vehicle for delivery",
                 "derivedStatusCode": "OD", "scanLocation": {"city": "NEW YORK"}},
            ],
        }]},
        {"trackingNumber": "382192985988", "trackResults": [{
            "error": {"code": "TRACKING.TRACKINGNUMBER.NOTFOUND",
                      "message": "Tracking number cannot be found."},
        }]},
    ]}
}


class FakeResp:
    def __init__(self, status: int, body=None):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Answers each (method, url-suffix) with a queue of FakeResps."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, suffix), queue in self.routes.items():
            if m == method and url.endswith(suffix):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)


# ---------------------------------------------------------------------------
# DHL
# ---------------------------------------------------------------------------
def test_dhl_parses_unified_payload():
    r = fast_dhl.parse("1790531772", DHL_PAYLOAD)
    assert r.ok and r.status is Status.DELIVERED
    assert r.origin == "NEW DELHI - INDIA, IN" and r.destination == "CABINTEELY - IRELAND, IE"
    assert r.service == "EXPRESS WORLDWIDE" and r.pieces == 1 and r.weight == "0.5 kg"
    assert r.signed_by == "MURPHY"
    assert r.delivered_at == datetime.fromisoformat("2026-06-30T10:25:00+01:00")
    # newest first; the coarse 'transit' code is refined from the wording
    assert [e.status for e in r.events] == [Status.DELIVERED, Status.OUT_FOR_DELIVERY]


def test_dhl_empty_payload_is_not_found():
    r = fast_dhl.parse("123", {"shipments": []})
    assert not r.ok and "no shipment" in r.error


def _dhl(monkeypatch, routes):
    fake = FakeSession(routes)
    monkeypatch.setattr(fast_dhl, "session", lambda: fake)
    return fast_dhl.DHLFast(api_key="k", min_interval=0), fake


def test_dhl_fetcher_statuses(monkeypatch):
    fetcher, fake = _dhl(monkeypatch, {("GET", "/track/shipments"): [
        FakeResp(200, DHL_PAYLOAD), FakeResp(404, {"status": 404}), FakeResp(429)]})
    results, transient = fetcher.track_many(["1790531772", "111", "222"])
    assert results["1790531772"].status is Status.DELIVERED
    assert not results["111"].ok and "no shipment" in results["111"].error
    assert "rate limit" in transient["222"]          # left for the browser
    assert fake.calls[0][2]["headers"]["DHL-API-Key"] == "k"
    assert fake.calls[0][2]["params"] == {"trackingNumber": "1790531772"}


def test_dhl_bad_key_is_transient(monkeypatch):
    fetcher, _ = _dhl(monkeypatch, {("GET", "/track/shipments"): [FakeResp(401)]})
    results, transient = fetcher.track_many(["1"])
    assert not results and "DHL_API_KEY" in transient["1"]


# ---------------------------------------------------------------------------
# FedEx
# ---------------------------------------------------------------------------
def test_fedex_parses_track_api_payload():
    got = fast_fedex.parse(FEDEX_PAYLOAD)
    r = got["382195828808"]
    assert r.ok and r.status is Status.DELIVERED
    assert r.origin == "NEW DELHI, IN" and r.destination == "NEW YORK, NY, US"
    assert r.service == "FedEx International Priority"
    assert r.weight == "1.5 LB" and r.pieces == 1 and r.signed_by == "J.SMITH"
    assert r.delivered_at == datetime.fromisoformat("2026-07-04T11:23:00-04:00")
    assert [e.status for e in r.events] == [Status.DELIVERED, Status.OUT_FOR_DELIVERY]
    nf = got["382192985988"]
    assert not nf.ok and "no record" in nf.error


def _fedex(monkeypatch, routes):
    fake = FakeSession(routes)
    monkeypatch.setattr(fast_fedex, "session", lambda: fake)
    return fast_fedex.FedExFast(key="k", secret="s", base="https://apis.example"), fake


TOKEN = FakeResp(200, {"access_token": "tok", "expires_in": 3600})


def test_fedex_fetcher_batches_and_reuses_token(monkeypatch):
    fetcher, fake = _fedex(monkeypatch, {
        ("POST", "/oauth/token"): [TOKEN],
        ("POST", "/track/v1/trackingnumbers"): [FakeResp(200, FEDEX_PAYLOAD)],
    })
    numbers = ["382195828808", "382192985988"] + [f"9{i:011d}" for i in range(31)]
    results, transient = fetcher.track_many(numbers)
    assert results["382195828808"].status is Status.DELIVERED
    assert not results["382192985988"].ok
    # the 31 extra numbers were absent from the fake answer -> transient
    assert len(transient) == 31
    track_calls = [c for c in fake.calls if c[1].endswith("/trackingnumbers")]
    assert len(track_calls) == 2                       # 33 numbers -> 30 + 3
    assert len(track_calls[0][2]["json"]["trackingInfo"]) == 30
    assert sum(1 for c in fake.calls if c[1].endswith("/oauth/token")) == 1
    assert track_calls[0][2]["headers"]["Authorization"] == "Bearer tok"


def test_fedex_refreshes_token_once_on_401(monkeypatch):
    fetcher, fake = _fedex(monkeypatch, {
        ("POST", "/oauth/token"): [TOKEN],
        ("POST", "/track/v1/trackingnumbers"): [FakeResp(401), FakeResp(200, FEDEX_PAYLOAD)],
    })
    results, _ = fetcher.track_many(["382195828808"])
    assert results["382195828808"].ok
    assert sum(1 for c in fake.calls if c[1].endswith("/oauth/token")) == 2


def test_fedex_bad_credentials_are_transient(monkeypatch):
    fetcher, _ = _fedex(monkeypatch, {("POST", "/oauth/token"): [FakeResp(401)]})
    results, transient = fetcher.track_many(["1"])
    assert not results and "FEDEX_API_KEY" in transient["1"]


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("dhl,fedex,expected", [
    ("", ("", ""), set()),
    ("key", ("", ""), {Carrier.DHL}),
    ("", ("k", ""), set()),                       # FedEx needs both halves
    ("key", ("k", "s"), {Carrier.DHL, Carrier.FEDEX}),
])
def test_official_fetchers_only_when_keyed(monkeypatch, dhl, fedex, expected):
    monkeypatch.setattr(config, "FAST_ENABLED", True)
    monkeypatch.setattr(config, "DHL_API_KEY", dhl)
    monkeypatch.setattr(config, "FEDEX_API_KEY", fedex[0])
    monkeypatch.setattr(config, "FEDEX_SECRET_KEY", fedex[1])
    built = set(tracking_service._build_fast())
    assert built - {Carrier.ARAMEX, Carrier.UPS} == expected
