"""
Tests for the live, no-store /api/track endpoint. The scraper is mocked so no
browser or network is used — we only verify the endpoint wiring and shaping.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import index  # noqa: E402
from index import create_app  # noqa: E402
from models import Carrier, Status, TrackingEvent, TrackingResult  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


class _FakeScraper:
    def __init__(self, result=None, boom: str | None = None):
        self._result = result
        self._boom = boom

    def scrape(self, number: str) -> TrackingResult:
        if self._boom:
            raise RuntimeError(self._boom)
        return self._result


def _sample(number: str) -> TrackingResult:
    return TrackingResult(
        tracking_number=number,
        carrier=Carrier.DHL,
        status=Status.DELIVERED,
        origin="Leipzig",
        destination="Dublin",
        delivered_at=datetime(2026, 6, 30, 10, 25, tzinfo=timezone.utc),
        events=[TrackingEvent(description="Delivered", status=Status.DELIVERED)],
        scraped_at=datetime(2026, 6, 30, 11, 0, tzinfo=timezone.utc),
    )


def test_track_returns_scraped_result(client: TestClient, monkeypatch):
    monkeypatch.setitem(index.SCRAPERS, Carrier.DHL, _FakeScraper(_sample("1790531772")))
    r = client.get("/api/track?carrier=dhl&tracking_number=1790531772")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "delivered"
    assert body["destination"] == "Dublin"
    assert len(body["events"]) == 1
    assert body["ok"] is True


def test_track_reports_scrape_failure_without_500(client: TestClient, monkeypatch):
    monkeypatch.setitem(index.SCRAPERS, Carrier.UPS, _FakeScraper(boom="bot-check wall"))
    r = client.get("/api/track?carrier=ups&tracking_number=1Z999")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "bot-check wall" in body["error"]


def test_track_validates_inputs(client: TestClient):
    assert client.get("/api/track?carrier=usps&tracking_number=1").status_code == 422
    assert client.get("/api/track?carrier=ups&tracking_number=has%20space").status_code == 422
    assert client.get("/api/track?carrier=ups&tracking_number=").status_code == 422


def test_track_does_not_persist(client: TestClient, monkeypatch, tmp_path):
    import config
    import storage

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "track.db"))
    storage.init_db()
    monkeypatch.setitem(index.SCRAPERS, Carrier.DHL, _FakeScraper(_sample("NOPERSIST")))

    client.get("/api/track?carrier=dhl&tracking_number=NOPERSIST")
    # The live track flow must never write to the store.
    assert storage.list_shipments() == []
