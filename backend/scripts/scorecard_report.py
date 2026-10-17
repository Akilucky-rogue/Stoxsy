"""
Scorecard report — the Reality Engine's read-out (F&O increment 5).

Answers the only question that matters: for each engine, what is the MEASURED
net-of-cost edge on the ideas it actually logged — expectancy, hit-rate, payoff,
and calibration (stated confidence vs realised hit-rate)? Nothing is hand-picked:
it reads the mechanically-resolved idea-log, so the numbers can't flatter
themselves. Accuracy is REPORTED here, never promised.

By default it first resolves matured ideas (cash barriers + expired condors) so
the report reflects everything knowable today.

Usage (from backend, venv active):
    python scripts/scorecard_report.py
    python scripts/scorecard_report.py --engine fno_income --calibration
    python scripts/scorecard_report.py --no-resolve        # report only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.scorecard.metrics import calibration_table
from qsde.scorecard.store import fetch_resolved
from qsde.scorecard.tracker import report


def _fmt_block(name: str, b: dict) -> str:
    n = b.get("n", 0)
    if not n:
        return f"  {name:<28} (no resolved ideas yet)"
    exp = b.get("expectancy", 0.0)
    hit = b.get("hit_rate", 0.0)
    payoff = b.get("payoff_ratio", float("nan"))
    brier = b.get("brier")
    edge = "EDGE" if exp > 0 else "no edge"
    line = (f"  {name:<28} n={n:<4} hit={hit:6.1%}  "
            f"expectancy={exp:+.4f} [{edge}]  payoff={payoff:.2f}  "
            f"W/L={b.get('n_win',0)}/{b.get('n_loss',0)}")
    if brier is not None and brier == brier:
        line += f"  brier={brier:.3f}"
    return line


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-engine MEASURED net edge from the idea-log")
    ap.add_argument("--engine", default=None, help="Restrict to one engine (substring of its id)")
    ap.add_argument("--no-resolve", action="store_true",
                    help="Skip the resolve step; report on whatever is already resolved.")
    ap.add_argument("--calibration", action="store_true",
                    help="Also print the confidence-vs-realised calibration table.")
    args = ap.parse_args()

    if not args.no_resolve:
        from qsde.scorecard.resolver import (
            resolve_open_ideas, resolve_option_ideas, resolve_pair_ideas,
        )
        cash = resolve_open_ideas()
        opt = resolve_option_ideas()
        pair = resolve_pair_ideas()
        print(f"resolved now — cash: {cash.get('closed',0)} closed, "
              f"condors: {opt.get('closed',0)} closed "
              f"({opt.get('no_settlement',0)} awaiting settlement), "
              f"pairs: {pair.get('closed',0)} closed\n")

    records = fetch_resolved(engine=args.engine)
    if not records:
        print("No resolved ideas yet — the Scorecard has nothing to measure.\n"
              "Run the engines + let their barrier/expiry windows elapse, then re-run.")
        return

    rep = report(records)
    print("=" * 78)
    print(f"SCORECARD — measured net-of-cost edge ({len(records)} resolved ideas)")
    print("=" * 78)
    print(_fmt_block("OVERALL", rep["overall"]))
    print("-" * 78)
    for engine, blk in sorted(rep["by_engine"].items()):
        print(_fmt_block(engine, blk))
    print("=" * 78)
    print("expectancy = mean per-trade net return (THE goal, not hit-rate). "
          "brier: 0=perfect, 0.25=coin-flip.")

    if args.calibration:
        confs = [r["confidence"] for r in records
                 if r.get("confidence") is not None and r.get("realized_ret_net") is not None]
        outs = [1 if float(r["realized_ret_net"]) > 0 else 0 for r in records
                if r.get("confidence") is not None and r.get("realized_ret_net") is not None]
        if confs:
            print("\nCalibration (stated confidence vs realised hit-rate):")
            for row in calibration_table(confs, outs):
                if row["n"]:
                    print(f"  conf [{row['bin_lo']:.1f},{row['bin_hi']:.1f}]  "
                          f"n={row['n']:<4} mean_conf={row['mean_conf']:.3f}  "
                          f"realised={row['realized']:.3f}")


if __name__ == "__main__":
    main()
