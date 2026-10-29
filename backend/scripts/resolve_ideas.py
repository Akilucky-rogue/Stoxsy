"""
Resolve open TradeIdeas to their realized barrier outcomes, then print the live
Scorecard per engine (Phase 0 increment 2). EOD-driven; additive (only fills
outcome columns on already-logged ideas).

Usage (from backend, venv active):
    python scripts/resolve_ideas.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.scorecard.resolver import resolve_open_ideas
from qsde.scorecard.store import fetch_resolved
from qsde.scorecard.tracker import report


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    counts = resolve_open_ideas()
    print(f"\nResolve: {counts}")

    rep = report(fetch_resolved())
    print(f"\nScorecard (overall): {rep['overall']}")
    by_engine = rep.get("by_engine", {})
    if not by_engine:
        print("  (no resolved ideas yet — run scripts/run_engines.py, wait for the "
              "barrier window, then resolve again)")
    for engine, block in sorted(by_engine.items()):
        print(f"  {engine:<28} {block}")


if __name__ == "__main__":
    main()
