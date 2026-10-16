"""
Tests for the fast path: Aramex/UPS parsing from saved markup/JSON, the
cache -> fast -> browser order in tracking_service, the stale-shipment
refresher, and the API wiring. No network: fetchers and scrapers are faked.
"""
from __future__ import annotations

import io
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import config
import index
import storage
import tracking_service
from fast import aramex as fast_aramex
from fast import ups as fast_ups
from index import create_app
from models import Carrier, Status, TrackingResult

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------
ARAMEX_RESULTS = """
<a class="shipment-card" href="/track/details?q=abc">
  <span class="shipment-tag">International Shipment</span>
  <div class="shipment-num"><h5>34931899391</h5></div>
  <p class="shipment-update-descp">The shipment has been delivered</p>
  <p class="shipment-update-datetime">14 Jun 26 22:16</p>
  <div class="orgin-info"><span class="country">India</span><span class="city">Ganaur</span></div>
  <div class="dest-info"><span class="country">Saudi Arabia</span><span class="city">Riyadh</span></div>
  <div class="shipment-progess-point start done"><div class="track-check-info"><span>Created</span></div></div>
  <div class="shipment-progess-point done"><div class="track-check-info"><span>In transit</span></div></div>
  <div class="shipment-progess-point end current done"><div class="track-check-info"><span>Delivered</span></div></div>
</a>
"""

ARAMEX_DETAILS = """
<div><p>Shipment Type</p><p>Parcel Express</p><p>Number of Items</p><p>1</p>
<p>Weight</p><p>0.64</p></div>
<table>
 <tr><th></th><th>Date</th><th>Location</th><th>Activity</th></tr>
 <tr><td></td><td>14 Jun 26 22:16</td><td>Riyadh, Saudi Arabia</td><td>The shipment has been delivered</td></tr>
 <tr><td></td><td>13 Jun 26 09:00</td><td>Riyadh, Saudi Arabia</td><td>Out for delivery</td></tr>
</table>
"""


def test_aramex_parses_card_and_details():
    cards = fast_aramex.parse_cards(ARAMEX_RESULTS)
    card = cards["34931899391"]
    assert card["href"] == "/track/details?q=abc" and card["stage"] == "Delivered"
    events, fields = fast_aramex.parse_details(ARAMEX_DETAILS)
    r = fast_aramex.build_result("34931899391", card, events, fields)
    assert r.ok and r.status is Status.DELIVERED
    assert r.origin == "Ganaur, India" and r.destination == "Riyadh, Saudi Arabia"
    assert r.service == "Parcel Express" and r.weight == "0.64 kg" and r.pieces == 1
    assert len(r.events) == 2 and r.events[0].description == "The shipment has been delivered"
    assert r.delivered_at == datetime(2026, 6, 14, 22, 16)


