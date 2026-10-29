"""
Backfill universe.sector / universe.industry from yfinance (GICS-style sectors).

The PairsStatArbEngine only pairs SAME-sector names (its anti-data-mining guard),
so a sparse `sector` column starves it. This fills sector+industry for every active
symbol so the engine gets a real candidate universe. Sectors change rarely — run
this on demand / weekly, not daily.

Idempotent: UPDATEs existing universe rows (never inserts). yfinance is best-effort
per symbol; a failed/blank fetch leaves that row's existing sector untouched.

Usage (from backend, venv active):
    python scripts/backfill_sectors.py
    python scripts/backfill_sectors.py --dry-run        # preview, no writes
    python scripts/backfill_sectors.py --only RELIANCE,TCS
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import execute_sql, read_sql

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sectors")


def fetch_sector(symbol: str) -> tuple[str | None, str | None]:
    """(sector, industry) for an NSE symbol via yfinance. (None, None) on failure."""
    import yfinance as yf
    try:
        info = yf.Ticker(f"{symbol}.NS").info
        sector = (info.get("sector") or "").strip() or None
        industry = (info.get("industry") or "").strip() or None
        return sector, industry
    except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the run
        log.warning("yfinance failed for %s: %s", symbol, str(e)[:80])
        return None, None


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill universe.sector/industry from yfinance")
    ap.add_argument("--only", default=None, help="Comma-separated symbols (default: all active).")
    ap.add_argument("--refetch-all", action="store_true",
                    help="Refetch even symbols that already have a sector (default: only blanks).")
    ap.add_argument("--sleep", type=float, default=0.3, help="Politeness gap between calls (s).")
    ap.add_argument("--dry-run", action="store_true", help="Print, do not write.")
    args = ap.parse_args()

    if args.only:
        want = [s.strip().upper() for s in args.only.split(",") if s.strip()]
        df = read_sql("SELECT symbol, sector FROM universe WHERE symbol = ANY(:s)",
                      params={"s": want})
    else:
        df = read_sql("SELECT symbol, sector FROM universe WHERE is_active = TRUE ORDER BY symbol")
    if df.empty:
        log.error("No universe symbols to process.")
        sys.exit(1)

    todo = df if args.refetch_all else df[df["sector"].isna() | (df["sector"].astype(str).str.strip() == "")]
    log.info("Universe: %d symbols; %d need sectors (%s).",
             len(df), len(todo), "refetch-all" if args.refetch_all else "blank-only")

    updated, failed = 0, 0
    counts: dict[str, int] = {}
    for sym in todo["symbol"].tolist():
        sector, industry = fetch_sector(str(sym))
        if not sector:
            failed += 1
            continue
        counts[sector] = counts.get(sector, 0) + 1
        log.info("  %-12s -> %s | %s", sym, sector, industry)
        if not args.dry_run:
            execute_sql(
                "UPDATE universe SET sector = %(sec)s, industry = %(ind)s, updated_at = NOW() "
                "WHERE symbol = %(s)s",
                {"sec": sector, "ind": industry, "s": str(sym)},
            )
        updated += 1
        time.sleep(args.sleep)

    log.info("=" * 60)
    log.info("Sectors %s: %d updated, %d failed. Distribution: %s",
             "previewed" if args.dry_run else "written", updated, failed,
             dict(sorted(counts.items(), key=lambda kv: -kv[1])))
    log.info("Pairs engine candidate pairs scale ~ Σ C(sector_size, 2). Re-run "
             "scripts/run_pairs.py --dry-run to see the new candidate count.")


if __name__ == "__main__":
    main()
