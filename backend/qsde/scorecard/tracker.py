"""
Scorecard orchestration (skeleton) — ARCHITECTURE.md §2.

Ties emitted ideas -> realized outcomes -> honest per-engine stats. The
persistence layer (a scorecard_ideas table) and the live idea->outcome matcher
land in a later Phase-0 step; for now this aggregates already-resolved records
(e.g. from qsde.execution.paper_journal) through the pure metrics.

The honesty rule (baked in by design): log EVERY idea at emit time with a FIXED
outcome window, and match outcomes mechanically — NEVER select which trades to
count after the fact. That is what stops the Scorecard from flattering itself.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from qsde.scorecard.metrics import summarize


def report(records: Iterable[dict]) -> dict:
    """Aggregate resolved idea records into a per-engine + overall scorecard.

    Each record: {engine, horizon?, realized_ret_net, confidence?, hit?}.
    `hit` defaults to (realized_ret_net > 0) when absent.
    """
    by_engine: dict[str, list[dict]] = defaultdict(list)
    allrecs: list[dict] = []
    for r in records:
        by_engine[r.get("engine", "unknown")].append(r)
        allrecs.append(r)

    def _block(rs: list[dict]) -> dict:
        rets = [float(x["realized_ret_net"]) for x in rs
                if x.get("realized_ret_net") is not None]
        confs = [x["confidence"] for x in rs if x.get("confidence") is not None]
        outs = [int(x.get("hit", 1 if float(x.get("realized_ret_net", 0)) > 0 else 0))
                for x in rs if x.get("confidence") is not None]
        if confs and len(confs) == len(outs):
            return summarize(rets, confs, outs)
        return summarize(rets)

    return {"overall": _block(allrecs),
            "by_engine": {e: _block(rs) for e, rs in by_engine.items()}}


__all__ = ["report"]
