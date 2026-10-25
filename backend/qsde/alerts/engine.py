"""Evaluate the alert rules and synchronise the stored alerts. Idempotent: running it twice in a row changes nothing but `last_seen` / `occurrences`."""
from __future__ import annotations

from typing import Optional

import pandas as pd
import psycopg2.extras

from qsde.alerts import facts as F
from qsde.alerts import rules as R
from qsde.alerts import store
from qsde.db.connection import get_sync_conn


def _gather(name: str, fn, errors: list):
    try:
        return fn()
    except Exception as e:      # noqa: BLE001  a failing gatherer must be visible, never silent
        errors.append((name, f"{type(e).__name__}: {str(e)[:200]}"))
        return None


def _error_alerts(errors: list, scope: str) -> list:
    return [R.alert("alert_rule_error", "warning", f"Alert check could not run: {n}", f"{msg}. Alerts that depend on it are not being evaluated.", f"ruleerr:{n}", scope=scope,
                    entity_type="alert_engine", entity_id=n) for n, msg in errors]


def collect_global(cfg: dict, now: pd.Timestamp) -> tuple[list, list]:
    errors: list = []
    f = {"now": now, "data": _gather("data dates", F.data_dates, errors) or {}, "sources": _gather("source health", F.sources, errors) or [],
         "corp_actions": _gather("corporate actions", lambda: F.corp_actions(cfg, now), errors) or {}, "models": _gather("model health", F.models, errors) or [],
         "logging": _gather("prediction logging", F.logging_status, errors) or [], "kite": _gather("kite session", F.kite, errors),
         "series": _gather("price-series consistency", F.series_consistency, errors), "bse_ca": _gather("BSE corporate actions", F.bse_corporate_actions, errors),
         "bucket_a": _gather("Bucket A decisions", F.bucket_a, errors), "ops": _gather("pipeline heartbeats", F.ops, errors)}
    f["models"] = [{**m, "model_version": m["model_version"], "horizon_id": m["horizon_id"]} for m in f["models"]]
    out = []
    for rule in R.GLOBAL_RULES:
        try:
            out += rule(f, cfg)
        except Exception as e:      # noqa: BLE001
            errors.append((rule.__name__, f"{type(e).__name__}: {str(e)[:200]}"))
    return out + _error_alerts(errors, "global"), errors


def collect_user(user_id: str, cfg: dict, now: pd.Timestamp) -> tuple[list, list]:
    errors: list = []
    p = _gather("portfolio", lambda: F.portfolio(user_id, cfg), errors)
    out = []
    for rule in R.USER_RULES:
        try:
            out += rule({"now": now, "portfolio": p}, cfg)
        except Exception as e:      # noqa: BLE001
            errors.append((rule.__name__, f"{type(e).__name__}: {str(e)[:200]}"))
    return out + _error_alerts(errors, "user"), errors


def _run(conn, scope: str, user_id: Optional[str], conditions: list, errors: list) -> dict:
    cur = conn.cursor()
    cur.execute("INSERT INTO alert_runs (scope) VALUES (%s) RETURNING id", (scope,))
    rid = cur.fetchone()["id"]
    res = store.sync(conn, user_id, conditions)
    cur.execute("UPDATE alert_runs SET finished_at=NOW(), n_conditions=%s, opened=%s, resolved=%s, errors=%s WHERE id=%s",
                (len(conditions), res["opened"], res["resolved"], psycopg2.extras.Json([{"check": n, "error": m} for n, m in errors]), rid))
    return {**res, "conditions": len(conditions), "errors": errors}


def evaluate(user_id: Optional[str] = None, include_global: bool = True, now: Optional[pd.Timestamp] = None, cfg: Optional[dict] = None) -> dict:
    """Recompute and store. user_id=None evaluates the system-wide rules only; with a user id it also evaluates that user's portfolio rules."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    cfg = cfg or R.load_config()
    out: dict = {}
    if include_global:
        conds, errs = collect_global(cfg, now)
        with get_sync_conn() as conn:
            out["global"] = _run(conn, "global", None, conds, errs)
    if user_id:
        conds, errs = collect_user(user_id, cfg, now)
        with get_sync_conn() as conn:
            out["user"] = _run(conn, f"user:{user_id}", user_id, conds, errs)
    return out
