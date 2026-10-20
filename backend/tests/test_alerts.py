"""Alerts: rules (pure), the sync lifecycle (pure plan + a database round trip on a throwaway scope), and the API."""
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from qsde.alerts import rules as R
from qsde.alerts import store

CFG = R.load_config()
NOW = pd.Timestamp("2026-10-20 08:00", tz="UTC")                 # 13:30 IST on a Tuesday


def kinds(alerts):
    return sorted((a["rule_id"], a["severity"]) for a in alerts)


def test_expected_session_and_weekday_lag():
    tue_noon = pd.Timestamp("2026-10-20 12:00", tz="Asia/Kolkata")
    assert str(R.expected_last_session(tue_noon)) == "2026-10-19"
    assert str(R.expected_last_session(pd.Timestamp("2026-10-20 21:00", tz="Asia/Kolkata"))) == "2026-10-20"
    assert str(R.expected_last_session(pd.Timestamp("2026-10-18 10:00", tz="Asia/Kolkata"))) == "2026-10-16"            # Sunday: Friday's data is current
    assert R.weekdays_behind(pd.Timestamp("2026-10-16").date(), pd.Timestamp("2026-10-19").date()) == 1
    assert R.weekdays_behind(pd.Timestamp("2026-10-19").date(), pd.Timestamp("2026-10-19").date()) == 0


def test_data_staleness_thresholds_and_message_content():
    f = {"now": NOW, "data": {"nse_last": pd.Timestamp("2026-10-16").date(), "bse_last": pd.Timestamp("2026-10-15").date(), "amfi_last": pd.Timestamp("2026-10-10").date()}}
    a = R.rule_data_staleness(f, CFG)
    by = {x["dedup_key"]: x for x in a}
    assert "stale:nse_last" not in by                                       # one weekday behind is tolerated (a holiday can explain it)
    assert by["stale:bse_last"]["severity"] == "warning" and "Threshold" in by["stale:bse_last"]["detail"]
    assert by["stale:amfi_last"]["severity"] == "critical" and by["stale:amfi_last"]["payload"]["days"] == 10
    crit = R.rule_data_staleness({"now": NOW, "data": {"nse_last": pd.Timestamp("2026-10-12").date(), "bse_last": pd.Timestamp("2026-10-19").date(), "amfi_last": pd.Timestamp("2026-10-19").date()}}, CFG)
    assert kinds(crit) == [("data_staleness", "critical")]
    assert kinds(R.rule_data_staleness({"now": NOW, "data": {"nse_last": None, "bse_last": pd.Timestamp("2026-10-19").date()}}, CFG)) == [("data_staleness", "critical")]


def test_source_health_corporate_actions_and_kite():
    s = R.rule_source_health({"sources": [{"source": "a", "status": "ok", "consecutive_failures": 0}, {"source": "b", "status": "failing", "consecutive_failures": 3, "last_error": "timeout"},
                                           {"source": "c", "status": "ok", "consecutive_failures": 1}]}, CFG)
    assert {x["entity_id"]: x["severity"] for x in s} == {"b": "critical", "c": "warning"}
    ca = R.rule_corporate_actions({"corp_actions": {"unreviewed_research_only": 151, "candidates_open": 12, "upcoming": [{"symbol": "TCS", "ex_date": "2026-10-22", "subject": "Bonus 1:1"}]}}, CFG)
    assert kinds(ca) == [("ca_conflicts", "warning"), ("ca_unreviewed", "warning"), ("ca_upcoming", "info")]
    assert R.rule_corporate_actions({"corp_actions": {"unreviewed_research_only": 0, "candidates_open": 0, "upcoming": []}}, CFG) == []
    assert kinds(R.rule_kite({"now": NOW, "kite": {"has_token": False}}, CFG)) == [("kite_token", "info")]
    assert kinds(R.rule_kite({"now": NOW, "kite": {"has_token": True, "expires_at": NOW - pd.Timedelta(hours=1)}}, CFG)) == [("kite_token", "warning")]
    assert R.rule_kite({"now": NOW, "kite": {"has_token": True, "expires_at": NOW + pd.Timedelta(hours=9)}}, CFG) == []
    assert R.rule_kite({"now": NOW}, CFG) == []


