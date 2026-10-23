"""Investor mode: goals, manual balances and the Investor Plan.

Everything here is the INVESTMENT pot only and belongs to the signed-in user. The plan is a scenario and a risk framing from resampled history (never a forecast), always states its sample, proxies and seed,
and never raises equity to rescue a goal. Writes need admin or trader; viewers read.
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth_deps import Principal, require_auth, require_role
from qsde.db.connection import get_sync_conn, read_sql
from qsde.investor import assets as A
from qsde.investor import history as H
from qsde.investor import service as S
from qsde.modes import profile as prof

router = APIRouter()
_lock = threading.Lock()
_hist: dict = {}


class GoalIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["goal", "emergency"] = "goal"
    target_amount: float = Field(gt=0, le=1e12)
    target_date: date
    current_amount: float = Field(default=0, ge=0, le=1e12)
    monthly_sip: float = Field(default=0, ge=0, le=1e9)
    priority: int = Field(default=1, ge=1, le=99)
    is_test: bool = False


class GoalPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_amount: Optional[float] = Field(default=None, gt=0, le=1e12)
    target_date: Optional[date] = None
    current_amount: Optional[float] = Field(default=None, ge=0, le=1e12)
    monthly_sip: Optional[float] = Field(default=None, ge=0, le=1e9)
    priority: Optional[int] = Field(default=None, ge=1, le=99)


class BalanceIn(BaseModel):
    asset_class: Literal["equity", "gold", "debt"]
    label: str = Field(min_length=1, max_length=120)
    amount: float = Field(ge=0, le=1e12)
    as_of: date


def _history() -> H.History:
    key = str(H.last_complete_month().date())
    with _lock:
        if _hist.get("key") != key:
            _hist.update({"key": key, "h": H.load()})
        return _hist["h"]


def _goal_row(r: dict) -> dict:
    return {"id": int(r["id"]), "name": r["name"], "kind": r["kind"], "target_amount": float(r["target_amount"]), "target_date": r["target_date"], "current_amount": float(r["current_amount"]),
            "monthly_sip": float(r["monthly_sip"]), "priority": int(r["priority"]), "is_test": bool(r["is_test"])}


def _goals(c, user_id: str) -> list:
    cur = c.cursor()
    cur.execute("SELECT * FROM goals WHERE user_id = %s AND NOT archived ORDER BY priority, id", (user_id,))
    return [_goal_row(r) for r in cur.fetchall()]


def _balances(c, user_id: str) -> list:
    cur = c.cursor()
    cur.execute("SELECT id, asset_class, label, amount, as_of FROM investor_balances WHERE user_id = %s AND NOT archived ORDER BY id", (user_id,))
    return [{"id": int(r["id"]), "asset_class": r["asset_class"], "label": r["label"], "amount": float(r["amount"]), "as_of": r["as_of"]} for r in cur.fetchall()]


@router.get("/investor/goals")
def list_goals(p: Principal = Depends(require_auth)):
    with get_sync_conn() as c:
        return [{**g, "target_date": str(g["target_date"])} for g in _goals(c, p.id)]


@router.post("/investor/goals")
def add_goal(b: GoalIn, p: Principal = Depends(require_role("admin", "trader"))):
    if b.target_date <= date.today():
        raise HTTPException(status_code=422, detail="the target date must be in the future")
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO goals (user_id, name, kind, target_amount, target_date, current_amount, monthly_sip, priority, is_test) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (p.id, b.name, b.kind, b.target_amount, b.target_date, b.current_amount, b.monthly_sip, b.priority, b.is_test))
        gid = int(cur.fetchone()["id"])
        prof.record(c, p.id, p.role, "goal.create", {"id": gid, **b.model_dump(mode="json")})
    return {"id": gid}


@router.patch("/investor/goals/{goal_id}")
def edit_goal(goal_id: int, b: GoalPatch, p: Principal = Depends(require_role("admin", "trader"))):
    ch = {k: v for k, v in b.model_dump().items() if v is not None}
    if not ch:
        raise HTTPException(status_code=422, detail="nothing to change")
    if "target_date" in ch and ch["target_date"] <= date.today():
        raise HTTPException(status_code=422, detail="the target date must be in the future")
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT * FROM goals WHERE id = %s AND user_id = %s AND NOT archived FOR UPDATE", (goal_id, p.id))
        old = cur.fetchone()
        if old is None:
            raise HTTPException(status_code=404, detail="goal not found")
        cur.execute(f"UPDATE goals SET {', '.join(f'{k} = %s' for k in ch)}, updated_at = NOW() WHERE id = %s", [*ch.values(), goal_id])
        prof.record(c, p.id, p.role, "goal.update", {"id": goal_id, "before": {k: str(old[k]) for k in ch}, "after": {k: str(v) for k, v in ch.items()}})
    return {"id": goal_id}


@router.delete("/investor/goals/{goal_id}")
def archive_goal(goal_id: int, p: Principal = Depends(require_role("admin", "trader"))):
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE goals SET archived = TRUE, updated_at = NOW() WHERE id = %s AND user_id = %s AND NOT archived RETURNING id", (goal_id, p.id))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="goal not found")
        prof.record(c, p.id, p.role, "goal.archive", {"id": goal_id})
    return {"archived": goal_id}


@router.get("/investor/balances")
def list_balances(p: Principal = Depends(require_auth)):
    with get_sync_conn() as c:
        return [{**b, "as_of": str(b["as_of"])} for b in _balances(c, p.id)]


@router.post("/investor/balances")
def add_balance(b: BalanceIn, p: Principal = Depends(require_role("admin", "trader"))):
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO investor_balances (user_id, asset_class, label, amount, as_of) VALUES (%s,%s,%s,%s,%s) RETURNING id", (p.id, b.asset_class, b.label, b.amount, b.as_of))
        bid = int(cur.fetchone()["id"])
        prof.record(c, p.id, p.role, "balance.create", {"id": bid, **b.model_dump(mode="json")})
    return {"id": bid}


@router.delete("/investor/balances/{balance_id}")
def archive_balance(balance_id: int, p: Principal = Depends(require_role("admin", "trader"))):
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("UPDATE investor_balances SET archived = TRUE WHERE id = %s AND user_id = %s AND NOT archived RETURNING id", (balance_id, p.id))
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail="balance not found")
        prof.record(c, p.id, p.role, "balance.archive", {"id": balance_id})
    return {"archived": balance_id}


def _priced_holdings(user_id: str) -> tuple[list, Optional[str]]:
    """Latest INVESTMENT-pot import, valued at the last close. Trading-pot and unassigned imports are never read here."""
    from qsde.ml import data as mldata
    from qsde.portfolio import analytics

    imp = read_sql("SELECT id, imported_at FROM portfolio_imports WHERE user_id = :u AND pot = 'investment' ORDER BY imported_at DESC LIMIT 1", params={"u": user_id})
    if imp.empty:
        return [], None
    h = read_sql("SELECT symbol, quantity::float8 AS quantity, avg_cost::float8 AS avg_cost FROM portfolio_holdings WHERE import_id = :i", params={"i": int(imp["id"].iloc[0])})
    start = str((pd.Timestamp.today() - pd.Timedelta(days=40)).date())
    px = mldata.load_prices(h["symbol"].tolist(), start=start)[["symbol", "date", "close"]]
    if px.empty:
        return [{"symbol": s, "value": None} for s in h["symbol"]], None
    last = px.sort_values("date").groupby("symbol").tail(1)
    df = analytics.value_holdings(h, last, px["date"].max())
    return [{"symbol": r.symbol, "value": None if pd.isna(r.value) else float(r.value)} for r in df.itertuples()], str(pd.Timestamp(px["date"].max()).date())


@router.get("/investor/plan")
def investor_plan(p: Principal = Depends(require_auth)):
    with get_sync_conn() as c:
        profile = prof.load(c, p.id)
        goals = _goals(c, p.id)
        balances = _balances(c, p.id)
    holdings, priced_at = _priced_holdings(p.id)
    try:
        hist = _history()
    except ValueError as e:
        raise HTTPException(status_code=503, detail=f"history unavailable: {e}")
    plan = S.compose(profile, goals, balances, holdings, hist, A.load_instruments(), date.today())
    plan["holdings_priced_at"] = priced_at
    plan["balances"] = [{**b, "as_of": str(b["as_of"])} for b in balances]
    return plan
