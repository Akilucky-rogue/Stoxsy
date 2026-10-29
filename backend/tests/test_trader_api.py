"""Trader desk against the real database with a throwaway user: the pot is needed, the gate runs on every write, refusals are audited and store nothing, closing books P&L and R,
and one user's ideas never count against another's limits. Real RELIANCE prices are used for the card; nothing touches the 'local' profile."""
import uuid

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.auth_deps import Principal, require_auth
from api.main import app
from qsde.db.connection import get_sync_conn, read_sql
from qsde.trader import setups, store

SYM = "RELIANCE"


@pytest.fixture()
def who():
    uid = str(uuid.uuid4())
    role = {"v": "trader"}
    app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role=role["v"], authenticated=True)
    yield TestClient(app), uid, role
    app.dependency_overrides.pop(require_auth, None)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")
        for t in ("mode_audit", "trader_ideas", "user_profiles"):
            cur.execute(f"DELETE FROM {t} WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM audit_log WHERE user_id = %s", (uid,))


def pot(c, amount=1_000_000):
    assert c.put("/api/me/profile", json={"investment_capital": 40_000_000, "trading_capital": amount, "emergency_fund": 0}).status_code == 200


def audit(uid, action):
    return int(read_sql("SELECT count(*) AS n FROM mode_audit WHERE user_id = :u AND action = :a", params={"u": uid, "a": action})["n"].iloc[0])


def n_ideas(uid):
    return int(read_sql("SELECT count(*) AS n FROM trader_ideas WHERE user_id = :u", params={"u": uid})["n"].iloc[0])


@pytest.mark.real_data
def test_without_a_trading_pot_nothing_is_sized_or_stored(who):
    c, uid, _ = who
    d = c.get("/api/trader/desk").json()
    assert d["needs"] == ["trading_capital"] and d["candidates"] == [] and d["trading_capital"] is None and "nothing is assumed" in d["headline"]
    r = c.post("/api/trader/ideas", json={"symbol": SYM})
    assert r.status_code == 422 and "trading capital is not set" in r.json()["detail"][0]
    assert n_ideas(uid) == 0 and audit(uid, "trader.refused") == 1                      # the refusal itself is on the record


@pytest.mark.real_data
def test_a_proposed_card_follows_the_definitions_and_is_research_only(who):
    c, uid, _ = who
    pot(c)
    j = c.post("/api/trader/propose", json={"symbol": SYM, "horizon_id": "swing_5d", "setup": "manual"}).json()
    assert j["allowed"], j["reasons"]
    k = j["card"]
    assert k["evidence"] == "RESEARCH" and "No engine is approved" in k["evidence_note"] and k["stop"] < k["entry"] < k["target"]
    assert k["risk_rupees"] <= 1_000_000 * 0.0025 + 1e-6 and k["risk_pct_of_trading_capital"] <= 0.25 + 1e-9                      # the stricter default, never more
    assert k["qty"] >= 1 and k["position_value"] <= 1_000_000 and k["time_stop"] > k["as_of"] and k["invalidated_if"]
    assert n_ideas(uid) == 0                                                                    # propose stores nothing


@pytest.mark.real_data
def test_the_write_path_re_runs_the_gate_so_a_prepared_card_cannot_skip_a_limit(who):
    c, uid, _ = who
    pot(c)
    bad_stop = c.post("/api/trader/ideas", json={"symbol": SYM, "entry": 100, "stop": 120})
    assert bad_stop.status_code == 422 and "below the entry" in bad_stop.json()["detail"][0]
    approved = c.post("/api/trader/ideas", json={"symbol": SYM, "evidence": "APPROVED"})
    assert approved.status_code == 422 and "no engine is approved" in approved.json()["detail"][0]
    assert c.post("/api/trader/ideas", json={"symbol": "NOSUCHSYMBOLXYZ"}).status_code == 422
    assert n_ideas(uid) == 0 and audit(uid, "trader.refused") == 3


@pytest.mark.real_data
def test_viewers_can_read_but_not_write(who):
    c, uid, role = who
    pot(c)
    role["v"] = "viewer"
    assert c.get("/api/trader/desk").status_code == 200 and c.get("/api/trader/ideas").status_code == 200
    assert c.post("/api/trader/ideas", json={"symbol": SYM}).status_code == 403
    assert c.post("/api/trader/ideas/1/close", json={"price": 1, "reason": "manual"}).status_code == 403
    assert n_ideas(uid) == 0