def test_aramex_missing_card_is_not_found_and_failed_page_is_transient(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        if "results" in url:
            if "BAD" in kw["params"]["ShipmentNumber"]:
                raise fast_aramex.TransientError("HTTP 503")
            return ARAMEX_RESULTS
        return ARAMEX_DETAILS

    monkeypatch.setattr(fast_aramex, "_get", fake_get)
    got, transient = fast_aramex.AramexFast(chunk=2, workers=2).track_many(
        ["34931899391", "00000000001", "BAD1"])
    assert got["34931899391"].ok
    assert not got["00000000001"].ok and "no shipment" in got["00000000001"].error
    assert transient == {"BAD1": "HTTP 503"}


UPS_JSON = {
    "statusCode": "200",
    "trackDetails": [{
        "trackingNumber": "1ZH40B480432930678",
        "packageStatus": "Delivered", "packageStatusType": "D", "isDelivered": True,
        "receivedBy": "SMITH", "leftAt": "Front Door",
        "shipToAddress": {"city": "COVENTRY", "country": "GB"},
        "milestones": [{"location": "India", "name": "Label Created"}],
        "additionalInformation": {
            "serviceInformation": {"serviceName": "UPS Worldwide Express Saver�"},
            "weight": "1.5", "weightUnit": "KGS", "shippedOrBilledDate": "06/09/2026"},
        "shipmentProgressActivities": [
            {"date": "06/26/2026", "time": "4:42 P.M.", "location": "COVENTRY, GB",
             "activityScan": "DELIVERED ", "gmtDate": "20260626", "gmtTime": "15:42:41",
             "gmtOffset": "+01:00"},
            {"date": "06/24/2026", "time": "1:00 A.M.", "location": "Delhi, IN",
             "activityScan": "Departed from Facility"},
        ],
    }],
}


def test_ups_parses_json():
    r = fast_ups.parse("1ZH40B480432930678", UPS_JSON)
    assert r.ok and r.status is Status.DELIVERED
    assert r.service == "UPS Worldwide Express Saver"
    assert r.weight == "1.5 KGS" and r.signed_by == "SMITH"
    assert r.origin == "India" and r.destination == "COVENTRY, GB"
    assert r.delivered_at.isoformat() == "2026-06-26T16:42:41+01:00"
    assert r.events[1].timestamp == datetime(2026, 6, 24, 1, 0)
    assert r.details["Left at"] == "Front Door"


def test_ups_unknown_number_is_invalid():
    r = fast_ups.parse("55104432", {"statusCode": "200", "trackDetails": [
        {"errorCode": "504", "errorText": "Invalid"}]})
    assert not r.ok and "invalid" in r.error


def test_ups_rewarms_once_when_cookies_expire(monkeypatch):
    u = fast_ups.UPSFast(workers=1)
    warms = []
    monkeypatch.setattr(u, "_warm", lambda: (warms.append(1), setattr(u, "_cookies", {"x": "y"}),
                                              setattr(u, "_warmed_at", time.monotonic() + len(warms))))
    calls = {"n": 0}

    def call(number):
        calls["n"] += 1
        if calls["n"] == 1:
            raise fast_ups.CookiesExpired()
        return fast_ups.parse(number, UPS_JSON)

    monkeypatch.setattr(u, "_call", call)
    got, transient = u.track_many(["1ZH40B480432930678"])
    assert got["1ZH40B480432930678"].ok and not transient
    assert len(warms) == 2


# ---------------------------------------------------------------------------
# Service: cache -> fast -> browser
# ---------------------------------------------------------------------------
def _ok(carrier, n, status=Status.IN_TRANSIT):
    return TrackingResult(tracking_number=n, carrier=carrier, status=status,
                          scraped_at=datetime.now(timezone.utc))


class FakeFast:
    def __init__(self, carrier, transient=()):
        self.carrier, self.transient, self.calls = carrier, set(transient), []

    def track_many(self, numbers):
        self.calls.append(list(numbers))
        got = {n: _ok(self.carrier, n) for n in numbers if n not in self.transient}
        return got, {n: "blocked" for n in numbers if n in self.transient}


class FakeBrowser:
    def __init__(self):
        self.seen = []

    def __call__(self, carrier):
        outer = self

        class Ctx:
            def __enter__(self):
                return lambda n: (outer.seen.append((carrier, n)), _ok(carrier, n))[1]

            def __exit__(self, *a):
                return False
        return Ctx()


def test_fast_then_browser_fallback_then_cache(monkeypatch):
    ar = FakeFast(Carrier.ARAMEX, transient={"A2"})
    monkeypatch.setattr(tracking_service, "FAST", {Carrier.ARAMEX: ar})
    browser = FakeBrowser()

    out = tracking_service.track_many(
        [(Carrier.ARAMEX, "A1"), (Carrier.ARAMEX, "A2"), (Carrier.DHL, "D1")], browser)
    assert all(r.ok for r in out.values()) and len(out) == 3
    assert ar.calls == [["A1", "A2"]]
    # A2 was blocked on the fast path; DHL has no fast fetcher.
    assert sorted(browser.seen) == [(Carrier.ARAMEX, "A2"), (Carrier.DHL, "D1")]

    # Second time: all fresh in the DB -> nobody is asked.
    hits = []
    tracking_service.track_many(
        [(Carrier.ARAMEX, "A1"), (Carrier.DHL, "D1")], browser,
        on_result=lambda c, n, r, cached: hits.append(cached))
    assert hits == [True, True] and ar.calls == [["A1", "A2"]]


def test_failed_refresh_keeps_good_data(monkeypatch):
    storage.ensure_db()
    good = _ok(Carrier.UPS, "1ZOLD", Status.IN_TRANSIT)
    good.events = []
    storage.save(good)
    bad = TrackingResult.failure("1ZOLD", Carrier.UPS, "UPS says invalid")
    kept = tracking_service._store(bad)
    assert kept.ok and kept.details["Last refresh failed"] == "UPS says invalid"
    assert storage.get_shipment("ups", "1ZOLD")["ok"] is True


def test_stale_query_skips_delivered_and_fresh():
    storage.ensure_db()
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    for n, st, when in [("OLD", Status.IN_TRANSIT, old), ("NEW", Status.IN_TRANSIT, None),
                        ("DONE", Status.DELIVERED, old)]:
        r = _ok(Carrier.UPS, n, st)
        r.scraped_at = when or datetime.now(timezone.utc)
        storage.save(r)
    assert storage.stale_shipments(config.CACHE_MAX_AGE, 10) == [("ups", "OLD")]


def test_refresher_sweep_retracks_stale(monkeypatch):
    storage.ensure_db()
    r = _ok(Carrier.ARAMEX, "A9")
    r.scraped_at = datetime.now(timezone.utc) - timedelta(days=1)
    storage.save(r)
    ar = FakeFast(Carrier.ARAMEX)
    monkeypatch.setattr(tracking_service, "FAST", {Carrier.ARAMEX: ar})
    sweep = tracking_service.Refresher(FakeBrowser()).sweep()
    assert sweep["checked"] == 1 and sweep["ok"] == 1 and ar.calls == [["A9"]]
    assert storage.stale_shipments(config.CACHE_MAX_AGE, 10) == []


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture()
def fast_api(monkeypatch):
    fakes = {Carrier.ARAMEX: FakeFast(Carrier.ARAMEX), Carrier.UPS: FakeFast(Carrier.UPS)}
    monkeypatch.setattr(tracking_service, "FAST", fakes)
    return fakes


def test_track_endpoint_caches(fast_api):
    c = TestClient(create_app())
    first = c.get("/api/track", params={"carrier": "aramex", "tracking_number": "A1"}).json()
    second = c.get("/api/track", params={"carrier": "aramex", "tracking_number": "A1"}).json()
    forced = c.get("/api/track", params={"carrier": "aramex", "tracking_number": "A1",
                                         "refresh": "true"}).json()
    assert first["from_cache"] is False and second["from_cache"] is True
    assert forced["from_cache"] is False
    assert fast_api[Carrier.ARAMEX].calls == [["A1"], ["A1"]]
    assert c.get("/api/health").json()["fast_path"] == ["aramex", "ups"]
    found = c.get("/api/shipments", params={"search": "A1"}).json()
    assert [s["tracking_number"] for s in found] == ["A1"]


def test_batch_uses_fast_path_in_bulk(fast_api):
    wb = Workbook()
    ws = wb.active
    ws.append(["company", "awb"])
    for i in range(30):
        ws.append(["Aramex" if i % 2 else "UPS", f"N{i}"])
    buf = io.BytesIO()
    wb.save(buf)
    c = TestClient(create_app())
    job = c.post("/api/batch", files={"file": ("orders.xlsx", buf.getvalue())}).json()
    for _ in range(300):
        job = c.get(f"/api/batch/{job['id']}").json()
        if job["state"] == "completed":
            break
        time.sleep(0.02)
    assert job["counts"]["done"] == 30
    # One bulk call per carrier, not 30 lookups.
    assert len(fast_api[Carrier.ARAMEX].calls) == 1 and len(fast_api[Carrier.UPS].calls) == 1