def model_fact(**h):
    return {"model_version": "m1", "horizon_id": "position_20d", "health": h}


def test_model_rules_kill_switch_degradation_drift_and_logging():
    f = {"now": NOW, "models": [model_fact(kill_switch=["calibration slope 0.20 < 0.3"], retrain={"trigger": True, "kinds": ["drift"], "reasons": ["drift: 6 features"]},
                                           score_drift={"psi": 0.4, "flag": True, "reference": "first 20 dates", "current": "last 20 dates"})]}
    assert kinds(R.rule_models(f, CFG)) == [("model_degradation", "warning"), ("model_drift", "warning"), ("model_kill_switch", "critical")]
    assert R.rule_models({"now": NOW, "models": [model_fact(kill_switch=[], retrain={"trigger": False, "kinds": []})]}, CFG) == []
    due = (pd.Timestamp("2026-10-21 09:15", tz="Asia/Kolkata")).isoformat()
    never = R.rule_models({"now": NOW, "logging": [{"model_version": "m1", "signal_date": "2026-10-20", "last_logged": None, "deadline": due}]}, CFG)
    assert kinds(never) == [("model_log_due", "info")] and "has not started" in never[0]["title"]
    pending = R.rule_models({"now": NOW, "logging": [{"model_version": "m1", "signal_date": "2026-10-20", "last_logged": "2026-10-19", "deadline": due}]}, CFG)
    assert kinds(pending) == [("model_log_due", "info")]
    late = R.rule_models({"now": pd.Timestamp("2026-10-21 05:00", tz="UTC"), "logging": [{"model_version": "m1", "signal_date": "2026-10-20", "last_logged": "2026-10-19", "deadline": due}]}, CFG)
    assert kinds(late) == [("model_log_missed", "warning")] and "permanent gap" in late[0]["detail"]
    ok = R.rule_models({"now": NOW, "logging": [{"model_version": "m1", "signal_date": "2026-10-20", "last_logged": "2026-10-20", "deadline": due}]}, CFG)
    assert ok == []


def test_portfolio_rules_concentration_correlation_drawdown_exposure():
    w = {"A": 0.30, "B": 0.20, "C": 0.15, "D": 0.12, "E": 0.10, "F": 0.08, "G": 0.05}
    f = {"portfolio": {"weights": w, "avg_pairwise_correlation": 0.72, "top_pair": ("A", "B", 0.93), "current_drawdown": -0.27, "high_risk_share": 0.5, "regime": "risk_off"}}
    a = R.rule_portfolio(f, CFG)
    by = {x["dedup_key"]: x for x in a}
    assert by["conc:name:A"]["severity"] == "critical" and by["conc:name:B"]["severity"] == "warning" and by["conc:name:C"]["severity"] == "warning" and "conc:name:D" not in by
    assert "conc:top5" in by and by["corr:avg"]["detail"].count("A / B") == 1 and by["dd:current"]["severity"] == "critical" and "exposure:risk_off" in by
    assert all(x["scope"] == "user" for x in a)
    calm = {"portfolio": {"weights": {f"S{i}": 0.05 for i in range(20)}, "avg_pairwise_correlation": 0.3, "top_pair": None, "current_drawdown": -0.05, "high_risk_share": 0.1, "regime": "neutral"}}
    assert R.rule_portfolio(calm, CFG) == [] and R.rule_portfolio({}, CFG) == []


def test_plan_sync_opens_refreshes_resolves_and_keeps_acknowledgement_unless_worse():
    live = [{"id": 1, "dedup_key": "a", "severity": "warning", "state": "acknowledged"}, {"id": 2, "dedup_key": "b", "severity": "info", "state": "open"}, {"id": 3, "dedup_key": "c", "severity": "warning", "state": "open"}]
    cond = [{"dedup_key": "a", "severity": "warning"}, {"dedup_key": "b", "severity": "critical"}, {"dedup_key": "d", "severity": "info"}, {"dedup_key": "d", "severity": "info"}]
    p = store.plan_sync(live, cond)
    assert [c["dedup_key"] for c in p["insert"]] == ["d"] and p["resolve"] == [3]
    up = {u["id"]: u for u in p["update"]}
    assert up[1]["reopen"] is False and up[2]["reopen"] is False                                  # an unacknowledged alert simply escalates in place
    worse = store.plan_sync([{"id": 9, "dedup_key": "x", "severity": "warning", "state": "acknowledged"}], [{"dedup_key": "x", "severity": "critical"}])
    assert worse["update"][0]["reopen"] is True


