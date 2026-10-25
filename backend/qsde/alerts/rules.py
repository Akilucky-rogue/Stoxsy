"""Alert rules (pure): facts in, a list of conditions out. No database access here; `facts.py` gathers the facts and `engine.py` stores the result.

Every alert states the observed value and the threshold it crossed, says what it means for the product, and carries a stable `dedup_key` so a condition that keeps
holding is ONE alert that is refreshed, not a new one each run. An alert is information: it never places, changes or cancels anything.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

CONFIG = Path(__file__).resolve().parents[3] / "infra" / "data" / "alert_rules.json"
SEVERITIES = ("info", "warning", "critical")


def load_config(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def alert(rule_id: str, severity: str, title: str, detail: str, dedup_key: str, scope: str = "global", entity_type: Optional[str] = None,
          entity_id: Optional[str] = None, payload: Optional[dict] = None) -> dict:
    assert severity in SEVERITIES
    return {"rule_id": rule_id, "severity": severity, "title": title, "detail": detail, "dedup_key": dedup_key, "scope": scope, "entity_type": entity_type,
            "entity_id": entity_id, "payload": payload or {}}


# ── calendar helpers (weekday sessions; exchange holidays are not known here, so thresholds tolerate one or two days) ─────

def expected_last_session(now_ist: pd.Timestamp, after_hours: int = 20) -> date:
    """The latest weekday whose end-of-day data should already exist: today once the evening load window has begun, otherwise the previous weekday."""
    d = now_ist.date()
    if d.weekday() < 5 and now_ist.hour >= after_hours:
        return d
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def weekdays_behind(last: date, expected: date) -> int:
    if last >= expected:
        return 0
    return int(len(pd.bdate_range(pd.Timestamp(last) + pd.Timedelta(days=1), pd.Timestamp(expected))))


# ── rules ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def rule_data_staleness(facts: dict, cfg: dict) -> list:
    c = cfg["data_staleness"]
    now_ist = pd.Timestamp(facts["now"]).tz_convert("Asia/Kolkata")
    exp = expected_last_session(now_ist, c["after_hours_ist"])
    out = []
    for key, label, warn, crit in (("nse_last", "NSE end-of-day prices", c["nse_weekdays_warn"], c["nse_weekdays_critical"]),
                                   ("bse_last", "BSE end-of-day prices", c["bse_weekdays_warn"], c["bse_weekdays_critical"]),
                                   ("fno_last", "NSE F&O end-of-day archive", c["bse_weekdays_warn"], c["bse_weekdays_critical"])):
        if key not in facts.get("data", {}):
            continue                                                      # a source that is not part of this run's facts is not judged
        last = facts["data"][key]
        if last is None:
            out.append(alert("data_staleness", "critical", f"{label}: no data loaded", "The source table is empty.", f"stale:{key}", entity_type="source", entity_id=key))
            continue
        lag = weekdays_behind(pd.Timestamp(last).date(), exp)
        if lag >= warn:
            sev = "critical" if lag >= crit else "warning"
            out.append(alert("data_staleness", sev, f"{label} are {lag} weekdays behind",
                             f"Latest session loaded: {pd.Timestamp(last).date()}; expected by now: {exp}. Threshold: {warn} weekday(s) warning, {crit} critical. "
                             "An exchange holiday can explain one or two days; signals and decision cards are computed from the last loaded session.",
                             f"stale:{key}", entity_type="source", entity_id=key, payload={"last": str(pd.Timestamp(last).date()), "expected": str(exp), "weekdays_behind": lag}))
    a = facts.get("data", {}).get("amfi_last")
    if a is not None:
        days = (now_ist.date() - pd.Timestamp(a).date()).days
        if days >= c["amfi_days_warn"]:
            sev = "critical" if days >= c["amfi_days_critical"] else "warning"
            out.append(alert("data_staleness", sev, f"AMFI NAVs are {days} days old", f"Latest NAV date loaded: {pd.Timestamp(a).date()}. Threshold: {c['amfi_days_warn']} days warning, "
                             f"{c['amfi_days_critical']} critical. Fund returns and the funds page show the last loaded NAV.", "stale:amfi_last", entity_type="source", entity_id="amfi_last",
                             payload={"last": str(pd.Timestamp(a).date()), "days": days}))
    return out


def rule_source_health(facts: dict, cfg: dict) -> list:
    c = cfg["source_health"]
    out = []
    for s in facts.get("sources", []):
        n = int(s.get("consecutive_failures") or 0)
        if s.get("status") != "ok" or n >= c["failures_warn"]:
            sev = "critical" if n >= c["failures_critical"] else "warning"
            out.append(alert("source_health", sev, f"Source {s['source']} is failing ({n} in a row)", f"Status: {s.get('status')}. Last error: {str(s.get('last_error') or 'none recorded')[:300]}. "
                             f"Threshold: {c['failures_warn']} failure(s) warning, {c['failures_critical']} critical.", f"source:{s['source']}", entity_type="source", entity_id=s["source"],
                             payload={"consecutive_failures": n, "status": s.get("status")}))
    return out


def rule_corporate_actions(facts: dict, cfg: dict) -> list:
    c, ca = cfg["corporate_actions"], facts.get("corp_actions", {})
    out = []
    n = int(ca.get("unreviewed_research_only", 0))
    if n >= c["unreviewed_research_only_warn"]:
        out.append(alert("ca_unreviewed", "warning", f"{n} corporate-action events are applied in research only, not confirmed",
                         "These price-only split candidates are adjusted in the research materialisation but NOT in the official series, so official prices may show an unadjusted gap on those names. "
                         "They need the owner's event-by-event decision (see the Bucket A packet).", "ca:unreviewed_research", entity_type="corporate_actions", payload={"count": n}))
    k = int(ca.get("candidates_open", 0))
    if k >= c["candidates_open_warn"]:
        out.append(alert("ca_conflicts", "warning", f"{k} corporate-action candidates are unresolved or awaiting review",
                         "The reconciler found price events that do not match any official record cleanly (unresolved or candidate status).", "ca:candidates_open",
                         entity_type="corporate_actions", payload={"count": k}))
    for u in ca.get("upcoming", []):
        out.append(alert("ca_upcoming", "info", f"{u['symbol']}: corporate action around its ex-date {u['ex_date']}",
                         f"{u['subject']}. The name is in the current defensive-signal universe: expect a price gap near the ex-date; stops and returns across it need the adjusted series.",
                         f"ca:upcoming:{u['symbol']}:{u['ex_date']}", entity_type="symbol", entity_id=u["symbol"], payload=u))
    return out


def rule_price_series(facts: dict, cfg: dict) -> list:
    s = facts.get("series")
    if not s or not (s["applied_not_in_registry"] or s["registry_not_applied"]):
        return []
    return [alert("price_series_mismatch", "warning", "The official price series and the governed adjustment registry disagree",
                  f"{s['applied_not_in_registry']} events are adjusted in the official `ohlcv` series (the one the cards, signals and portfolio read) but are not in the governed registry "
                  f"(these include the Bucket A events awaiting your decision), and {s['registry_not_applied']} governed events are not applied in `ohlcv`. "
                  f"(`ohlcv`: {s['applied_in_ohlcv']} events, labelled official_prices_bucket_a_policy_v1 (31 confirmed, 34 held and applied provisionally, 4 rejected); registry: {s['registry_events']} events, labelled governed_research_series_not_production.) "
                  "Owner decision: `ohlcv` stays unchanged until all Bucket A events are adjudicated, then it is rebuilt once from the reconciled registry (versioned, prior series kept, before/after cliff and continuity reports).",
                  "series:mismatch", entity_type="corporate_actions", payload=s)]


def rule_bucket_a(facts: dict, cfg: dict) -> list:
    b = facts.get("bucket_a")
    if not b or b.get("pending") is None:
        return []
    if b["pending"] == 0 and b["needs_more_evidence"] == 0:
        return []
    return [alert("ca_bucket_a_progress", "info", f"Bucket A adjudication: {b['decided']} of {b['total']} decided, {b['pending']} pending",
                  f"{b['confirm']} confirmed, {b['needs_more_evidence']} held for more evidence, {b['reject']} rejected, {b['pending']} not yet reviewed. The official `ohlcv` rebuild waits for every event to be adjudicated.",
                  "ca:bucket_a_progress", entity_type="corporate_actions", payload={k: b[k] for k in ("total", "confirm", "needs_more_evidence", "reject", "pending")})]


def rule_bse_corporate_actions(facts: dict, cfg: dict) -> list:
    b = facts.get("bse_ca")
    if b is None:
        return []
    if b["files"] == 0:
        return [alert("bse_ca_source", "info", "No BSE corporate-action export has been ingested",
                      "BSE-only names stay out of the universe, the defensive signal and decision cards until BSE actions are ingested, reconciled and a BSE adjusted series is validated. "
                      "BSE's API rejects automated clients: download the export from BSE's Corporate Actions page and save it in the inbox folder data/inbox/bse_corporate_actions, then run scripts/load_bse_corporate_actions.py --inbox.",
                      "bse_ca:none", entity_type="source", entity_id="bse_corporate_actions")]
    return []


def rule_models(facts: dict, cfg: dict) -> list:
    out = []
    now = pd.Timestamp(facts["now"])
    for m in facts.get("models", []):
        key = f"{m['model_version']}|{m['horizon_id']}"
        h = m.get("health") or {}
        if h.get("kill_switch"):
            out.append(alert("model_kill_switch", "critical", f"Kill-switch conditions met: {m['model_version']} ({m['horizon_id']})",
                             "; ".join(h["kill_switch"]) + ". Halting a model's signals is always safe; resuming needs the owner.", f"kill:{key}", entity_type="model", entity_id=m["model_version"],
                             payload={"reasons": h["kill_switch"]}))
        r = h.get("retrain") or {}
        if r.get("trigger") and "drift" in r.get("kinds", []):
            out.append(alert("model_degradation", "warning", f"Model evidence is degrading: {m['model_version']} ({m['horizon_id']})", "; ".join(r.get("reasons", [])) +
                             ". A retrain can only produce a candidate; the live model is unchanged.", f"degrade:{key}", entity_type="model", entity_id=m["model_version"], payload={"kinds": r.get("kinds")}))
        sd = h.get("score_drift")
        if sd and sd.get("flag"):
            out.append(alert("model_drift", "warning", f"Score distribution drift: {m['model_version']} ({m['horizon_id']})", f"PSI {sd['psi']:.2f} between {sd['reference']} and {sd['current']} (threshold 0.25).",
                             f"drift:{key}", entity_type="model", entity_id=m["model_version"], payload=sd))
    for lg in facts.get("logging", []):
        due = pd.Timestamp(lg["deadline"])
        sd = pd.Timestamp(lg["signal_date"]).date()
        if lg["last_logged"] is not None and pd.Timestamp(lg["last_logged"]) >= pd.Timestamp(lg["signal_date"]):
            continue
        if lg["last_logged"] is None:
            out.append(alert("model_log_due", "info", "Forward prediction logging has not started",
                             f"{lg['model_version']}: nothing has been logged yet. Run scripts/log_cards.py after an end-of-day load and before the next session opens "
                             f"(for the {sd} signal that deadline was {due.tz_convert('Asia/Kolkata'):%d %b %H:%M} IST; a signal logged after its deadline is refused).",
                             f"logstart:{lg['model_version']}", entity_type="model", entity_id=lg["model_version"], payload=lg))
        elif now >= due:
            out.append(alert("model_log_missed", "warning", f"Predictions for {sd} were not logged in time",
                             f"{lg['model_version']}: the signal for {sd} was not logged before {due.tz_convert('Asia/Kolkata'):%d %b %H:%M} IST (the next session's open). "
                             "Late logging is refused because the entry price is already known, so the shadow series has a permanent gap for that session.", f"logmiss:{lg['model_version']}:{sd}",
                             entity_type="model", entity_id=lg["model_version"], payload=lg))
        else:
            out.append(alert("model_log_due", "info", f"Predictions for {sd} are due", f"Run scripts/log_cards.py before {due.tz_convert('Asia/Kolkata'):%d %b %H:%M} IST.",
                             f"logdue:{lg['model_version']}:{sd}", entity_type="model", entity_id=lg["model_version"], payload=lg))
    return out


def rule_kite(facts: dict, cfg: dict) -> list:
    k = facts.get("kite")
    if k is None:
        return []
    now = pd.Timestamp(facts["now"])
    if not k.get("has_token"):
        return [alert("kite_token", "info", "No Kite session", "Live quotes, instrument master and F&O data need a Kite login. Nothing in the current product depends on it; log in when you want F&O research data enabled.",
                      "kite:none", entity_type="broker", entity_id="kite")]
    exp = k.get("expires_at")
    if exp is None:
        return []
    left = (pd.Timestamp(exp) - now).total_seconds() / 3600.0
    if left <= 0:
        return [alert("kite_token", "warning", "Kite session has expired", f"The access token expired at {pd.Timestamp(exp):%d %b %H:%M} UTC. Log in again to resume live data.", "kite:expired", entity_type="broker", entity_id="kite")]
    if left <= cfg["kite"]["expiry_warn_hours"]:
        return [alert("kite_token", "warning", f"Kite session expires in {left:.1f} h", "Log in again before it lapses if you need live data.", "kite:expiring", entity_type="broker", entity_id="kite")]
    return []


def rule_portfolio(facts: dict, cfg: dict) -> list:
    p, c = facts.get("portfolio"), cfg["portfolio"]
    if not p:
        return []
    out = []
    w = pd.Series(p["weights"]).sort_values(ascending=False)
    for sym, v in w.items():
        if v >= c["single_name_warn"]:
            sev = "critical" if v >= c["single_name_critical"] else "warning"
            out.append(alert("portfolio_concentration", sev, f"{sym} is {v:.0%} of your portfolio", f"Threshold: {c['single_name_warn']:.0%} warning, {c['single_name_critical']:.0%} critical for one name "
                             "(the decision cards cap a name at 8%).", f"conc:name:{sym}", scope="user", entity_type="symbol", entity_id=sym, payload={"weight": float(v)}))
    top5 = float(w.head(5).sum()) if len(w) > 5 else None
    if top5 is not None and top5 >= c["top5_warn"]:
        out.append(alert("portfolio_concentration", "warning", f"Your top 5 holdings are {top5:.0%} of the portfolio", f"Threshold: {c['top5_warn']:.0%}.", "conc:top5", scope="user",
                         entity_type="portfolio", payload={"top5": top5}))
    corr = p.get("avg_pairwise_correlation")
    if corr is not None and corr >= c["avg_pairwise_correlation_warn"]:
        tp = p.get("top_pair")
        out.append(alert("portfolio_correlation", "warning", f"Your holdings move together (average correlation {corr:.2f})",
                         f"Threshold: {c['avg_pairwise_correlation_warn']:.2f} over the last {c['correlation_lookback_sessions']} sessions" + (f"; most correlated pair: {tp[0]} / {tp[1]} ({tp[2]:.2f})" if tp else "") +
                         ". Highly correlated holdings are closer to one position than their count suggests.", "corr:avg", scope="user", entity_type="portfolio", payload={"avg_corr": corr}))
    dd = p.get("current_drawdown")
    if dd is not None and dd <= c["drawdown_warn"]:
        sev = "critical" if dd <= c["drawdown_critical"] else "warning"
        out.append(alert("portfolio_drawdown", sev, f"Portfolio is {dd:.0%} below its 1-year peak", f"Pro-forma (today's holdings held constant over the window). Threshold: {c['drawdown_warn']:.0%} warning, "
                         f"{c['drawdown_critical']:.0%} critical.", "dd:current", scope="user", entity_type="portfolio", payload={"current_drawdown": dd}))
    hr, regime = p.get("high_risk_share"), p.get("regime")
    if regime == "risk_off" and hr is not None and hr >= c["high_risk_share_warn"]:
        out.append(alert("portfolio_exposure", "warning", f"Risk-off regime with {hr:.0%} of the portfolio in the highest-risk buckets",
                         f"The universe regime is risk-off while {hr:.0%} of your value sits in defensive buckets 1-2 (threshold {c['high_risk_share_warn']:.0%}).", "exposure:risk_off", scope="user",
                         entity_type="portfolio", payload={"high_risk_share": hr, "regime": regime}))
    return out


def _market_open(now_ist: pd.Timestamp, c: dict) -> bool:
    if now_ist.weekday() >= 5:
        return False
    m = now_ist.hour * 60 + now_ist.minute
    return c["market_open_ist_minutes"] <= m <= c["market_close_ist_minutes"]


def rule_ops(facts: dict, cfg: dict) -> list:
    """Is the live stream still alive, and has the daily routine run? Judged only for components that have reported (or that the owner declared expected on the always-on host):
    a stream that was never started is not an alarm, a stream that stopped beating inside the market session is."""
    beats = {b["component"]: b for b in (facts.get("ops") or [])}
    c = cfg.get("ops")
    if c is None:
        return []
    now = pd.Timestamp(facts["now"])
    now_ist = now.tz_convert("Asia/Kolkata")
    out = []
    s = beats.get("stream")
    expected = c.get("stream_expected", False)
    if (s is not None or expected) and _market_open(now_ist, c):
        if s is None:
            out.append(alert("ops_stream", "critical", "Live stream is not running", "The stream is expected during market hours on this host but has never reported. Start it or check the service.",
                             "ops:stream:missing", entity_type="pipeline", entity_id="stream"))
        else:
            last_beat = pd.Timestamp(s["last_beat"])
            age = (now - last_beat).total_seconds()
            beat_today = last_beat.tz_convert("Asia/Kolkata").date() == now_ist.date() or expected
            if beat_today and s["status"] != "stopped" and age > c["stream_beat_stale_seconds"]:
                sev = "critical" if age > c["stream_beat_critical_seconds"] else "warning"
                out.append(alert("ops_stream", sev, f"Live stream stopped reporting {age / 60:.0f} min ago",
                                 f"The last heartbeat was at {last_beat.tz_convert('Asia/Kolkata'):%H:%M} IST on {s.get('host')}. The machine may have slept or lost power, or the service crashed. "
                                 "Live bars after that are missing and are filled from Kite history at the close.", "ops:stream:stale", entity_type="pipeline", entity_id="stream",
                                 payload={"age_seconds": age}))
            else:
                det = s.get("detail") or {}
                tick_age = det.get("last_tick_age_s")
                if s["status"] == "ok" and tick_age is not None and tick_age > c["stream_silent_tick_seconds"] and det.get("connected"):
                    out.append(alert("ops_stream", "warning", f"Stream is connected but silent for {tick_age / 60:.0f} min",
                                     "The socket is open and no price tick has arrived. Check the market session and the symbols subscribed.", "ops:stream:silent", entity_type="pipeline", entity_id="stream"))
    d = beats.get("daily_pipeline")
    if d is not None or c.get("daily_expected", False):
        exp = expected_last_session(now_ist, c["daily_after_hours_ist"])
        last = None if d is None else pd.Timestamp(d["last_beat"]).tz_convert("Asia/Kolkata").date()
        if d is None or weekdays_behind(last, exp) > c["daily_weekdays_warn"]:
            out.append(alert("ops_daily", "warning", "The daily routine has not completed" + ("" if d is None else f" since {last}"),
                             "End-of-day data, signals, reconciliation and alerts depend on it. Run the daily command or check the scheduler.", "ops:daily:stale", entity_type="pipeline", entity_id="daily_pipeline"))
        elif d["status"] == "failed":
            out.append(alert("ops_daily", "warning", "The last daily routine finished with failures", "Failed steps: " + ", ".join((d.get("detail") or {}).get("failed", [])) + ".", "ops:daily:failed",
                             entity_type="pipeline", entity_id="daily_pipeline"))
    return out


GLOBAL_RULES = (rule_data_staleness, rule_source_health, rule_corporate_actions, rule_price_series, rule_bucket_a, rule_bse_corporate_actions, rule_models, rule_kite, rule_ops)
USER_RULES = (rule_portfolio,)
