"""Investor goals, balances and plan against the real database with a synthetic proxy history (fast and deterministic). Uses the owner's three TEST goals, flagged is_test; nothing here touches the 'local' profile."""
import uuid
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.auth_deps import Principal, require_auth
from api.main import app
from api.routes import investor
from qsde.db.connection import get_sync_conn
from qsde.investor import history as H

NL = chr(10)


def fake_history():
    rng = np.random.default_rng(5)
    idx = pd.date_range("2016-01-31", periods=129, freq="ME")
    m = pd.DataFrame({"equity": rng.normal(0.009, 0.045, 129), "gold": rng.normal(0.01, 0.04, 129), "debt": np.full(129, 0.005)}, index=idx)
    days = pd.bdate_range("2016-02-01", "2026-09-30")
    d = pd.DataFrame({"equity": rng.normal(0.0004, 0.01, len(days)), "gold": rng.normal(0.0004, 0.008, len(days)), "debt": np.full(len(days), 0.00022)}, index=days)
    return H.History(monthly=m, daily=d, proxies={"equity": {"series": "TEST"}}, notes=["synthetic test history"])


@pytest.fixture()
def who(monkeypatch):
    uid = str(uuid.uuid4())
    role = {"v": "trader"}
    app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role=role["v"], authenticated=True)
    monkeypatch.setattr(investor, "_history", fake_history)
    yield TestClient(app), uid, role
    app.dependency_overrides.pop(require_auth, None)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")
        for t in ("mode_audit", "goals", "investor_balances", "portfolio_imports", "user_profiles"):
            cur.execute(f"DELETE FROM {t} WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM audit_log WHERE user_id = %s", (uid,))


def in_years(y):
    return str(date.today() + timedelta(days=int(365.25 * y)))


def answers(c, **kw):
    body = {"drawdown_reaction": "hold", "income_stability": "stable", "dependents": 1, **kw}
    assert c.put("/api/me/profile", json=body).status_code == 200


TEST_GOALS = [
    {"name": "TEST emergency top-up", "kind": "emergency", "target_amount": 500000, "target_date": in_years(2.2), "monthly_sip": 15000, "is_test": True},
    {"name": "TEST long-term wealth", "kind": "goal", "target_amount": 10000000, "target_date": in_years(15.2), "monthly_sip": 25000, "is_test": True},
    {"name": "TEST medium-term", "kind": "goal", "target_amount": 2000000, "target_date": in_years(5.2), "monthly_sip": 10000, "is_test": True},
]


def test_plan_asks_for_missing_answers_then_for_a_goal_and_never_assumes(who):
    c, uid, _ = who
    j = c.get("/api/investor/plan").json()
    assert j["status"] == "needs_input" and set(j["needs"]) == {"drawdown_reaction", "income_stability", "dependents"} and j["goals"] == []
    assert any("Emergency fund is not recorded" in n for n in j["notes"]) and any("No manual balances" in n for n in j["notes"])
    answers(c)
    assert c.get("/api/investor/plan").json()["status"] == "needs_goal"


def test_goals_crud_validation_ownership_and_audit(who):
    c, uid, role = who
    assert c.post("/api/investor/goals", json={**TEST_GOALS[1], "target_date": str(date.today())}).status_code == 422
    assert c.post("/api/investor/goals", json={**TEST_GOALS[1], "target_amount": 0}).status_code == 422
    gid = c.post("/api/investor/goals", json=TEST_GOALS[1]).json()["id"]
    assert [g["name"] for g in c.get("/api/investor/goals").json()] == ["TEST long-term wealth"]
    assert c.patch(f"/api/investor/goals/{gid}", json={"monthly_sip": 30000}).status_code == 200
    assert c.patch(f"/api/investor/goals/{gid}", json={}).status_code == 422
    role["v"] = "viewer"
    assert c.get("/api/investor/goals").status_code == 200 and c.post("/api/investor/goals", json=TEST_GOALS[1]).status_code == 403 and c.delete(f"/api/investor/goals/{gid}").status_code == 403
    role["v"] = "trader"
    app.dependency_overrides[require_auth] = lambda: Principal(id=str(uuid.uuid4()), email="o@example.com", role="trader", authenticated=True)
    try:
        assert c.get("/api/investor/goals").json() == [] and c.patch(f"/api/investor/goals/{gid}", json={"name": "x"}).status_code == 404 and c.delete(f"/api/investor/goals/{gid}").status_code == 404
    finally:
        app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role="trader", authenticated=True)
    assert c.delete(f"/api/investor/goals/{gid}").status_code == 200 and c.get("/api/investor/goals").json() == []
    acts = [r[0] for r in __import__("qsde.db.connection", fromlist=["x"]).read_sql("SELECT action FROM mode_audit WHERE user_id = :u ORDER BY id", params={"u": uid}).values]
    assert acts == ["goal.create", "goal.update", "goal.archive"]


