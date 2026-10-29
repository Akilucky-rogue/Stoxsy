"""Existing book first: imported trading-pot positions count toward every limit, need a stop before new ideas unlock, get a Section A command, and never include investment-pot imports."""
import uuid
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from api.auth_deps import Principal, require_auth
from api.main import app
from qsde.db.connection import get_sync_conn, read_sql
from qsde.trader import legacy as LG, risk as R

TODAY = date(2026, 10, 27)


def test_manage_commands_in_priority_order():
    assert LG.manage(entry=100, stop=None, last=105)["action"] == "SET_STOP_NOW"
    assert LG.manage(entry=100, stop=95, last=94, time_stop=date(2030, 1, 1), today=TODAY)["action"] == "EXIT_STOP_BREACHED"
    assert LG.manage(entry=100, stop=95, last=95)["action"] == "EXIT_STOP_BREACHED"                                      # at the stop counts
    assert LG.manage(entry=100, stop=95, last=101, time_stop=TODAY, today=TODAY)["action"] == "EXIT_TIME_STOP"
    assert LG.manage(entry=100, stop=95, last=107.5, today=TODAY)["action"] == "RAISE_STOP_TO_COST"                      # +1.5R
    assert LG.manage(entry=100, stop=95, last=107.4, today=TODAY)["action"] == "HOLD_AS_PLANNED"
    assert LG.manage(entry=100, stop=100, last=120, today=TODAY)["action"] == "HOLD_AS_PLANNED"                          # stop already at cost: nothing to raise


