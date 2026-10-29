"""In-app alerts: what needs attention (stale or failing data, corporate-action conflicts, model degradation, portfolio risk, Kite session).

Alerts are derived from current state by the rules in qsde/alerts and refreshed on demand; an alert for a condition that keeps holding is a single alert, and one whose
condition has cleared resolves itself. Acknowledging an alert hides nothing and changes nothing else. Nothing here can place, change or cancel an order, and nothing is sent
outside this application.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.auth_deps import Principal, require_auth, require_role
from qsde.alerts import engine, store
from qsde.db.connection import get_sync_conn, read_sql

router = APIRouter()
MIN_REFRESH_SECONDS = 60


class AlertRow(BaseModel):
    id: int
    scope: str
    rule_id: str
    severity: str
    state: str
    title: str
    detail: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    first_seen: str
    last_seen: str
    occurrences: int
    acked_by: Optional[str] = None
    acked_at: Optional[str] = None
    resolved_at: Optional[str] = None
    resolved_why: Optional[str] = None
    payload: dict = {}


class AlertList(BaseModel):
    n_matching: int
    offset: int
    rows: list[AlertRow]
    last_run: Optional[str] = None


class AlertSummary(BaseModel):
    open: dict
    acknowledged: int
    last_run: Optional[str] = None
    stale: bool = True


def _iso(x) -> Optional[str]:
    return None if x is None or pd.isna(x) else pd.Timestamp(x).isoformat()


def _last_run(user_id: str) -> Optional[pd.Timestamp]:
    d = read_sql("SELECT max(finished_at) AS t FROM alert_runs WHERE scope = 'global' AND finished_at IS NOT NULL")
    t = d["t"].iloc[0]
    return None if pd.isna(t) else pd.Timestamp(t)


@router.get("/alerts", response_model=AlertList)
def list_alerts(state: str = Query("live", pattern="^(live|open|acknowledged|resolved|all)$"), severity: Optional[str] = Query(None, pattern="^(info|warning|critical)$"),
                limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0, le=100000), p: Principal = Depends(require_auth)):
    n, d = store.list_alerts(read_sql, p.id, state, severity, limit, offset)
    rows = [AlertRow(id=int(r.id), scope="you" if r.user_id else "system", rule_id=r.rule_id, severity=r.severity, state=r.state, title=r.title, detail=r.detail, entity_type=r.entity_type,
                     entity_id=r.entity_id, first_seen=_iso(r.first_seen), last_seen=_iso(r.last_seen), occurrences=int(r.occurrences), acked_by=r.acked_by, acked_at=_iso(r.acked_at),
                     resolved_at=_iso(r.resolved_at), resolved_why=r.resolved_why, payload=r.payload or {}) for r in d.itertuples()]
    return AlertList(n_matching=n, offset=offset, rows=rows, last_run=_iso(_last_run(p.id)))


@router.get("/alerts/summary", response_model=AlertSummary)
def alert_summary(p: Principal = Depends(require_auth)):
    d = read_sql("SELECT severity, state, count(*) AS n FROM alert_events WHERE state <> 'resolved' AND (user_id IS NULL OR user_id = :u) GROUP BY 1, 2", params={"u": p.id})
    o = {"critical": 0, "warning": 0, "info": 0}
    ack = 0
    for r in d.itertuples():
        if r.state == "open":
            o[r.severity] = int(r.n)
        else:
            ack += int(r.n)
    t = _last_run(p.id)
    return AlertSummary(open=o, acknowledged=ack, last_run=_iso(t), stale=t is None or (pd.Timestamp.now(tz="UTC") - t.tz_convert("UTC")).total_seconds() > 15 * 60)


@router.post("/alerts/refresh")
def refresh(p: Principal = Depends(require_auth)):
    """Re-evaluate now. The system-wide rules are skipped if they ran less than a minute ago; the caller's own portfolio rules always run."""
    t = _last_run(p.id)
    recent = t is not None and (pd.Timestamp.now(tz="UTC") - t.tz_convert("UTC")).total_seconds() < MIN_REFRESH_SECONDS
    res = engine.evaluate(user_id=p.id, include_global=not recent)
    return {"evaluated": sorted(res), "skipped_global": recent, "errors": [e for v in res.values() for e in v["errors"]]}


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge(alert_id: int, p: Principal = Depends(require_role("admin", "trader"))):
    with get_sync_conn() as conn:
        ok = store.acknowledge(conn, alert_id, p.email or p.id, p.id)
    if not ok:
        raise HTTPException(status_code=404, detail="no open alert with that id that you can acknowledge")
    return {"acknowledged": alert_id}
