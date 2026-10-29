#!/usr/bin/env python
"""Universe expansion — Kite-sourced top-500 by liquidity.

Replaces the 48-symbol bootstrap universe with a data-defined top-N
liquidity-ranked universe. Does NOT depend on NSE's published NIFTY 500
list (NSE blocks scraping); instead uses Kite's authoritative instrument
master + our own ADV computation.

Pipeline
--------
1. sync_universe_from_kite("NSE")
       Pull all NSE EQ tradingsymbols into the universe table.
       Uses kite_instruments (refreshes from Kite if empty).

2. Identify symbols that don't yet have OHLCV history.

3. Backfill 5 years of daily bars for those symbols via the existing
   kite_seed_ohlcv script (~3 minutes for 500 new symbols at 3 req/sec).

4. compute_factors_batch for the new symbols so factor_pit has rows
   for the engine to score.

5. refresh_universe_tiers — classify every symbol by trailing-60d ADV:
       tradeable: ADV >= ₹10cr/day
       marginal:  ₹1cr-₹10cr
       illiquid:  < ₹1cr

6. The Tier 1 rule engine reads `universe.liquidity_tier='tradeable'`
   on its next run and produces signals over the expanded set.

Usage
-----
First-time expansion (full pipeline):
    python backend/scripts/expand_universe_nifty500.py

Just re-classify existing universe (no Kite calls, fast):
    python backend/scripts/expand_universe_nifty500.py --tag-only

Limit to top-N by alphabetical for a fast smoke test:
    python backend/scripts/expand_universe_nifty500.py --limit 100

Skip OHLCV backfill (assume already done):
    python backend/scripts/expand_universe_nifty500.py --no-backfill
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [expand-universe] %(message)s",
)
log = logging.getLogger("expand_universe")


def _banner(msg: str) -> None:
    log.info("=" * 70)
    log.info(msg)
    log.info("=" * 70)


def _list_universe_symbols_without_ohlcv(min_history_days: int = 30) -> list[str]:
    """Symbols in universe.is_active=TRUE that don't have at least
    `min_history_days` of OHLCV history. These need backfilling."""
    from qsde.db.connection import read_sql
    df = read_sql(
        """
        SELECT u.symbol
          FROM universe u
     LEFT JOIN (
                  SELECT symbol, COUNT(*) AS n
                    FROM ohlcv
                GROUP BY symbol
               ) o ON o.symbol = u.symbol
         WHERE u.is_active = TRUE
           AND COALESCE(o.n, 0) < :min_days
         ORDER BY u.symbol
        """,
        params={"min_days": min_history_days},
    )
    return [str(s) for s in df["symbol"].tolist()]


def step_sync_universe(exchange: str = "NSE", limit: int | None = None) -> int:
    """Step 1: pull NSE/BSE equities from Kite into universe table."""
    from qsde.ingestion.universe import sync_universe_from_kite
    # replace=False so existing 48 symbols + any prior expansion survive.
    n = sync_universe_from_kite(exchange=exchange, limit=limit, replace=False)
    log.info("Universe sync (%s): %d symbols upserted", exchange, n)
    return n


def step_backfill_ohlcv(years: int = 5) -> int:
    """Step 2-3: backfill OHLCV for symbols without enough history."""
    candidates = _list_universe_symbols_without_ohlcv()
    if not candidates:
        log.info("All universe symbols already have OHLCV; skipping backfill")
        return 0
    log.info("Need backfill for %d symbols (5y daily)", len(candidates))

    start = (date.today() - timedelta(days=years * 365)).isoformat()
    script_path = Path(__file__).resolve().parent / "kite_seed_ohlcv.py"
    # kite_seed_ohlcv iterates the active universe, so it'll pick up the
    # new symbols automatically. Run it as a subprocess to preserve its
    # rate-limiting + progress logging + retry behavior.
    cmd = [sys.executable, str(script_path), "--from", start]
    log.info("Invoking: %s", " ".join(cmd))
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        log.error("kite_seed_ohlcv exited %d; backfill incomplete", result.returncode)
    log.info("Backfill done in %.1fs", time.time() - t0)
    return len(candidates)


def step_compute_factors() -> int:
    """Step 4: compute factor_pit rows for the freshly-backfilled symbols."""
    from qsde.factors.engine import compute_factors_batch
    from qsde.db.connection import read_sql
    symbols = [
        str(s) for s in read_sql(
            "SELECT symbol FROM universe WHERE is_active = TRUE ORDER BY symbol"
        )["symbol"].tolist()
    ]
    if not symbols:
        log.warning("No active universe symbols; skipping factor compute")
        return 0
    start = (date.today() - timedelta(days=400)).isoformat()
    log.info("Computing factors for %d symbols from %s...", len(symbols), start)
    t0 = time.time()
    combined = compute_factors_batch(symbols, start=start, write_tail_days=400)
    log.info("Factor compute: %d rows in %.1fs", len(combined), time.time() - t0)
    return int(len(combined))


def step_tag_tiers() -> dict:
    """Step 5: classify every symbol by ADV and write liquidity_tier."""
    from qsde.universe.liquidity_filter import refresh_universe_tiers
    summary = refresh_universe_tiers()
    log.info("Tier classification: %s", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--exchange", default="NSE", help="NSE or BSE (default NSE).")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap symbols from Kite (alphabetical) for faster first run.")
    ap.add_argument("--years", type=int, default=5,
                    help="OHLCV backfill history in years (default 5).")
    ap.add_argument("--no-sync", action="store_true",
                    help="Skip universe sync from Kite (assume already done).")
    ap.add_argument("--no-backfill", action="store_true",
                    help="Skip OHLCV backfill.")
    ap.add_argument("--no-factors", action="store_true",
                    help="Skip factor compute.")
    ap.add_argument("--tag-only", action="store_true",
                    help="Only run the tier classification step.")
    args = ap.parse_args()

    t_start = time.time()

    if args.tag_only:
        _banner("Liquidity tier classification only (--tag-only)")
        step_tag_tiers()
        log.info("Total: %.1fs", time.time() - t_start)
        return

    if not args.no_sync:
        _banner(f"1/4  Sync universe from Kite ({args.exchange})")
        step_sync_universe(exchange=args.exchange, limit=args.limit)

    if not args.no_backfill:
        _banner("2/4  Backfill OHLCV for new symbols")
        step_backfill_ohlcv(years=args.years)

    if not args.no_factors:
        _banner("3/4  Compute factors for active universe")
        step_compute_factors()

    _banner("4/4  Classify liquidity tiers")
    step_tag_tiers()

    _banner(f"Expansion complete in {time.time() - t_start:.1f}s")


if __name__ == "__main__":
    main()
