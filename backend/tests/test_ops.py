"""Pipeline liveness: the alert rule for a silent stream and a missed daily routine, and the heartbeat round trip."""
import pandas as pd
import pytest

from qsde.alerts import rules as R

CFG = R.load_config()
UTC = "UTC"


def now_ist(h, m=0, day="2026-10-26"):                      # 2026-10-26 is a Monday
    return pd.Timestamp(f"{day} {h:02d}:{m:02d}", tz="Asia/Kolkata").tz_convert(UTC)


def beat(component, age_min, status="ok", at=None, detail=None, host="mini"):
    at = at or now_ist(11)
    return {"component": component, "last_beat": at - pd.Timedelta(minutes=age_min), "status": status, "host": host, "detail": detail or {}}


def run(now, beats, **over):
    cfg = {**CFG, "ops": {**CFG["ops"], **over}}
    return R.rule_ops({"now": now, "ops": beats}, cfg)


def kinds(a):
    return [(x["rule_id"], x["severity"], x["dedup_key"]) for x in a]


def test_a_stream_that_never_started_is_not_an_alarm_unless_the_host_declares_it_expected():
    assert run(now_ist(11), []) == []
    a = run(now_ist(11), [], stream_expected=True)
    assert kinds(a) == [("ops_stream", "critical", "ops:stream:missing")]


def test_a_stream_that_stops_beating_in_the_session_warns_then_goes_critical():
    now = now_ist(11)
    assert run(now, [beat("stream", 1)]) == []                                          # beating
    w = run(now, [beat("stream", 5)])
    assert kinds(w) == [("ops_stream", "warning", "ops:stream:stale")] and "slept" in w[0]["detail"]
    c = run(now, [beat("stream", 25)])
    assert kinds(c) == [("ops_stream", "critical", "ops:stream:stale")]


def test_nothing_is_judged_outside_market_hours_for_a_stopped_stream_or_a_beat_from_a_previous_day():
    assert run(now_ist(17), [beat("stream", 120, at=now_ist(17))]) == []                # after the close
    assert run(now_ist(8), [beat("stream", 600, at=now_ist(8))]) == []                  # before the open
    assert run(now_ist(11, day="2026-10-24"), [beat("stream", 5, at=now_ist(11, day="2026-10-24"))]) == []     # Saturday
    assert run(now_ist(11), [beat("stream", 5, status="stopped")]) == []                # a clean stop is not an incident
    old = beat("stream", 0, at=now_ist(15, day="2026-10-23"))                           # last beat is from Friday: not today's session
    assert run(now_ist(10), [old]) == []
    assert kinds(run(now_ist(10), [old], stream_expected=True)) == [("ops_stream", "critical", "ops:stream:stale")]


def test_a_connected_but_silent_socket_is_reported():
    d = {"connected": True, "last_tick_age_s": 700.0}
    assert kinds(run(now_ist(11), [beat("stream", 0, detail=d)])) == [("ops_stream", "warning", "ops:stream:silent")]
    assert run(now_ist(11), [beat("stream", 0, detail={"connected": True, "last_tick_age_s": 5.0})]) == []
    assert run(now_ist(11), [beat("stream", 0, detail={"connected": False, "last_tick_age_s": 900})]) == []     # disconnected is a different problem (the status says degraded)


def test_the_daily_routine_is_judged_after_its_window_and_failures_are_named():
    now = now_ist(21)                                                                  # Monday evening: today's run is due
    done = beat("daily_pipeline", 30, at=now)
    assert run(now, [done]) == []
    stale = beat("daily_pipeline", 0, at=now_ist(21, day="2026-10-22"))                # last ran Thursday
    assert kinds(run(now, [stale])) == [("ops_daily", "warning", "ops:daily:stale")]
    failed = beat("daily_pipeline", 30, status="failed", at=now, detail={"failed": ["nse_cash", "alerts"]})
    out = run(now, [failed])
    assert kinds(out) == [("ops_daily", "warning", "ops:daily:failed")] and "nse_cash, alerts" in out[0]["detail"]
    assert kinds(run(now, [], daily_expected=True)) == [("ops_daily", "warning", "ops:daily:stale")]
    assert run(now, []) == []                                                          # not expected, never reported


def test_heartbeat_round_trip_and_a_database_failure_never_breaks_the_caller(monkeypatch):
    from qsde.db.connection import execute_sql
    from qsde.ops import heartbeat as H

    try:
        assert H.beat("zz_test", "degraded", {"x": 1}) is True
        row = [r for r in H.read_all() if r["component"] == "zz_test"][0]
        assert row["status"] == "degraded" and row["detail"] == {"x": 1} and row["host"]
        with pytest.raises(ValueError):
            H.beat("zz_test", "excellent")
        import qsde.db.connection as C
        monkeypatch.setattr(C, "get_sync_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        assert H.beat("zz_test") is False
    finally:
        monkeypatch.undo()
        execute_sql("DELETE FROM ops_heartbeat WHERE component = 'zz_test'")