@pytest.mark.real_data
def test_open_close_book_pnl_and_r_and_limits_see_the_open_position(who):
    c, uid, _ = who
    pot(c)
    made = c.post("/api/trader/ideas", json={"symbol": SYM, "horizon_id": "swing_5d", "setup": "manual"})
    assert made.status_code == 200, made.text
    iid, card = made.json()["id"], made.json()["card"]
    desk = c.get("/api/trader/desk?limit=3").json()
    assert desk["meters"]["positions"] == 1 and desk["open"][0]["symbol"] == SYM and desk["meters"]["open_risk"] == pytest.approx(card["risk_rupees"])
    again = c.post("/api/trader/ideas", json={"symbol": SYM})                                  # same symbol while open
    assert again.status_code == 422 and "already open" in again.json()["detail"][0]
    exit_price = card["stop"]                                                                   # stopped out
    closed = c.post(f"/api/trader/ideas/{iid}/close", json={"price": exit_price, "reason": "stop"}).json()
    assert closed["pnl_rupees"] == pytest.approx(card["qty"] * (exit_price - card["entry"]), abs=0.01)
    assert closed["r_multiple"] < 0 and closed["r_multiple"] > -1.0                            # stopped at the stop: loses less than the planned all-in risk (cost and gap were allowed for)
    assert c.post(f"/api/trader/ideas/{iid}/close", json={"price": 1, "reason": "manual"}).status_code == 404
    perf = c.get("/api/trader/performance").json()
    assert perf["n_closed"] == 1 and perf["by_exit"] == {"stop": 1} and "too few" in perf["note"]
    after = c.get("/api/trader/desk").json()["meters"]
    assert after["positions"] == 0 and after["daily_pnl"] == pytest.approx(closed["pnl_rupees"], abs=0.01)
    assert audit(uid, "trader.idea") == 1 and audit(uid, "trader.close") == 1


@pytest.mark.real_data
def test_one_users_ideas_do_not_count_against_another(who):
    c, uid, _ = who
    pot(c)
    assert c.post("/api/trader/ideas", json={"symbol": SYM}).status_code == 200
    other = str(uuid.uuid4())
    app.dependency_overrides[require_auth] = lambda: Principal(id=other, email="o@example.com", role="trader", authenticated=True)
    try:
        assert c.get("/api/trader/desk").json()["meters"]["positions"] == 0
        assert c.get("/api/trader/ideas").json() == []
    finally:
        app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role="trader", authenticated=True)


def test_the_database_rejects_a_card_that_skips_the_api(who):
    _, uid, _ = who
    with pytest.raises(Exception):
        with get_sync_conn() as conn:
            conn.cursor().execute("INSERT INTO trader_ideas (user_id, symbol, setup, side, horizon_id, evidence, entry, stop, qty, risk_rupees, time_stop, card) VALUES (%s,'X','s','short','swing_5d','RESEARCH',100,90,1,1,CURRENT_DATE,'{}')", (uid,))
    with pytest.raises(Exception):
        with get_sync_conn() as conn:
            conn.cursor().execute("INSERT INTO trader_ideas (user_id, symbol, setup, side, horizon_id, evidence, entry, stop, qty, risk_rupees, time_stop, status, card) VALUES (%s,'X','s','long','swing_5d','RESEARCH',100,90,1,1,CURRENT_DATE,'closed','{}')", (uid,))


def test_breakout_baseline_detects_a_breakout_and_ignores_a_stale_or_quiet_one():
    days = pd.bdate_range("2026-08-03", periods=30)
    up = pd.DataFrame({"symbol": "UP", "date": days, "high": [100.0] * 29 + [110.0], "close": [99.0] * 29 + [108.0], "volume": [1000.0] * 29 + [3000.0]})
    quiet = pd.DataFrame({"symbol": "QUIET", "date": days, "high": [100.0] * 30, "close": [99.0] * 30, "volume": [1000.0] * 30})
    thin = pd.DataFrame({"symbol": "THIN", "date": days, "high": [100.0] * 29 + [110.0], "close": [99.0] * 29 + [108.0], "volume": [1000.0] * 29 + [100.0]})     # breaks out on low volume
    short = up.head(10).assign(symbol="SHORT")
    b = setups.breakouts(pd.concat([up, quiet, thin, short]))
    assert b["symbol"].tolist() == ["UP"] and b["breakout_pct"].iloc[0] == pytest.approx(108 / 100 - 1) and b["volume_x_median"].iloc[0] == pytest.approx(3.0)


def test_week_start_is_monday_and_sessions_skip_weekends():
    from datetime import date
    assert store.week_start(date(2026, 10, 28)) == date(2026, 10, 26) and store.week_start(date(2026, 10, 25)) == date(2026, 10, 19)
    from qsde import exchange_calendar as X

    cal = X.Calendar()                                                                  # nothing loaded: weekdays, and it says so
    assert store.add_sessions(date(2026, 10, 23), 1, cal) == (date(2026, 10, 26), True) and store.add_sessions(date(2026, 10, 23), 5, cal)[0] == date(2026, 10, 30)
    loaded = X.from_rows([("CM", date(2026, 11, 10), False)])
    assert store.add_sessions(date(2026, 11, 6), 3, loaded) == (date(2026, 11, 12), False)      # 9, (10 holiday) 11, 12


@pytest.mark.real_data
def test_alert_facts_for_the_desk_are_json_safe_and_describe_the_open_book(who):
    import json

    from qsde.alerts import facts as F, rules as AR

    c, uid, _ = who
    assert F.trader(uid) is None                                                   # no pot, no ideas: nothing to alert on
    pot(c)
    assert F.trader(uid) is None                                                   # a pot but no ideas yet
    made = c.post("/api/trader/ideas", json={"symbol": SYM}).json()
    t = F.trader(uid)
    json.dumps(t)                                                                  # alert payloads are stored as JSON
    assert len(t["open"]) == 1 and t["open"][0]["id"] == made["id"] and t["open"][0]["time_stop"] == made["card"]["time_stop"] and t["meters"]["positions"] == 1
    out = AR.rule_trader({"now": pd.Timestamp.now(tz="UTC"), "trader": t}, AR.load_config())
    json.dumps(out)
