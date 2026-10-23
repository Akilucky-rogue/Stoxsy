"""Mode switch, profile and capital partition against the real database. A test user id is used (never 'local'); its rows are removed afterwards
(mode_audit is append-only by trigger, so cleanup disables triggers for the session, which needs the local superuser)."""
import uuid

import psycopg2
import pytest
from fastapi.testclient import TestClient

from api.auth_deps import Principal, require_auth
from api.main import app
from qsde.db.connection import execute_sql, get_sync_conn, read_sql

NL = chr(10)
CSV = NL.join(["symbol,quantity,avg_cost", "TCS,10,3000", "INFY,5,1500"])


@pytest.fixture()
def who():
    uid = str(uuid.uuid4())             # a UUID, like a real account id (audit_log.user_id is a UUID)
    role = {"v": "trader"}
    app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role=role["v"], authenticated=True)
    yield TestClient(app), uid, role
    app.dependency_overrides.pop(require_auth, None)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute("DELETE FROM mode_audit WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM audit_log WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM portfolio_imports WHERE user_id = %s", (uid,))
        cur.execute("DELETE FROM user_profiles WHERE user_id = %s", (uid,))


def audit(uid):
    return read_sql("SELECT action, payload FROM mode_audit WHERE user_id = :u ORDER BY id", params={"u": uid})


def test_first_read_is_a_blank_investor_profile_with_no_assumed_split(who):
    c, uid, _ = who
    j = c.get("/api/me/profile").json()
    assert j["mode"] is None and j["lens_pot"] is None                                   # nothing is preset: the user chooses
    assert j["investment_capital"] is None and j["trading_capital"] is None and j["emergency_fund"] is None
    assert j["effective_limits"]["risk_per_trade_pct"] == 0.25 and j["engine_approved"] is False and j["loosened_limits"] == []


def test_mode_switch_never_touches_the_pots_or_the_imports(who):
    c, uid, _ = who
    assert c.put("/api/me/profile", json={"investment_capital": 900000, "trading_capital": 20000, "emergency_fund": 100000}).status_code == 200
    a = c.post("/api/portfolio/import", json={"pot": "investment", "csv_text": CSV, "label": "inv"}).json()
    b = c.post("/api/portfolio/import", json={"pot": "trading", "csv_text": CSV, "label": "trd"}).json()
    snap = lambda: (read_sql("SELECT investment_capital::float8 AS i, trading_capital::float8 AS t, emergency_fund::float8 AS e FROM user_profiles WHERE user_id = :u", params={"u": uid}).iloc[0].tolist(),   # noqa: E731
                    read_sql("SELECT id, pot FROM portfolio_imports WHERE user_id = :u ORDER BY id", params={"u": uid}).values.tolist())
    before = snap()
    for m in ("trader", "investor", "trader", "investor"):
        assert c.put("/api/me/profile", json={"mode": m}).json()["mode"] == m
    assert snap() == before and before[1] == [[a["import_id"], "investment"], [b["import_id"], "trading"]]
    assert [x for x in audit(uid)["action"] if x == "mode.change"] == ["mode.change"] * 4


def test_an_import_into_one_pot_does_not_hide_the_other_and_the_same_symbol_can_be_in_both(who):
    c, uid, _ = who
    inv = c.post("/api/portfolio/import", json={"pot": "investment", "csv_text": CSV}).json()
    trd = c.post("/api/portfolio/import", json={"pot": "trading", "csv_text": NL.join(["symbol,quantity,avg_cost", "TCS,1,3100"])}).json()
    assert inv["replaced_previous"] is False and trd["replaced_previous"] is False                 # a trading import is not a replacement of the investment one
    assert c.get("/api/portfolio/summary?pot=investment&benchmark=none").json()["import_id"] == inv["import_id"]
    assert c.get("/api/portfolio/summary?pot=trading&benchmark=none").json()["import_id"] == trd["import_id"]
    # the lens follows the mode
    c.put("/api/me/profile", json={"mode": "trader"})
    j = c.get("/api/portfolio/summary?benchmark=none").json()
    assert j["pot"] == "trading" and [h["symbol"] for h in j["holdings"]] == ["TCS"]
    c.put("/api/me/profile", json={"mode": "investor"})
    j = c.get("/api/portfolio/summary?benchmark=none").json()
    assert j["pot"] == "investment" and {h["symbol"] for h in j["holdings"]} == {"TCS", "INFY"}
    again = c.post("/api/portfolio/import", json={"pot": "investment", "csv_text": CSV}).json()
    assert again["replaced_previous"] is True                                                       # same pot: it does supersede
    assert c.get("/api/portfolio/summary?pot=trading&benchmark=none").json()["import_id"] == trd["import_id"]


def test_an_import_without_a_pot_is_refused(who):
    c, _, _ = who
    assert c.post("/api/portfolio/import", json={"csv_text": CSV}).status_code == 422
    assert c.post("/api/portfolio/import", json={"pot": "retirement", "csv_text": CSV}).status_code == 422


