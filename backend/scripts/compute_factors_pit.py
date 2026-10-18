"""Compute factors (fixed-kernel ffd_v2) and persist only point-in-time universe symbol-days.

Usage (from backend/):
    python scripts/compute_factors_pit.py --pilot            # 40 liquid today + 20 liquid in 2018
    python scripts/compute_factors_pit.py --min-adv 10       # every symbol ever in universe_pit with adv>=10cr
    python scripts/compute_factors_pit.py --symbols RELIANCE,TCS

Writes to factor_pit (long form). Re-running is idempotent per (symbol,date,factor,valid_from).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
log = logging.getLogger("compute_factors_pit")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", default=None, help="comma list; overrides selection")
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--min-adv", type=float, default=10.0, help="universe filter: adv_cr >= this")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--chunk", type=int, default=25)
    ap.add_argument("--method", default="adv60_top500_v1")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import pandas as pd

    from qsde.db.connection import read_sql
    from qsde.factors.engine import compute_factors_batch

    uni = read_sql("SELECT date, symbol, rank, adv_cr FROM universe_pit "
                   "WHERE method = :m AND adv_cr >= :a", params={"m": args.method, "a": args.min_adv})
    if uni.empty:
        log.error("universe_pit is empty - run qsde.universe.pit.build_from_db first")
        return 1

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.pilot:
        last = uni[uni["date"] == uni["date"].max()].nsmallest(40, "rank")["symbol"]
        d18 = uni[uni["date"] == pd.Timestamp("2018-06-29").date()].nsmallest(20, "rank")["symbol"]
        symbols = sorted(set(last) | set(d18))
    else:
        symbols = sorted(uni["symbol"].unique())
    uni = uni[uni["symbol"].isin(symbols)]
    log.info("symbols=%d  universe symbol-days=%d", len(symbols), len(uni))

    t0, total = time.time(), 0
    for i in range(0, len(symbols), args.chunk):
        chunk = symbols[i:i + args.chunk]
        compute_factors_batch(chunk, start=args.start, universe=uni[uni["symbol"].isin(chunk)])
        total += len(chunk)
        log.info("done %d/%d symbols  (%.0fs elapsed)", total, len(symbols), time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
