"""Trader mode: the desk, the risk gate, research and paper ideas, the journal. RESEARCH and PAPER only; no order can be placed from here.

The gate (`qsde.trader.risk.evaluate`) runs on EVERY path that stores an idea, against the signed-in user's real state read from the database, so a limit cannot be skipped by posting a card.
Trading capital comes only from the user's profile (the trading pot); nothing is assumed when it is not set. Reads need a signed-in user; writes need admin or trader.
"""
from __future__ import annotations

from typing import Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.auth_deps import Principal, require_auth, require_role
from api.routes import cockpit
from qsde.db.connection import get_sync_conn, read_sql
from qsde.modes import policy as pol, profile as prof
from qsde.trader import risk as R, setups, store

router = APIRouter()
HORIZONS = ("swing_5d", "position_20d")
DISCLAIMER = ("Research and decision support, not investment advice. Every idea here is RESEARCH or PAPER: no engine is approved and no edge has been shown. No order is placed by this product.")


class IdeaIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)
    horizon_id: Literal["swing_5d", "position_20d"] = "swing_5d"
    setup: str = Field(default="manual", max_length=60)
    entry: Optional[float] = Field(default=None, gt=0)
    stop: Optional[float] = Field(default=None, gt=0)
    target: Optional[float] = Field(default=None, gt=0)
    evidence: Literal["RESEARCH", "PAPER", "APPROVED"] = "RESEARCH"


class CloseIn(BaseModel):
    price: float = Field(gt=0)
    reason: Literal["stop", "target", "time", "manual"]
    notes: Optional[str] = Field(default=None, max_length=500)


def _profile(p: Principal) -> dict:
    with get_sync_conn() as c:
        return prof.load(c, p.id)


def _signal() -> Optional[pd.DataFrame]:
    return pd.read_parquet(cockpit.SIGNAL) if cockpit.SIGNAL.exists() else None


@router.get("/trader/desk")
def desk(limit: int = 8, p: Principal = Depends(require_auth)):
    pr = _profile(p)
    l = R.Limits.from_effective(pol.effective_limits(pr.get("limits")))
    cap = float(pr.get("trading_capital") or 0.0)
    now = pd.Timestamp.now(tz="UTC")
    st, _ = store.build_state(read_sql, p.id, cap, now)
    out = {"disclaimer": DISCLAIMER, "engine_approved": pol.engine_approved(), "trading_capital": cap or None, "meters": R.meters(st, l), "limits": l.__dict__,
           "open": [{"symbol": o.symbol, "qty": o.qty, "entry": o.entry, "stop": o.stop, "last": o.last_price, "risk_rupees": o.risk_rupees, "unrealised": o.qty * (o.last_price - o.entry)} for o in st.open_positions],
           "mode": pr.get("mode"), "candidates": [], "needs": []}
    if not cap:
        out["needs"].append("trading_capital")
        out["headline"] = "Set your trading pot on the Profile page to see sized ideas: nothing is assumed."
        return out
    c = setups.candidates(read_sql, _signal(), limit=max(1, min(limit, 20)))
    for r in c.itertuples():
        d = store.propose(read_sql, p.id, pr, symbol=r.symbol, horizon_id="swing_5d", setup=setups.SETUP, now=now, state=st)
        out["candidates"].append({"symbol": r.symbol, "breakout_pct": r.breakout_pct, "volume_x_median": r.volume_x_median, "risk_bucket": None if "risk_bucket" not in c.columns or pd.isna(r.risk_bucket) else int(r.risk_bucket),
                                  "allowed": d["allowed"], "card": d["card"], "reasons": d["reasons"], "notes": d["notes"]})
    n_ok = sum(1 for x in out["candidates"] if x["allowed"])
    out["headline"] = (f"{n_ok} research candidate(s) pass your risk rules today (of {len(out['candidates'])} breakouts checked). They are baselines with no shown edge."
                       if out["candidates"] else "No breakout candidates in the universe today: nothing to do is a normal answer.")
    return out


@router.post("/trader/propose")
def propose(b: IdeaIn, p: Principal = Depends(require_auth)):
    return store.propose(read_sql, p.id, _profile(p), symbol=b.symbol.upper(), horizon_id=b.horizon_id, setup=b.setup, entry=b.entry, stop=b.stop, target=b.target, evidence=b.evidence)


@router.post("/trader/ideas")
def create_idea(b: IdeaIn, p: Principal = Depends(require_role("admin", "trader"))):
    d = store.propose(read_sql, p.id, _profile(p), symbol=b.symbol.upper(), horizon_id=b.horizon_id, setup=b.setup, entry=b.entry, stop=b.stop, target=b.target, evidence=b.evidence)
    with get_sync_conn() as c:                                   # commits on exit: the refusal record must survive the error raised below
        if d["allowed"]:
            iid = store.persist(c, p.id, d, p.role)
        else:
            prof.record(c, p.id, p.role, "trader.refused", {"symbol": b.symbol.upper(), "reasons": d["reasons"]})
    if not d["allowed"]:
        raise HTTPException(status_code=422, detail=d["reasons"])
    return {"id": iid, "card": d["card"], "notes": d["notes"]}


@router.post("/trader/ideas/{idea_id}/close")
def close_idea(idea_id: int, b: CloseIn, p: Principal = Depends(require_role("admin", "trader"))):
    try:
        with get_sync_conn() as c:
            return store.close(c, p.id, p.role, idea_id, b.price, b.reason, b.notes)
    except LookupError:
        raise HTTPException(status_code=404, detail="open idea not found")


@router.get("/trader/ideas")
def list_ideas(status: Literal["open", "closed", "all"] = "all", limit: int = 100, p: Principal = Depends(require_auth)):
    q = "SELECT id, created_at, symbol, setup, horizon_id, evidence, entry, stop, target, qty, risk_rupees, time_stop, status, opened_at, closed_at, exit_price, exit_reason, pnl_rupees, r_multiple, notes FROM trader_ideas WHERE user_id = :u"
    if status != "all":
        q += " AND status = :s"
    d = read_sql(q + " ORDER BY opened_at DESC LIMIT :n", params={"u": p.id, "s": status, "n": max(1, min(limit, 500))})
    return [{k: (None if pd.isna(v) else (v.isoformat() if hasattr(v, "isoformat") else (float(v) if k in ("entry", "stop", "target", "risk_rupees", "exit_price", "pnl_rupees", "r_multiple") else v))) for k, v in r.items()} for r in d.to_dict("records")]


@router.get("/trader/performance")
def performance(p: Principal = Depends(require_auth)):
    return {**store.performance(read_sql, p.id), "disclaimer": DISCLAIMER}