def test_a_pot_is_assigned_once_and_never_changes(who):
    c, uid, _ = who
    iid = c.post("/api/portfolio/import", json={"pot": "trading", "csv_text": CSV}).json()["import_id"]
    assert c.post(f"/api/portfolio/imports/{iid}/assign-pot", json={"pot": "investment"}).status_code == 409
    with pytest.raises(psycopg2.Error, match="immutable"):                                          # not even by direct SQL
        execute_sql("UPDATE portfolio_imports SET pot = 'investment' WHERE id = %s", (iid,))
    # a pre-pots import (NULL) can be assigned exactly once
    execute_sql("INSERT INTO portfolio_imports (user_id, sha256, n_rows_file, n_accepted, n_rejected) VALUES (%s, %s, 1, 1, 0)", (uid, "0" * 64))
    old = int(read_sql("SELECT id FROM portfolio_imports WHERE user_id = :u AND pot IS NULL", params={"u": uid})["id"].iloc[0])
    assert c.post(f"/api/portfolio/imports/{old}/assign-pot", json={"pot": "investment"}).status_code == 200
    assert c.post(f"/api/portfolio/imports/{old}/assign-pot", json={"pot": "trading"}).status_code == 409
    assert c.post("/api/portfolio/imports/999999999/assign-pot", json={"pot": "trading"}).status_code == 404
    acts = list(audit(uid)["action"])
    assert "portfolio.assign_pot" in acts and acts.count("portfolio.import") == 1


def test_another_users_import_cannot_be_assigned_or_read(who):
    c, uid, _ = who
    execute_sql("INSERT INTO portfolio_imports (user_id, sha256, n_rows_file, n_accepted, n_rejected) VALUES (%s, %s, 1, 1, 0)", ("test-modes-other", "1" * 64))
    try:
        other = int(read_sql("SELECT id FROM portfolio_imports WHERE user_id = 'test-modes-other'")["id"].iloc[0])
        assert c.post(f"/api/portfolio/imports/{other}/assign-pot", json={"pot": "investment"}).status_code == 404
    finally:
        execute_sql("DELETE FROM portfolio_imports WHERE user_id = 'test-modes-other'")


def test_limits_above_the_ceiling_are_refused_and_an_approved_loosening_is_audited(who):
    c, uid, role = who
    assert c.put("/api/me/profile", json={"limits": {"risk_per_trade_pct": 1.0}, "approval_note": "owner wants one percent"}).status_code == 422
    assert c.put("/api/me/profile", json={"limits": {"risk_per_trade_pct": 0.5}}).status_code == 422          # needs a note
    role["v"] = "trader"
    assert c.put("/api/me/profile", json={"limits": {"risk_per_trade_pct": 0.5}, "approval_note": "owner reviewed the paper results"}).status_code == 422   # a trader cannot approve
    role["v"] = "admin"
    r = c.put("/api/me/profile", json={"limits": {"risk_per_trade_pct": 0.5}, "approval_note": "owner reviewed the paper results"})
    assert r.status_code == 200 and r.json()["effective_limits"]["risk_per_trade_pct"] == 0.5 and r.json()["loosened_limits"] == ["risk_per_trade_pct"]
    row = audit(uid).iloc[-1]
    assert row["action"] == "limits.change" and row["payload"]["approved"] == ["risk_per_trade_pct"] and "paper results" in row["payload"]["approval_note"]
    r = c.put("/api/me/profile", json={"limits": {"risk_per_trade_pct": 0.1}})                                # tightening back is free
    assert r.json()["effective_limits"]["risk_per_trade_pct"] == 0.1 and r.json()["loosened_limits"] == []


def test_trading_pot_above_five_percent_needs_an_audited_admin_override(who):
    c, uid, role = who
    role["v"] = "admin"
    body = {"investment_capital": 1000000, "trading_capital": 100000, "emergency_fund": 0}
    assert c.put("/api/me/profile", json=body).status_code == 422
    assert read_sql("SELECT count(*) AS n FROM user_profiles WHERE user_id = :u", params={"u": uid})["n"].iloc[0] == 0         # refused means nothing written
    ok = c.put("/api/me/profile", json={**body, "trading_override_note": "owner accepts a larger trading pot"})
    assert ok.status_code == 200 and ok.json()["trading_share_of_investable"] == pytest.approx(100000 / 1100000)
    assert "override_note" in audit(uid).iloc[-1]["payload"]


def test_a_viewer_can_read_but_not_write(who):
    c, uid, role = who
    role["v"] = "viewer"
    assert c.get("/api/me/profile").status_code == 200
    assert c.put("/api/me/profile", json={"mode": "trader"}).status_code == 403
    assert c.post("/api/portfolio/import", json={"pot": "investment", "csv_text": CSV}).status_code == 403
    assert c.post("/api/portfolio/imports/1/assign-pot", json={"pot": "investment"}).status_code == 403


def test_mode_audit_is_append_only(who):
    c, uid, _ = who
    c.put("/api/me/profile", json={"mode": "trader"})
    with pytest.raises(psycopg2.Error, match="append-only"):
        execute_sql("UPDATE mode_audit SET action = 'x' WHERE user_id = %s", (uid,))
    with pytest.raises(psycopg2.Error, match="append-only"):
        execute_sql("DELETE FROM mode_audit WHERE user_id = %s", (uid,))


def test_profiles_are_per_user(who):
    c, uid, _ = who
    c.put("/api/me/profile", json={"mode": "trader", "investment_capital": 500000})
    app.dependency_overrides[require_auth] = lambda: Principal(id="test-modes-other2", email="o@example.com", role="trader", authenticated=True)
    try:
        j = c.get("/api/me/profile").json()
        assert j["mode"] is None and j["investment_capital"] is None
    finally:
        pass


def test_nothing_is_preset_saving_other_fields_never_chooses_a_mode_for_you(who):
    c, uid, _ = who
    assert c.put("/api/me/profile", json={"investment_capital": 500000}).json()["mode"] is None
    assert c.put("/api/me/profile", json={"dependents": 2}).json()["mode"] is None
    r = c.get("/api/portfolio/summary?benchmark=none")                                       # no pot given and no mode chosen: asks instead of guessing
    assert r.status_code == 422 and "nothing is preset" in r.json()["detail"]
    assert c.put("/api/me/profile", json={"mode": "trader"}).json()["lens_pot"] == "trading"