def test_emergency_stop_is_the_lower_of_swing_low_and_two_atr_below():
    days = pd.bdate_range("2026-09-01", periods=30)
    flat = pd.DataFrame({"date": days, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
    # ATR14 = 2.0 -> close - 2 ATR = 96; 5-session swing low = 99 -> the lower value (96) wins
    assert LG.emergency_stop(flat) == pytest.approx(96.0)
    dip = flat.copy()
    dip.loc[dip.index[-3], "low"] = 90.0                                                                                  # a deep recent low is below 2 ATR
    assert LG.emergency_stop(dip) == pytest.approx(90.0)
    assert LG.emergency_stop(flat.head(10)) is None                                                                       # too little history: no guess


def pos(sym, qty=100, entry=100.0, stop=95.0, last=100.0, prev=100.0, risk=600.0):
    return R.OpenPosition(sym, qty, entry, stop, last, prev, risk)


def test_the_gate_refuses_new_ideas_while_an_imported_position_has_no_stop():
    L = R.Limits.from_effective({"risk_per_trade_pct": 0.25, "open_risk_pct": 3, "daily_loss_pct": 1.5, "weekly_loss_pct": 4, "max_positions": 8, "max_pair_correlation": 0.55, "liquidity_cap_pct": 1})
    kw = dict(symbol="AAA", side="long", entry=100.0, stop=95.0, cost_per_unit=0.3, gap_allowance=0.5, adv_value=5e8)
    ok = R.evaluate(R.State(capital=1e6, open_positions=[pos("ZEEL")]), L, **kw)
    assert ok.allowed
    blocked = R.evaluate(R.State(capital=1e6, open_positions=[pos("ZEEL")], unprotected=["ZEEL", "YESBANK"]), L, **kw)
    assert not blocked.allowed and "2 imported trading-pot position(s) have no stop (YESBANK, ZEEL)" in blocked.reasons[0] and "existing book is managed before any new idea" in blocked.reasons[0]
    assert R.meters(R.State(capital=1e6, unprotected=["ZEEL"]), L)["unprotected"] == ["ZEEL"]


@pytest.fixture()
def who():
    uid = str(uuid.uuid4())
    app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role="trader", authenticated=True)
    yield TestClient(app), uid
    app.dependency_overrides.pop(require_auth, None)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute("DELETE FROM portfolio_holdings WHERE import_id IN (SELECT id FROM portfolio_imports WHERE user_id = %s)", (uid,))
        for t in ("legacy_triage", "portfolio_imports", "mode_audit", "trader_ideas", "user_profiles", "compliance_restricted_list"):
            cur.execute(f"DELETE FROM {t} WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM audit_log WHERE user_id = %s", (uid,))


def make_import(uid, pot, rows):
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO portfolio_imports (user_id, sha256, n_rows_file, n_accepted, n_rejected, pot) VALUES (%s,%s,%s,%s,0,%s) RETURNING id", (uid, uuid.uuid4().hex + uuid.uuid4().hex, len(rows), len(rows), pot))
        iid = cur.fetchone()["id"]
        for sym, qty, cost in rows:
            cur.execute("INSERT INTO portfolio_holdings (import_id, symbol, quantity, avg_cost) VALUES (%s,%s,%s,%s)", (iid, sym, qty, cost))
    return iid


def pot(c, amount=1_000_000):
    assert c.put("/api/me/profile", json={"investment_capital": 40_000_000, "trading_capital": amount, "emergency_fund": 0}).status_code == 200


@pytest.mark.real_data
def test_imported_positions_lock_new_ideas_until_each_has_a_stop_then_count_toward_open_risk(who):
    c, uid = who
    pot(c)
    last = float(read_sql("SELECT close FROM ohlcv WHERE symbol = 'RELIANCE' ORDER BY date DESC LIMIT 1")["close"].iloc[0])
    make_import(uid, "trading", [("RELIANCE", 50, round(last * 0.97, 2))])
    make_import(uid, "investment", [("TCS", 10, 3000.0)])                                                               # investment pot: never part of the trader book
    t = c.get("/api/trader/triage").json()
    assert [p["symbol"] for p in t["positions"]] == ["RELIANCE"] and t["unprotected"] == ["RELIANCE"]
    assert t["positions"][0]["action"] == "SET_STOP_NOW" and t["positions"][0]["emergency_stop"] is not None and t["positions"][0]["risk_rupees"] > 0
    desk = c.get("/api/trader/desk").json()
    assert desk["unprotected"] == ["RELIANCE"] and "Existing book first" in desk["headline"] and desk["meters"]["positions"] == 1 and desk["book"][0]["source"] == "imported"
    r = c.post("/api/trader/ideas", json={"symbol": "INFY"})
    assert r.status_code == 422 and "no stop (RELIANCE)" in r.json()["detail"][0]
    # recording a stop unlocks new ideas; the imported position's risk is part of open risk
    stop = round(last * 0.94, 2)
    assert c.put("/api/trader/triage/RELIANCE", json={"stop_price": stop, "horizon_id": "swing_5d"}).status_code == 200
    d2 = c.get("/api/trader/desk").json()
    assert d2["unprotected"] == [] and d2["book"][0]["protected"] and d2["book"][0]["action"] in ("HOLD_AS_PLANNED", "RAISE_STOP_TO_COST")
    assert d2["meters"]["open_risk"] == pytest.approx(50 * (last - stop) + 50 * 0.0, rel=0.35)                         # counted at the recorded stop (plus a gap allowance)
    ok = c.post("/api/trader/ideas", json={"symbol": "INFY"})
    assert ok.status_code == 200, ok.text
    assert int(read_sql("SELECT count(*) AS n FROM mode_audit WHERE user_id = :u AND action = 'trader.triage'", params={"u": uid})["n"].iloc[0]) == 1


def test_stops_can_only_be_set_for_symbols_in_the_latest_trading_import(who):
    c, uid = who
    assert c.put("/api/trader/triage/RELIANCE", json={"stop_price": 100}).status_code == 404                              # nothing imported
    make_import(uid, "trading", [("RELIANCE", 5, 1000.0)])
    assert c.put("/api/trader/triage/TCS", json={"stop_price": 100}).status_code == 404                                   # not in the import
    assert c.put("/api/trader/triage/RELIANCE", json={"stop_price": 0}).status_code == 422                                # a stop must be positive


@pytest.mark.real_data
def test_restricted_list_starts_empty_blocks_a_symbol_until_released_and_is_audited(who):
    c, uid = who
    pot(c)
    assert c.get("/api/trader/restricted").json()["entries"] == []                                                         # nothing preset
    assert c.post("/api/trader/restricted", json={"symbol": "infy", "reason": "x"}).status_code == 422                       # a reason is required
    assert c.post("/api/trader/restricted", json={"symbol": "infy", "reason": "client coverage"}).status_code == 200
    r = c.post("/api/trader/ideas", json={"symbol": "INFY"})
    assert r.status_code == 422 and "INFY is on your restricted list (client coverage)" in r.json()["detail"][0]
    assert c.post("/api/trader/propose", json={"symbol": "RELIANCE"}).json()["allowed"] is True                             # other symbols are unaffected
    e = c.get("/api/trader/restricted").json()["entries"][0]
    assert e["symbol"] == "INFY" and e["active"] is True
    assert c.delete("/api/trader/restricted/INFY").status_code == 200 and c.delete("/api/trader/restricted/INFY").status_code == 404
    assert c.post("/api/trader/ideas", json={"symbol": "INFY"}).status_code == 200
    e2 = c.get("/api/trader/restricted").json()["entries"][0]
    assert e2["active"] is False and e2["released_at"] is not None                                                          # history is kept
    n = lambda a: int(read_sql("SELECT count(*) AS n FROM mode_audit WHERE user_id = :u AND action = :a", params={"u": uid, "a": a})["n"].iloc[0])    # noqa: E731
    assert n("trader.restricted.add") == 1 and n("trader.restricted.release") == 1


@pytest.mark.real_data
def test_a_restriction_with_a_past_end_date_no_longer_blocks(who):
    c, uid = who
    pot(c)
    assert c.post("/api/trader/restricted", json={"symbol": "INFY", "reason": "blackout", "restricted_until": "2020-01-01"}).status_code == 200
    assert c.get("/api/trader/restricted").json()["entries"][0]["active"] is False
    assert c.post("/api/trader/propose", json={"symbol": "INFY"}).json()["allowed"] is True
