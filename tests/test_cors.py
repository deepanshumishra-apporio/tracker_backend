"""
Tests for the CORS allowlist.

A rejected origin is the one failure mode the frontend cannot explain: the
browser drops the response before any JS runs, so the app can only report
"cannot reach the API" while the API is in fact healthy. These pin down which
origins are allowed, because the answer is invisible from the outside.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from index import create_app  # noqa: E402


def preflight(client: TestClient, origin: str):
    return client.options(
        "/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            # The API client sends Content-Type on every request, which makes
            # even a GET preflight — so this is the real browser handshake.
            "Access-Control-Request-Headers": "content-type",
        },
    )


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app())


@pytest.mark.parametrize(
    "origin",
    [
        "https://tracker-frontend-tawny.vercel.app",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
)
def test_allows_the_known_frontends(client: TestClient, origin: str) -> None:
    res = preflight(client, origin)
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        # Vercel names every branch and every deployment differently.
        "https://tracker-frontend-tawny-git-main-someone.vercel.app",
        "https://tracker-frontend-tawny-abc1234-someone.vercel.app",
        "https://tracker-frontend.vercel.app",
        # A dev server that could not have port 3000.
        "http://localhost:3001",
        "http://127.0.0.1:56062",
    ],
)
def test_allows_previews_and_any_local_port(client: TestClient, origin: str) -> None:
    res = preflight(client, origin)
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize(
    "origin",
    [
        # Someone else's Vercel project must not get a free scraper.
        "https://not-our-app.vercel.app",
        "https://evil.example.com",
        # Right name, wrong host — the suffix has to be vercel.app itself.
        "https://tracker-frontend-tawny.vercel.app.evil.com",
        # Loopback is trusted; a LAN address is not.
        "http://192.168.1.20:3000",
    ],
)
def test_rejects_everything_else(client: TestClient, origin: str) -> None:
    res = preflight(client, origin)
    assert "access-control-allow-origin" not in res.headers


def test_an_extra_origin_can_be_added_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "https://tracker.example.com")
    res = preflight(TestClient(create_app()), "https://tracker.example.com")
    assert res.headers["access-control-allow-origin"] == "https://tracker.example.com"


def test_a_plain_request_carries_the_allow_header(client: TestClient) -> None:
    # The preflight passing is not enough; the actual response needs the header
    # too, or the browser still discards the body.
    res = client.get(
        "/api/health", headers={"Origin": "http://localhost:3000"}
    )
    assert res.status_code == 200
    assert res.headers["access-control-allow-origin"] == "http://localhost:3000"
