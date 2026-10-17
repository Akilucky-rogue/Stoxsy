"""
Engine runner (Phase 0 increment 1) — scan engines, persist every emitted
TradeIdea to the Scorecard idea-log.

Runs BESIDE the legacy signal/writer path (strangler): it doesn't place orders,
doesn't touch the old tables — it just produces TradeIdeas and logs them. The
`recorder` is injectable so the loop is unit-tested without a DB.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable, Optional

from qsde.engines.base import Engine, TradeIdea
from qsde.scorecard.store import record_ideas

log = logging.getLogger(__name__)

Recorder = Callable[[Iterable[TradeIdea]], int]


def run_engine(engine: Engine, *, recorder: Optional[Recorder] = None) -> list[TradeIdea]:
    """Scan one engine and record its ideas. Returns the emitted ideas."""
    rec = recorder if recorder is not None else record_ideas
    ideas = engine.scan()
    if ideas:
        rec(ideas)
    log.info("engine %s: emitted %d ideas", engine.name, len(ideas))
    return ideas


def run_all(engines: Iterable[Engine], *,
            recorder: Optional[Recorder] = None) -> dict[str, list[TradeIdea]]:
    """Scan every engine; returns {engine_name: [ideas]}."""
    return {e.name: run_engine(e, recorder=recorder) for e in engines}


__all__ = ["run_engine", "run_all", "Recorder"]
