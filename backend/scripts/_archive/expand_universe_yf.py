"""
Expand the NSE equity universe WITHOUT a Kite token — via yfinance.

The Kite-based `expand_universe_nifty500.py` needs a live daily token. This is the
token-free path: a curated liquid NSE set (Nifty Next-50 + liquid mid-caps on top
of the existing Nifty-50) → bulk yfinance OHLCV → factors → ADV tiers. The point is
to give the cross-sectional ML a REAL cross-section (~150 names vs 48) so option
(c) — "does a bigger universe help?" — can actually be tested.

Usage (from backend, venv active):
    python scripts/expand_universe_yf.py                 # full: list -> ohlcv -> factors -> tiers
    python scripts/expand_universe_yf.py --years 8
    python scripts/expand_universe_yf.py --no-factors    # just list + ohlcv
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import execute_sql, read_sql, upsert_dataframe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [expand-yf] %(message)s")
log = logging.getLogger("expand_yf")

# Curated liquid NSE names beyond the Nifty-50 baseline (Nifty Next-50 + liquid
# mid-caps). yfinance validates each; bad/renamed tickers simply drop out.
CURATED_LIQUID: list[str] = [
    # Nifty Next-50-ish
    "ADANIGREEN", "ADANIPOWER", "ADANIENSOL", "AMBUJACEM", "DMART", "BANKBARODA",
    "BERGEPAINT", "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL", "DABUR", "GAIL",
    "GODREJCP", "HAVELLS", "ICICIGI", "ICICIPRULI", "IOC", "INDIGO", "NAUKRI",
    "JINDALSTEL", "JIOFIN", "LICI", "MARICO", "MOTHERSON", "MUTHOOTFIN",
    "PIDILITIND", "PNB", "RECLTD", "PFC", "SBICARD", "SIEMENS", "SRF", "TATAPOWER",
    "TORNTPHARM", "TRENT", "TVSMOTOR", "UNITDSPR", "VBL", "VEDL", "ZOMATO",
    "ZYDUSLIFE", "HAL", "BEL", "IRCTC", "CGPOWER", "POLYCAB", "DLF", "LODHA",
    "INDUSTOWER", "NHPC", "ABB", "MAXHEALTH", "AXISBANK", "HINDUNILVR", "NTPC",
    "POWERGRID", "JSWSTEEL", "EICHERMOT",
    # liquid mid-caps
    "PERSISTENT", "COFORGE", "MPHASIS", "LTTS", "OFSS", "TATAELXSI", "PAGEIND",
    "PIIND", "AUROPHARMA", "LUPIN", "ALKEM", "BIOCON", "GLENMARK", "LAURUSLABS",
    "IPCALAB", "ASHOKLEY", "BALKRISIND", "MRF", "APOLLOTYRE", "ESCORTS",
    "EXIDEIND", "BHARATFORG", "CUMMINSIND", "ABBOTINDIA", "SUNTV", "PVRINOX",
    "JUBLFOOD", "TATACOMM", "IDEA", "INDHOTEL", "OBEROIRLTY", "GODREJPROP",
    "PRESTIGE", "MANAPPURAM", "IDFCFIRSTB", "FEDERALBNK", "BANDHANBNK", "AUBANK",
    "NMDC", "SAIL", "HINDZINC", "NATIONALUM", "JSWENERGY", "TORNTPOWER", "IGL",
    "PETRONET", "GUJGASLTD", "MGL", "OIL", "CONCOR", "CROMPTON", "VOLTAS",
    "DIXON", "ASTRAL", "SUPREMEIND", "DEEPAKNTR", "AARTIIND", "NAVINFLUOR",
    "TATACHEM", "UPL", "COROMANDEL", "MFSL", "PHOENIXLTD", "KPITTECH",
]


def upsert_universe(symbols: list[str]) -> int:
    import json
    df = pd.DataFrame({"symbol": [s.upper() for s in symbols]})
    df["company_name"] = ""
    df["sector"] = None            # backfill_sectors.py fills these
    df["industry"] = None
    df["index_membership"] = json.dumps(["NSE EQ (yf-expanded)"])
    df["is_active"] = True
    df["exchange"] = "NSE"
    return upsert_dataframe(df, "universe", conflict_columns=["symbol"],
                           update_columns=["is_active", "exchange", "index_membership"])


def _needs_ohlcv(min_days: int = 200) -> list[str]:
    df = read_sql(
        """SELECT u.symbol FROM universe u
             LEFT JOIN (SELECT symbol, count(*) n FROM ohlcv GROUP BY symbol) o ON o.symbol=u.symbol
            WHERE u.is_active AND u.exchange='NSE' AND COALESCE(o.n,0) < :m ORDER BY u.symbol""",
        params={"m": min_days})
    return [str(s) for s in df["symbol"].tolist()]


def backfill_ohlcv_yf(symbols: list[str], years: int) -> int:
    """Bulk-download daily OHLCV via yfinance and upsert into `ohlcv`."""
    import yfinance as yf
    if not symbols:
        log.info("No symbols need OHLCV backfill.")
        return 0
    start = (date.today() - timedelta(days=years * 365)).isoformat()
    tickers = [f"{s}.NS" for s in symbols]
    log.info("yfinance bulk download: %d tickers from %s ...", len(tickers), start)
    data = yf.download(tickers, start=start, interval="1d", group_by="ticker",
                       auto_adjust=False, threads=True, progress=False)
    rows = 0
    for sym, tk in zip(symbols, tickers):
        try:
            sub = data[tk] if isinstance(data.columns, pd.MultiIndex) else data
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            out = pd.DataFrame({
                "symbol": sym,
                "date": pd.to_datetime(sub.index).date,
                "open": sub["Open"].astype(float).values,
                "high": sub["High"].astype(float).values,
                "low": sub["Low"].astype(float).values,
                "close": sub["Close"].astype(float).values,
                "adj_close": sub["Adj Close"].astype(float).values if "Adj Close" in sub else sub["Close"].astype(float).values,
                "volume": sub["Volume"].fillna(0).astype("int64").values,
                "source": "yfinance",
            })
            rows += upsert_dataframe(out, "ohlcv", conflict_columns=["symbol", "date"],
                                    update_columns=["open", "high", "low", "close", "adj_close", "volume", "source"])
        except Exception as e:  # noqa: BLE001 — one bad ticker must not stop the rest
            log.warning("  %s: %s", sym, str(e)[:80])
    log.info("OHLCV upserted: %d rows across %d symbols.", rows, len(symbols))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Expand NSE universe via yfinance (token-free)")
    ap.add_argument("--years", type=int, default=6, help="OHLCV history to pull (years).")
    ap.add_argument("--no-factors", action="store_true")
    args = ap.parse_args()

    t0 = time.time()
    n_uni = upsert_universe(CURATED_LIQUID)
    active = int(read_sql("SELECT count(*) n FROM universe WHERE is_active AND exchange='NSE'").iloc[0]["n"])
    log.info("Universe upsert: %d curated names; %d active NSE now.", n_uni, active)

    backfill_ohlcv_yf(_needs_ohlcv(), args.years)

    if not args.no_factors:
        from qsde.factors.engine import compute_factors_batch
        syms = read_sql("SELECT symbol FROM universe WHERE is_active AND exchange='NSE' ORDER BY symbol")["symbol"].tolist()
        start = (date.today() - timedelta(days=args.years * 365 - 30)).isoformat()
        log.info("Computing factors for %d symbols from %s ...", len(syms), start)
        combined = compute_factors_batch(syms, start=start, write_tail_days=10_000)
        log.info("Factors: %d rows.", len(combined))

    from qsde.universe.liquidity_filter import refresh_universe_tiers
    log.info("Tiers: %s", refresh_universe_tiers())

    cov = read_sql("SELECT count(DISTINCT symbol) n FROM ohlcv").iloc[0]["n"]
    log.info("Done in %.0fs. OHLCV coverage now: %d symbols. Next: backfill_sectors.py "
             "then retrain.py --horizons long, then compare model_runs.", time.time() - t0, int(cov))


if __name__ == "__main__":
    main()
