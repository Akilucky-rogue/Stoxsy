"""
Run the PairsStatArbEngine over the liquid universe: find stretched cointegrated
sector pairs, emit defined-risk PAIR ideas to the idea-log, then resolve any whose
spread has already hit its exit/stop/time barrier. Additive — places no orders.

Usage (from backend, venv active):
    python scripts/run_pairs.py
    python scripts/run_pairs.py --entry-z 2.5 --risk 3000 --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.pairs import PairsStatArbEngine
from qsde.engines.runner import run_engine
from qsde.scorecard.resolver import resolve_pair_ideas


def main() -> None:
    ap = argparse.ArgumentParser(description="Run pairs StatArb -> idea-log + resolve")
    ap.add_argument("--lookback", type=int, default=250)
    ap.add_argument("--entry-z", type=float, default=2.0)
    ap.add_argument("--stop-z", type=float, default=3.5)
    ap.add_argument("--max-pvalue", type=float, default=0.05)
    ap.add_argument("--risk", type=float, default=2000.0, help="Per-trade risk (Rs)")
    ap.add_argument("--budget", type=float, default=1_000_000.0)
    ap.add_argument("--max-loss-cap", type=float, default=float("inf"))
    ap.add_argument("--dry-run", action="store_true", help="Scan + print but do NOT log.")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine = PairsStatArbEngine(
        lookback=args.lookback, entry_z=args.entry_z, stop_z=args.stop_z,
        max_pvalue=args.max_pvalue, risk_rupees=args.risk, budget=args.budget,
        max_loss_cap=args.max_loss_cap,
    )
    recorder = (lambda ideas: 0) if args.dry_run else None
    ideas = run_engine(engine, recorder=recorder)

    print(f"\npairs_statarb: {len(ideas)} pair idea(s) "
          f"({'DRY-RUN, not logged' if args.dry_run else 'logged to trade_ideas'})")
    for i in ideas:
        print(f"  {i.symbol:<20} maxloss Rs{i.max_loss_rupees:.0f} conf {i.confidence}")
        for r in i.reasons:
            print(f"      {r}")

    if not args.dry_run:
        print("scorecard pair-resolve:", resolve_pair_ideas())


if __name__ == "__main__":
    main()
