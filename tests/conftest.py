"""Shared test setup."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import tracking_service  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No test may reach a carrier or the developer's tracking.db.

    * FAST is emptied, so lookups go through the (faked) browser scrapers;
      tests of the fast path install their own fake fetchers.
    * Results are now saved on every lookup, so each test gets its own DB.
    * The background refresher stays off.
    * The always-on browser pool stays off; its own tests start one.
    """
    monkeypatch.setattr(tracking_service, "FAST", {})
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(config, "REFRESH_ENABLED", False)
    monkeypatch.setattr(config, "BROWSER_POOL_SIZE", 0)
    monkeypatch.setattr(tracking_service, "POOL", None)
