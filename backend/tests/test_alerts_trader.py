"""Trader alerts (pure rule): stop and target proximity, time stop, and limits filling up. Information only."""
import pandas as pd

from qsde.alerts import rules as R

CFG = R.load_config()
NOW = pd.Timestamp("2026-10-27 10:00", tz="Asia/Kolkata").tz_convert("UTC")          # Tuesday
M = {"open_risk_pct": 0.9, "open_risk_limit_pct": 3.0, "daily_limit_used": 0.1, "daily_pnl_pct": -0.1, "daily_loss_limit_pct": 1.5, "weekly_limit_used": 0.0, "weekly_pnl_pct": 0.0, "weekly_loss_limit_pct": 4.0}


def pos(**kw):
    base = {"id": 1, "symbol": "AAA", "entry": 100.0, "stop": 90.0, "target": 120.0, "last": 101.0, "time_stop": "2026-11-05"}
    base.update(kw)
    return base


def run(opens, meters=None):
    return R.rule_trader({"now": NOW, "trader": {"meters": {**M, **(meters or {})}, "open": opens}}, CFG)


def ids(out):
    return sorted(a["dedup_key"] for a in out)


def test_nothing_to_say_when_everything_is_comfortably_inside_its_levels():
    assert run([pos()]) == []
    assert R.rule_trader({"now": NOW, "trader": None}, CFG) == []


def test_stop_near_then_breached_and_target_near_then_reached():
    near = run([pos(last=92.0)])                                   # 2 of 10 left = 20%
    assert ids(near) == ["trader:1:stop_near"] and near[0]["severity"] == "warning" and "20%" in near[0]["detail"]
    hit = run([pos(last=90.0)])
    assert ids(hit) == ["trader:1:stop"] and hit[0]["severity"] == "critical" and "gapped" in hit[0]["detail"]
    tnear = run([pos(last=116.0)])                                 # 4 of 20 left = 20%
    assert ids(tnear) == ["trader:1:target_near"] and tnear[0]["severity"] == "info"
    assert ids(run([pos(last=121.0)])) == ["trader:1:target"]


def test_time_stop_is_flagged_one_session_ahead_today_and_after_it_passed():
    assert ids(run([pos(time_stop="2026-10-28")])) == ["trader:1:time"]            # tomorrow
    assert ids(run([pos(time_stop="2026-10-27")])) == ["trader:1:time"]            # today
    late = run([pos(time_stop="2026-10-26")])
    assert ids(late) == ["trader:1:time"] and "passed" in late[0]["title"]
    assert run([pos(time_stop="2026-10-29")]) == []                                 # two sessions away: quiet


def test_limits_warn_at_80_percent_and_turn_critical_when_reached():
    warn = run([], {"open_risk_pct": 2.5})
    assert ids(warn) == ["trader:limit:open_risk"] and warn[0]["severity"] == "warning"
    both = run([], {"daily_limit_used": 1.2, "weekly_limit_used": 0.85})
    sev = {a["dedup_key"]: a["severity"] for a in both}
    assert sev == {"trader:limit:daily": "critical", "trader:limit:weekly": "warning"}


def test_the_rule_is_registered_for_users_only():
    assert R.rule_trader in R.USER_RULES and R.rule_trader not in R.GLOBAL_RULES
