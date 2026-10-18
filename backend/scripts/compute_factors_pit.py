"""Build ONE full Parquet feature snapshot (ffd_v2) for the point-in-time universe.

Usage (from backend/):
    python scripts/compute_factors_pit.py --pilot            # 40 liquid today + 20 liquid in 2018 (plumbing only)
    python scripts/compute_factors_pit.py --min-adv 10       # every symbol ever in universe_pit with adv>=10cr
    python scripts/compute_factors_pit.py --symbols RELIANCE,TCS

Writes an immutable snapshot under STOXSY_FEATURE_DIR and registers it in feature_snapshots
(a 'full' snapshot supersedes earlier ones of the same factor_version + universe_version).
Only point-in-time universe symbol-days are persisted; the full history is used to compute them.
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
    ap.add_argument("--chunk", type=int, default=50)
    ap.add_argument("--method", default="adv60_top500_v1")
    ap.add_argument("--tag", default=None, help="universe_version label (default derived)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import pandas as pd

    from qsde.db.connection import read_sql
    from qsde.factors.engine import compute_ffd_panel
    from qsde.factors.versions import FFD_VERSION
    from qsde.features.store import SnapshotWriter

    uni = read_sql("SELECT date, symbol, rank, adv_cr FROM universe_pit "
                   "WHERE method = :m AND adv_cr >= :a", params={"m": args.method, "a": args.min_adv})
    if uni.empty:
        log.error("universe_pit is empty - run qsde.universe.pit.build_from_db first")
        return 1

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        label = "custom"
    elif args.pilot:
        last = uni[uni["date"] == uni["date"].max()].nsmallest(40, "rank")["symbol"]
        d18 = uni[uni["date"] == pd.Timestamp("2018-06-29").date()].nsmallest(20, "rank")["symbol"]
        symbols = sorted(set(last) | set(d18))
        label = "PILOT-SURVIVOR-SELECTED"
    else:
        symbols = sorted(uni["symbol"].unique())
        label = "full"
    uni = uni[uni["symbol"].isin(symbols)]
    universe_version = args.tag or f"{args.method}+adv{args.min_adv:g}+{label}"
    log.info("symbols=%d  universe symbol-days=%d  universe_version=%s", len(symbols), len(uni), universe_version)

    writer = SnapshotWriter(FFD_VERSION, universe_version, kind="full",
                            params={"start": args.start, "universe_method": args.method,
                                    "min_adv_cr": args.min_adv, "selection": label,
                                    "n_symbols": len(symbols)})
    t0, done = time.time(), 0
    for i in range(0, len(symbols), args.chunk):
        chunk = symbols[i:i + args.chunk]
        _, to_write, _ = compute_ffd_panel(chunk, start=args.start,
                                           universe=uni[uni["symbol"].isin(chunk)])
        if len(to_write):
            writer.add(to_write)
        done += len(chunk)
        log.info("computed %d/%d symbols  (%.0fs)", done, len(symbols), time.time() - t0)
    snap = writer.close()
    log.info("snapshot %s  rows=%d  symbols=%d  features=%d  hash=%s",
             snap.snapshot_id, snap.n_rows, snap.n_symbols, len(snap.columns), snap.dataset_hash[:16])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
