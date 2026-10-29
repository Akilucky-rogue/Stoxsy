"""Tests for the Scorecard HTTP surface (/api/scorecard). DB-free: the data layer
is monkeypatched, but the REAL aggregation (tracker.report) runs, so the endpoint's
shaping + math are both exercised."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.main import app
import api.routes.scorecard as sc

client = TestClient(app)

_RESOLVED = [
    {"engine": "fno_income", "horizon": "expiry", "confidence": 0.7,
     "realized_ret_net": 0.8, "status": "WIN"},
    {"engine": "fno_income", "horizon": "expiry", "confidence": 0.6,
     "realized_ret_net": -1.0, "status": "LOSS"},
    {"engine": "rule_momentum:tier1", "horizon": "swing", "confidence": 0.55,
     "realized_ret_net": 0.02, "status": "WIN"},
]


def test_scorecard_report_aggregates_by_engine(monkeypatch):
    monkeypatch.setattr(sc, "fetch_resolved", lambda engine=None: _RESOLVED)
    r = client.get("/api/scorecard")
    assert r.status_code == 200
    body = r.json()
    assert body["n_resolved"] == 3
    fno = body["by_engine"]["fno_income"]
    assert fno["n"] == 2
    assert fno["hit_rate"] == pytest.approx(0.5)
    assert fno["expectancy"] == pytest.approx((0.8 - 1.0) / 2)   # -0.1, honest
    assert body["overall"]["n"] == 3


def test_scorecard_calibration_optional(monkeypatch):
    monkeypatch.setattr(sc, "fetch_resolved", lambda engine=None: _RESOLVED)
    assert "calibration" not in client.get("/api/scorecard").json()
    cal = client.get("/api/scorecard", params={"calibration": True}).json()
    assert isinstance(cal["calibration"], list) and len(cal["calibration"]) >= 1


def test_scorecard_empty_is_honest(monkeypatch):
    monkeypatch.setattr(sc, "fetch_resolved", lambda engine=None: [])
    body = client.get("/api/scorecard").json()
    assert body["n_resolved"] == 0 and body["overall"] is None and "message" in body


def test_scorecard_ideas_lists_log(monkeypatch):
    df = pd.DataFrame([{"idea_id": "x", "engine": "fno_income", "symbol": "NIFTY",
                        "structure": "IRON_CONDOR", "status": "OPEN"}])
    monkeypatch.setattr(sc, "read_sql", lambda *a, **k: df)
    body = client.get("/api/scorecard/ideas").json()
    assert body["count"] == 1 and body["ideas"][0]["symbol"] == "NIFTY"


def test_scorecard_resolve_runs_both_resolvers(monkeypatch):
    import qsde.scorecard.resolver as rv
    monkeypatch.setattr(rv, "resolve_open_ideas", lambda *a, **k: {"closed": 2})
    monkeypatch.setattr(rv, "resolve_option_ideas", lambda *a, **k: {"closed": 1})
    monkeypatch.setattr(rv, "resolve_pair_ideas", lambda *a, **k: {"closed": 3})
    body = client.post("/api/scorecard/resolve").json()
    assert body["cash"]["closed"] == 2 and body["condors"]["closed"] == 1
    assert body["pairs"]["closed"] == 3