def test_database_round_trip_is_idempotent_and_acknowledgement_survives_refresh():
    from qsde.db.connection import execute_sql, get_sync_conn, read_sql
    uid = "test-alerts-user"
    c1 = R.alert("t", "warning", "Test condition", "detail one", "t:one", scope="user", entity_type="x", entity_id="1")
    try:
        with get_sync_conn() as conn:
            assert store.sync(conn, uid, [c1]) == {"opened": 1, "refreshed": 0, "resolved": 0}
        with get_sync_conn() as conn:
            assert store.sync(conn, uid, [{**c1, "detail": "detail two"}]) == {"opened": 0, "refreshed": 1, "resolved": 0}
        row = read_sql("SELECT * FROM alert_events WHERE user_id = :u", params={"u": uid}).iloc[0]
        assert row["occurrences"] == 2 and row["detail"] == "detail two" and row["state"] == "open"
        with get_sync_conn() as conn:
            assert store.acknowledge(conn, int(row["id"]), "me", uid) is True
            assert store.acknowledge(conn, int(row["id"]), "me", uid) is False                  # already acknowledged
            assert store.acknowledge(conn, int(row["id"]), "me", "someone-else") is False       # not theirs
        with get_sync_conn() as conn:
            store.sync(conn, uid, [c1])
        assert read_sql("SELECT state FROM alert_events WHERE user_id = :u", params={"u": uid})["state"].iloc[0] == "acknowledged"
        with get_sync_conn() as conn:
            assert store.sync(conn, uid, []) == {"opened": 0, "refreshed": 0, "resolved": 1}
        r = read_sql("SELECT state, resolved_why FROM alert_events WHERE user_id = :u", params={"u": uid}).iloc[0]
        assert r["state"] == "resolved" and r["resolved_why"] == "condition cleared"
    finally:
        execute_sql("DELETE FROM alert_events WHERE user_id = %s", (uid,))


@pytest.fixture()
def client(monkeypatch):
    from api.routes import alerts
    from api.main import app
    d = pd.DataFrame([{"id": 1, "user_id": None, "rule_id": "kite_token", "severity": "info", "state": "open", "title": "No Kite session", "detail": "d", "entity_type": "broker", "entity_id": "kite",
                       "first_seen": pd.Timestamp("2026-10-20", tz="UTC"), "last_seen": pd.Timestamp("2026-10-20", tz="UTC"), "occurrences": 3, "acked_by": None, "acked_at": None,
                       "resolved_at": None, "resolved_why": None, "payload": {}}])
    monkeypatch.setattr(alerts.store, "list_alerts", lambda rs, u, st, sv, lim, off: (1, d))
    monkeypatch.setattr(alerts, "_last_run", lambda uid: pd.Timestamp.now(tz="UTC"))
    return TestClient(app)


def test_alerts_api_shapes_rows_validates_input_and_has_no_destructive_endpoint(client):
    j = client.get("/api/alerts").json()
    assert j["n_matching"] == 1 and j["rows"][0]["scope"] == "system" and j["rows"][0]["occurrences"] == 3 and j["last_run"]
    for bad in ("state=deleted", "severity=fatal", "limit=1000", "offset=-1"):
        assert client.get(f"/api/alerts?{bad}").status_code == 422
    from api.main import app
    paths = {k: set(v) for k, v in app.openapi()["paths"].items() if k.startswith("/api/alerts")}
    assert paths["/api/alerts"] == {"get"} and paths["/api/alerts/summary"] == {"get"} and paths["/api/alerts/refresh"] == {"post"} and paths["/api/alerts/{alert_id}/acknowledge"] == {"post"}
    assert not any("delete" in v for v in paths.values())
