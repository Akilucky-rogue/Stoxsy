"""
Scorecard persistence — the durable idea-log (Phase 0 increment 1).

`idea_to_row` is PURE (no DB): it serializes a TradeIdea into a trade_ideas row,
turning enums into their string values and dates into ISO strings inside the
JSONB columns — closing the serialization gap the Phase-0 self-review flagged.

`record_idea` / `record_ideas` persist at EMIT time (ON CONFLICT DO NOTHING, so
it is idempotent and mechanical). Logging every idea up-front is what makes the
Scorecard's anti-gaming guarantee real.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from typing import Iterable, Optional

from qsde.db.connection import execute_sql, read_sql
from qsde.engines.base import TradeIdea

log = logging.getLogger(__name__)

_INSERT_SQL = """
INSERT INTO trade_ideas (
    idea_id, scorecard_id, engine, symbol, structure, horizon, direction,
    entry, stop, target, qty, capital_required,
    net_credit, max_profit, margin_required, expiry, max_loss_rupees,
    confidence, expected_value, thesis_ref, adjustment_plan, valid_until,
    legs, greeks, breakevens, reasons, created_at, idea_date
) VALUES (
    %(idea_id)s, %(scorecard_id)s, %(engine)s, %(symbol)s, %(structure)s, %(horizon)s, %(direction)s,
    %(entry)s, %(stop)s, %(target)s, %(qty)s, %(capital_required)s,
    %(net_credit)s, %(max_profit)s, %(margin_required)s, %(expiry)s, %(max_loss_rupees)s,
    %(confidence)s, %(expected_value)s, %(thesis_ref)s, %(adjustment_plan)s, %(valid_until)s,
    %(legs)s::jsonb, %(greeks)s::jsonb, %(breakevens)s::jsonb, %(reasons)s::jsonb, %(created_at)s, %(idea_date)s
)
ON CONFLICT (engine, symbol, idea_date, structure) DO NOTHING
"""


def idea_to_row(idea: TradeIdea) -> dict:
    """Pure: TradeIdea -> a trade_ideas row dict (DB-ready). Enums -> values,
    nested legs/greeks/dates -> JSON (default=str handles date objects)."""
    return {
        "idea_id": idea.idea_id,
        "scorecard_id": idea.scorecard_id,
        "engine": idea.engine,
        "symbol": idea.symbol,
        "structure": idea.structure.value,           # enum -> "CASH_LONG" etc.
        "horizon": idea.horizon.value,
        "direction": int(idea.direction),
        "entry": idea.entry,
        "stop": idea.stop,
        "target": idea.target,
        "qty": idea.qty,
        "capital_required": idea.capital_required,
        "net_credit": idea.net_credit,
        "max_profit": idea.max_profit,
        "margin_required": idea.margin_required,
        "expiry": idea.expiry,                        # date object -> psycopg2 DATE
        "max_loss_rupees": idea.max_loss_rupees,
        "confidence": idea.confidence,
        "expected_value": idea.expected_value,
        "thesis_ref": idea.thesis_ref,
        "adjustment_plan": idea.adjustment_plan,
        "valid_until": idea.valid_until,
        "legs": json.dumps([asdict(leg) for leg in idea.legs], default=str),
        "greeks": json.dumps(asdict(idea.greeks)) if idea.greeks is not None else None,
        "breakevens": json.dumps(list(idea.breakevens)),
        "reasons": json.dumps(list(idea.reasons)),
        "created_at": idea.created_at,
        "idea_date": str(idea.created_at)[:10],   # YYYY-MM-DD for the per-day unique key
    }


def record_idea(idea: TradeIdea) -> bool:
    """Persist one idea at emit time. Idempotent (ON CONFLICT DO NOTHING)."""
    execute_sql(_INSERT_SQL, idea_to_row(idea))
    return True


def record_ideas(ideas: Iterable[TradeIdea]) -> int:
    """Persist a batch; returns the count submitted. One bad row is logged and skipped."""
    n = 0
    for idea in ideas:
        try:
            record_idea(idea)
            n += 1
        except Exception as e:  # noqa: BLE001
            log.warning("scorecard: failed to record idea %s/%s: %s",
                        idea.engine, idea.symbol, e)
    return n


def fetch_resolved(engine: Optional[str] = None) -> list[dict]:
    """Resolved ideas as Scorecard records {engine, horizon, confidence,
    realized_ret_net, status} — feed straight into scorecard.tracker.report()."""
    sql = ("SELECT engine, horizon, confidence, realized_ret_net, status "
           "FROM trade_ideas WHERE status IN ('WIN', 'LOSS', 'TIME', 'EXPIRED')")
    params: dict = {}
    if engine:
        sql += " AND engine = :e"
        params["e"] = engine
    df = read_sql(sql, params=params or None)
    return df.to_dict("records") if not df.empty else []


__all__ = ["idea_to_row", "record_idea", "record_ideas", "fetch_resolved"]
