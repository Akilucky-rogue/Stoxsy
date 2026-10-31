"""The System page must say whether the nightly routine is really running, from the routine's own heartbeat, not from a constant. Real database, run locally."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pytestmark = pytest.mark.real_data
FRESH_HOURS = 96                                 # a Friday run must still count on Monday morning, and a one-day exchange holiday must not flip it


def test_nightly_routine_comes_from_the_heartbeat_not_a_constant():
    from api.main import app
    from qsde.db.connection import read_sql

    hb = read_sql("SELECT last_beat, status FROM ops_heartbeat WHERE component = 'daily_pipeline'")
    if hb.empty:
        pytest.skip("the nightly routine has never run on this database")
    j = TestClient(app).get("/api/system/status").json()
    n = j["nightly_routine"]
    last = hb["last_beat"].iloc[0].to_pydatetime()
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    assert n["status"] == hb["status"].iloc[0] and abs(n["age_hours"] - age_h) < 0.1 and n["last_beat"].startswith(last.astimezone(timezone.utc).strftime("%Y-%m-%d"))
    assert j["scheduled_downloads_enabled"] is (age_h <= FRESH_HOURS)


def test_without_a_heartbeat_the_page_says_it_has_not_seen_the_routine(monkeypatch):
    import api.routes.cockpit as C

    monkeypatch.setattr(C, "_nightly_routine", lambda: None)
    from api.main import app

    j = TestClient(app).get("/api/system/status").json()
    assert j["nightly_routine"] is None and j["scheduled_downloads_enabled"] is False