def test_the_plan_for_the_three_test_goals_is_complete_reproducible_and_honest(who):
    c, uid, _ = who
    answers(c, income_stability="stable", dependents=1)
    for g in TEST_GOALS:
        assert c.post("/api/investor/goals", json=g).status_code == 200
    a, b = c.get("/api/investor/plan").json(), c.get("/api/investor/plan").json()
    assert a["status"] == "ok" and a == b                                                  # reproducible
    by = {g["name"]: g for g in a["goals"]}
    em, lt, mid = by["TEST emergency top-up"], by["TEST long-term wealth"], by["TEST medium-term"]
    assert em["profile"] == "emergency" and em["mix"] == {"equity": 0.0, "gold": 0.0, "debt": 1.0}
    assert lt["profile"] == "growth" and mid["profile"] == "balanced"                       # tolerance 'hold' caps at growth; 5.2 years allows balanced
    for g in (lt, mid):
        assert sum(g["mix"].values()) == pytest.approx(1.0) and g["projection"]["inputs"]["history_months"] == 129 and g["projection"]["inputs"]["seed"] == 20261023
        assert "not a forecast" in g["projection"]["label"] and [s["block_months"] for s in g["sensitivity"]] == [3, 6, 12]
        assert g["glide"] is True and g["is_test"] is True
    assert a["target"] is not None and sum(a["target"].values()) == pytest.approx(1.0) and a["target"]["equity"] < 0.70               # blended, below the growth mix because of the medium-term goal
    assert a["rebalance"]["action"] == "no_data"                                          # nothing imported yet: it says so
    assert "SCENARIO" in a["label"] and a["history"]["months"] == 129


def test_a_low_tolerance_lowers_equity_and_an_off_track_goal_gets_tradeoffs_not_more_equity(who):
    c, uid, _ = who
    answers(c, drawdown_reaction="sell_all", income_stability="stable", dependents=0)
    c.post("/api/investor/goals", json={**TEST_GOALS[1], "target_amount": 90_000_000, "monthly_sip": 5000})
    g = c.get("/api/investor/plan").json()["goals"][0]
    assert g["profile"] == "conservative" and g["mix"]["equity"] <= 0.25
    assert g["status"] == "off_track" and "monthly_contribution_needed" in g["tradeoffs"] and "not offered" in g["tradeoffs"]["note"]
    assert any("tolerance" in r for r in g["profile_reasons"])


def test_a_choice_above_what_is_allowed_is_not_used_and_a_lower_choice_is(who):
    c, uid, _ = who
    answers(c, drawdown_reaction="sell_some", income_stability="stable", dependents=0)               # tolerance caps at balanced
    c.post("/api/investor/goals", json=TEST_GOALS[1])
    c.put("/api/me/profile", json={"risk_profile": "aggressive"})
    g = c.get("/api/investor/plan").json()["goals"][0]
    assert g["profile"] == "balanced" and any("not used" in r for r in g["profile_reasons"])
    c.put("/api/me/profile", json={"risk_profile": "conservative"})
    g = c.get("/api/investor/plan").json()["goals"][0]
    assert g["profile"] == "conservative" and any("lower profile" in r for r in g["profile_reasons"])


@pytest.mark.real_data
def test_only_the_investment_pot_feeds_the_plan_and_balances_complete_the_picture(who):
    c, uid, _ = who
    answers(c)
    c.post("/api/investor/goals", json=TEST_GOALS[1])
    csv = NL.join(["symbol,quantity,avg_cost", "TCS,10,3000"])
    assert c.post("/api/portfolio/import", json={"pot": "trading", "csv_text": csv}).status_code == 200
    j = c.get("/api/investor/plan").json()
    assert j["current"]["total_classified"] == 0 and j["rebalance"]["action"] == "no_data"           # a trading import is invisible to the investor plan
    assert c.post("/api/portfolio/import", json={"pot": "investment", "csv_text": csv}).status_code == 200
    bid = c.post("/api/investor/balances", json={"asset_class": "debt", "label": "TEST liquid fund", "amount": 400000, "as_of": str(date.today())}).json()["id"]
    j = c.get("/api/investor/plan").json()
    v = j["current"]["values"]
    assert v["equity"] > 0 and v["debt"] == 400000 and v["gold"] == 0 and j["holdings_priced_at"]
    assert not any("No manual balances" in n for n in j["notes"]) and j["balances"][0]["label"] == "TEST liquid fund"
    assert {p["source"] for p in j["current"]["parts"]["debt"]} == {"manual"}
    assert c.delete(f"/api/investor/balances/{bid}").status_code == 200 and c.delete(f"/api/investor/balances/{bid}").status_code == 404
    assert c.post("/api/investor/balances", json={"asset_class": "crypto", "label": "x", "amount": 1, "as_of": str(date.today())}).status_code == 422


def test_unclassifiable_etfs_are_reported_not_counted_as_equity(who):
    from qsde.investor import assets as A

    inst = A.load_instruments()
    assert A.classify_symbol("GOLDBEES", inst)["class"] == "gold" and A.classify_symbol("NIFTYBEES", inst)["class"] == "equity"
    assert A.classify_symbol("LIQUIDBEES", inst)["class"] == "debt" and A.classify_symbol("TCS", inst)["kind"] == "stock"
    silver = A.classify_symbol("SILVERBEES", inst)
    assert silver["class"] is None and "not one of equity, gold or debt" in silver["reason"]
    alloc = A.current_allocation([{"symbol": "SILVERBEES", "value": 1000.0}, {"symbol": "TCS", "value": 3000.0}, {"symbol": "NOPRICE", "value": None}], [], inst)
    assert alloc["values"]["equity"] == 3000 and alloc["unclassified_value"] == 1000 and alloc["unpriced"][0]["symbol"] == "NOPRICE"
    assert len(A.completeness_notes(alloc, False)) == 3
