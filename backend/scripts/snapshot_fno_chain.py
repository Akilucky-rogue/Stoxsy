"""
Snapshot the F&O options chain, run the FnoIncomeEngine, log its ideas, and grow
the ATM-IV history (F&O increment 4).

This is the data-plumbing run the FnoIncomeEngine needs to operate on real data:

    NSE F&O bhavcopy  ->  parse + invert IV  ->  chain snapshots
                       ->  record ATM IV (fno_iv_history; iv_rank's window)
                       ->  FnoIncomeEngine.scan()  ->  defined-risk condors
                       ->  trade_ideas idea-log (idempotent per day) -> Scorecard

Additive + defined-risk: it places NO orders. iv_rank is cold (NaN/0.5) until a
few sessions of history accumulate — that is expected and honest; the engine
simply emits nothing until vol is measurably rich.

Usage (from backend, venv active):
    python scripts/snapshot_fno_chain.py                       # fetch latest bhavcopy
    python scripts/snapshot_fno_chain.py --date 2026-06-25
    python scripts/snapshot_fno_chain.py --csv saved_fo.csv    # offline / blocked NSE
    python scripts/snapshot_fno_chain.py --indices NIFTY BANKNIFTY --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.fno_income import FnoIncomeEngine
from qsde.engines.runner import run_engine
from qsde.fno.chain_source import BhavcopyChainSource
from qsde.fno.indices import DEFAULT_INDICES
from qsde.scorecard.resolver import resolve_option_ideas


def _last_trading_day() -> date:
    """Most recent weekday (NSE doesn't publish a weekend bhavcopy)."""
    d = datetime.now().date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def main() -> None:
    ap = argparse.ArgumentParser(description="Snapshot F&O chain -> engine -> idea-log")
    ap.add_argument("--date", type=str, default=None, help="Session date YYYY-MM-DD (default: last weekday)")
    ap.add_argument("--csv", type=str, default=None, help="Path to a saved F&O bhavcopy CSV (skip NSE fetch)")
    ap.add_argument("--indices", nargs="+", default=list(DEFAULT_INDICES))
    ap.add_argument("--iv-rank-min", type=float, default=0.5, help="Min iv_rank to sell premium")
    ap.add_argument("--min-iv-history", type=int, default=20,
                    help="Min trailing ATM-IV sessions before the engine trusts iv_rank "
                         "(warm-up; emits nothing until this many sessions accrue)")
    ap.add_argument("--max-loss-cap", type=float, default=float("inf"), help="Per-trade max loss (Rs)")
    ap.add_argument("--min-dte", type=int, default=2)
    ap.add_argument("--max-dte", type=int, default=60)
    ap.add_argument("--no-persist", action="store_true", help="Do NOT write ATM IV to fno_iv_history")
    ap.add_argument("--dry-run", action="store_true", help="Scan + print but do NOT log ideas")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asof = date.fromisoformat(args.date) if args.date else _last_trading_day()
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"--csv file not found: {csv_path}\n"
                  f"Pass a real saved F&O bhavcopy CSV, or omit --csv to fetch from NSE.")
            sys.exit(1)
        csv_text = csv_path.read_text(encoding="utf-8")
    else:
        csv_text = None

    source = BhavcopyChainSource(
        asof, indices=args.indices, csv_text=csv_text,
        min_dte=args.min_dte, max_dte=args.max_dte, persist=not args.no_persist,
    )
    engine = FnoIncomeEngine(
        lot_size=75, strike_step=50.0,          # per-index values ride on the snapshot
        iv_rank_min=args.iv_rank_min, min_iv_history=args.min_iv_history,
        max_loss_cap=args.max_loss_cap, source=source,
    )

    recorder = (lambda ideas: 0) if args.dry_run else None
    ideas = run_engine(engine, recorder=recorder)

    print(f"\nfno_income @ {asof}: {len(ideas)} defined-risk idea(s) "
          f"({'DRY-RUN, not logged' if args.dry_run else 'logged to trade_ideas'})")
    for i in ideas:
        print(f"  {i.symbol:<10} {i.structure.value:<12} exp {i.expiry} "
              f"credit Rs{(i.net_credit or 0):.0f} maxloss Rs{i.max_loss_rupees:.0f} "
              f"conf {i.confidence} BE {i.breakevens}")

    if not args.dry_run:
        # Condors are option structures -> the option resolver settles expired ones.
        print("scorecard resolve (expired condors):", resolve_option_ideas())


if __name__ == "__main__":
    main()
