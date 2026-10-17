"""
Run the new engines and persist their TradeIdeas to the Scorecard idea-log
(Phase 0 increment 1). Runs PARALLEL to the legacy rule_signal_writer — additive,
places no orders.

Usage (from backend, venv active):
    python scripts/run_engines.py
    python scripts/run_engines.py --risk 2000 --budget 1000000 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.rule_momentum import RuleMomentumEngine
from qsde.engines.runner import run_engine


def main() -> None:
    ap = argparse.ArgumentParser(description="Run engines -> persist TradeIdeas")
    ap.add_argument("--risk", type=float, default=2000.0, help="Per-trade risk budget (Rs)")
    ap.add_argument("--budget", type=float, default=1_000_000.0, help="Per-position capital (Rs)")
    ap.add_argument("--lot-size", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true",
                    help="Scan + print but do NOT persist to the idea-log.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    engine = RuleMomentumEngine(risk_rupees=args.risk, budget=args.budget, lot_size=args.lot_size)
    recorder = (lambda ideas: 0) if args.dry_run else None   # None -> default DB recorder
    ideas = run_engine(engine, recorder=recorder)

    print(f"\n{engine.name}: {len(ideas)} ideas "
          f"({'DRY-RUN, not persisted' if args.dry_run else 'persisted to trade_ideas'})")
    for i in ideas[:25]:
        side = "BUY" if i.direction > 0 else "SELL" if i.direction < 0 else "NEUTRAL"
        print(f"  {i.symbol:<12} {side:<7} {i.structure.value:<14} "
              f"entry {i.entry} stop {i.stop} qty {i.qty} risk Rs{i.max_loss_rupees:.0f}")


if __name__ == "__main__":
    main()
