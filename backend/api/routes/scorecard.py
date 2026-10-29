"""
Scorecard API — the Reality Engine over HTTP (ARCHITECTURE.md §2).

Surfaces the SAME mechanically-resolved idea-log the CLI reads, so the dashboard
can show each engine's MEASURED net-of-cost edge — expectancy, hit-rate, payoff,
calibration — alongside the live signals. Accuracy is reported here, never promised.

  GET  /api/scorecard            -> per-engine measured report (side-effect free)
  GET  /api/scorecard/ideas      -> the idea-log (open + resolved), newest first
  POST /api/scorecard/resolve    -> run the mechanical resolver (cash + condors)

GET is read-only by design; resolution (which writes outcomes) is an explicit POST.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query

from qsde.db.connection import read_sql
from qsde.scorecard.metrics import calibration_table
from qsde.scorecard.store import fetch_resolved
from qsde.scorecard.tracker import report

log = logging.getLogger(__name__)
router = APIRouter()


@router.get("/scorecard")
def scorecard(
    engine: Optional[str] = Query(default=None, description="Restrict to one engine id."),
    calibration: bool = Query(default=False,
        description="Include the confidence-vs-realised calibration table."),
):
    """Per-engine MEASURED net edge from the resolved idea-log. Read-only — call
    POST /scorecard/resolve first to settle matured ideas."""
    records = fetch_resolved(engine=engine)
    records = [r for r in records if r.get("realized_ret_net") is not None]
    if not records:
        return {"n_resolved": 0, "overall": None, "by_engine": {},
                "message": "No resolved ideas yet — run /scorecard/resolve once "
                           "barrier/expiry windows have elapsed."}
    rep = report(records)
    out = {"n_resolved": len(records), "overall": rep["overall"],
           "by_engine": rep["by_engine"]}
    if calibration:
        confs = [r["confidence"] for r in records if r.get("confidence") is not None]
        outs = [1 if float(r["realized_ret_net"]) > 0 else 0
                for r in records if r.get("confidence") is not None]
        out["calibration"] = [b for b in calibration_table(confs, outs) if b["n"]] \
            if confs else []
    return out


@router.get("/scorecard/ideas")
def scorecard_ideas(
    engine: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None, description="OPEN | WIN | LOSS | TIME | EXPIRED"),
    limit: int = Query(default=100, ge=1, le=500),
):
    """The idea-log itself (every emitted idea, open + resolved), newest first —
    the audit trail that makes the measured stats trustworthy."""
    clauses: list[str] = []
    params: dict[str, object] = {"lim": limit}
    if engine:
        clauses.append("engine = :e")
        params["e"] = engine
    if status:
        clauses.append("status = :st")
        params["st"] = status.upper()
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    df = read_sql(
        f"""SELECT idea_id, engine, symbol, structure, horizon, direction,
                   entry, stop, target, max_loss_rupees, confidence, expiry,
                   status, exit_price, exit_date, realized_ret, realized_ret_net,
                   created_at, resolved_at
              FROM trade_ideas {where}
          ORDER BY created_at DESC LIMIT :lim""",
        params=params,
    )
    return {"ideas": df.to_dict("records"), "count": len(df)}


@router.post("/scorecard/resolve")
def scorecard_resolve():
    """Run the mechanical resolver — settle every matured cash idea (barriers) and
    expired condor (expiry settlement). Idempotent: only OPEN, matured ideas change."""
    from qsde.scorecard.resolver import (
        resolve_open_ideas, resolve_option_ideas, resolve_pair_ideas,
    )
    cash = resolve_open_ideas()
    condors = resolve_option_ideas()
    pairs = resolve_pair_ideas()
    return {"cash": cash, "condors": condors, "pairs": pairs}
