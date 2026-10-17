"""
yfinance data client — batch OHLCV download for Nifty 200.

Handles:
  - Historical backfill (2006–2026, 20 years)
  - Daily incremental updates
  - Batch download with retry logic
  - Upsert into PostgreSQL ohlcv table
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from qsde.db import upsert_dataframe, read_sql

log = logging.getLogger(__name__)

# yfinance uses .NS suffix for NSE stocks
def _to_yf_symbol(symbol: str) -> str:
    """Convert internal symbol to yfinance format."""
    if symbol.startswith("^"):
        return symbol  # indices like ^NSEI
    if not symbol.endswith(".NS"):
        return f"{symbol}.NS"
    return symbol


def _from_yf_symbol(yf_symbol: str) -> str:
    """Convert yfinance symbol back to internal format."""
    return yf_symbol.replace(".NS", "")


def download_ohlcv(
    symbols: list[str],
    start: str = "2006-01-01",
    end: Optional[str] = None,
    batch_size: int = 25,
) -> pd.DataFrame:
    """
    Download OHLCV data for multiple symbols in batches.

    Args:
        symbols: List of internal symbols (e.g., ['RELIANCE', 'TCS']).
        start: Start date string (YYYY-MM-DD).
        end: End date string. Defaults to today.
        batch_size: Number of symbols per yfinance batch call.

    Returns:
        DataFrame with columns: symbol, date, open, high, low, close,
        adj_close, volume, source.
    """
    if end is None:
        end = date.today().isoformat()

    yf_symbols = [_to_yf_symbol(s) for s in symbols]
    all_frames = []

    # Process in batches to avoid yfinance rate limits
    for i in range(0, len(yf_symbols), batch_size):
        batch = yf_symbols[i:i + batch_size]
        batch_str = " ".join(batch)
        log.info(
            "Downloading OHLCV batch %d/%d (%d symbols): %s...",
            i // batch_size + 1,
            (len(yf_symbols) + batch_size - 1) // batch_size,
            len(batch),
            batch[:3],
        )

        try:
            data = yf.download(
                batch_str,
                start=start,
                end=end,
                group_by="ticker",
                auto_adjust=False,
                threads=True,
                progress=False,
            )

            if data.empty:
                log.warning("Empty data for batch starting at index %d", i)
                continue

            # Handle single vs multi-ticker response
            if len(batch) == 1:
                sym = batch[0]
                df = data.copy()
                df["yf_symbol"] = sym
                all_frames.append(df)
            else:
                for sym in batch:
                    try:
                        if sym in data.columns.get_level_values(0):
                            df = data[sym].copy()
                            df["yf_symbol"] = sym
                            all_frames.append(df)
                    except (KeyError, TypeError):
                        log.debug("No data for %s in batch", sym)

        except Exception as e:
            log.error("yfinance download failed for batch %d: %s", i // batch_size, e)
            continue

    if not all_frames:
        log.warning("No OHLCV data downloaded")
        return pd.DataFrame()

    # Combine and normalize
    combined = pd.concat(all_frames, ignore_index=False)
    combined = combined.reset_index()

    # Normalize column names (yfinance returns various capitalizations)
    col_map = {}
    for c in combined.columns:
        cl = str(c).lower().strip()
        if cl in ("date", "datetime"):
            col_map[c] = "date"
        elif cl == "open":
            col_map[c] = "open"
        elif cl == "high":
            col_map[c] = "high"
        elif cl == "low":
            col_map[c] = "low"
        elif cl == "close":
            col_map[c] = "close"
        elif cl in ("adj close", "adj_close"):
            col_map[c] = "adj_close"
        elif cl == "volume":
            col_map[c] = "volume"

    combined = combined.rename(columns=col_map)
    combined["symbol"] = combined["yf_symbol"].apply(_from_yf_symbol)
    combined["source"] = "yfinance"

    # Ensure date is proper date type
    combined["date"] = pd.to_datetime(combined["date"]).dt.date

    # Select and clean columns
    keep_cols = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume", "source"]
    for c in keep_cols:
        if c not in combined.columns:
            combined[c] = None

    result = combined[keep_cols].dropna(subset=["close"])
    result = result.drop_duplicates(subset=["symbol", "date"])

    log.info("Downloaded %d OHLCV rows for %d symbols", len(result), result["symbol"].nunique())
    return result


def backfill_ohlcv(
    symbols: list[str],
    start: str = "2006-01-01",
    end: Optional[str] = None,
) -> int:
    """
    Download and upsert historical OHLCV data into PostgreSQL.

    Args:
        symbols: List of internal symbols.
        start: Start date (YYYY-MM-DD). Default is 2006 for 20-year backfill.
        end: End date. Default is today.

    Returns:
        Number of rows upserted.
    """
    df = download_ohlcv(symbols, start=start, end=end)
    if df.empty:
        return 0

    return upsert_dataframe(
        df,
        table="ohlcv",
        conflict_columns=["symbol", "date"],
        update_columns=["open", "high", "low", "close", "adj_close", "volume", "source"],
    )


def sync_ohlcv_to_db(
    symbols: list[str],
    start: str = "2006-01-01",
    end: Optional[str] = None,
    batch_size: int = 25,
) -> int:
    """Fetch and upsert OHLCV for given symbols."""
    df = download_ohlcv(symbols, start, end, batch_size)
    if df.empty:
        return 0

    return upsert_dataframe(
        df, table="ohlcv",
        conflict_columns=["symbol", "date"],
    )

def fetch_fundamentals_batch(symbols: list[str]) -> pd.DataFrame:
    """Fetch per-period statement fundamentals from yfinance — PIT-safe.

    AUDIT_v2 C2 FIX: this function used to build a `trailing` dict from
    `tk.info` (a CURRENT snapshot: today's trailingPE, ROE, margins, market
    cap, etc.) and copy that dict into every historical fiscal-period row.
    The result was textbook lookahead — a `fund_pe` value dated 2019 carried
    the 2026 PE. Every `fund_*` factor over the training history equalled
    (approximately) its current value, and the ML model could "learn" that
    names whose 2026 PE is low outperformed in 2019.

    Fix: historical rows carry ONLY per-period statement data (revenue,
    net income, free cash flow, EPS derived from period earnings). All
    trailing/ratio columns are set to NULL for historical rows. The
    fundamentals-inclusive ML model is DEFERRED until a true point-in-time
    ratio source lands (Screener.in via Bright Data, per the architecture
    plan). Until then, factors/fundamental.py should either restrict itself
    to per-period fields or drop `fund_*` from training.

    Current-snapshot values (today's PE, ROE, etc.) are useful for LIVE
    display, but they must be fetched fresh on demand — never stamped
    into the historical `fundamentals` table.
    """
    all_rows = []

    # Columns that are LOOKAHEAD-DANGEROUS if written to historical rows.
    # We explicitly NULL these on every write so re-running the ingest
    # cleans up prior contaminated rows via ON CONFLICT UPDATE.
    _CONTAMINATED_COLUMNS = (
        "pe_ratio", "pb_ratio", "ps_ratio", "ev_ebitda", "ev_to_revenue",
        "roe", "roa", "roic", "roce",
        "gross_margin", "operating_margin", "net_margin",
        "debt_equity", "interest_coverage", "current_ratio",
        "revenue_growth_yoy", "eps_growth_yoy",
        "fcf_yield", "earnings_surprise",
        "dividend_yield", "market_cap", "enterprise_value",
    )

    for sym in symbols:
        yf_sym = _to_yf_symbol(sym)
        try:
            tk = yf.Ticker(yf_sym)

            # Fetch financials (Income Statement, Balance Sheet, Cash Flow).
            # NOTE: `tk.info` is deliberately NOT read here — it's a current
            # snapshot and stamping it into history would restore the bug.
            fin = tk.financials
            cf  = tk.cashflow

            if fin is None or fin.empty:
                log.debug("No historical financials for %s", sym)
                continue

            for col in fin.columns:
                fiscal_date = pd.to_datetime(col).date()
                # yfinance doesn't give us the real filing_date. Annual
                # results have a 60-day SEBI deadline in India; 45d is a
                # conservative-mid guess. Label this as an approximation
                # in the audit trail via source="yfinance_pit_v2".
                filing_date = fiscal_date + timedelta(days=45)

                row: dict = {
                    "symbol":      sym,
                    "fiscal_date": fiscal_date,
                    "filing_date": filing_date,
                    "source":      "yfinance_pit_v2",   # C2 fix marker
                }

                # Per-period statement fields — these are the ONLY values
                # that actually vary across fiscal periods for yfinance.
                try:
                    row["revenue"] = (
                        float(fin.loc["Total Revenue", col])
                        if "Total Revenue" in fin.index else None
                    )
                except Exception:
                    row["revenue"] = None

                try:
                    row["net_income"] = (
                        float(fin.loc["Net Income", col])
                        if "Net Income" in fin.index else None
                    )
                except Exception:
                    row["net_income"] = None

                # Free cash flow from the cashflow statement (per-period).
                row["free_cash_flow"] = None
                row["fcf_per_share"]  = None
                if cf is not None and not cf.empty and col in cf.columns:
                    try:
                        if "Free Cash Flow" in cf.index:
                            fcf = float(cf.loc["Free Cash Flow", col])
                            row["free_cash_flow"] = fcf
                            # fcf_per_share requires shares outstanding for
                            # the FISCAL PERIOD, not today. yfinance doesn't
                            # expose historical share counts; leave NULL
                            # rather than divide by today's share count and
                            # reintroduce the C2 bug.
                    except Exception:
                        pass

                # EPS from the income statement (per-period). yfinance
                # exposes `Basic EPS` and `Diluted EPS` in fin.
                row["eps"] = None
                try:
                    if "Diluted EPS" in fin.index:
                        row["eps"] = float(fin.loc["Diluted EPS", col])
                    elif "Basic EPS" in fin.index:
                        row["eps"] = float(fin.loc["Basic EPS", col])
                except Exception:
                    pass

                # NULL every ratio / current-snapshot column so a re-sync
                # over previously-contaminated rows cleans them via
                # ON CONFLICT UPDATE.
                for c in _CONTAMINATED_COLUMNS:
                    row[c] = None

                all_rows.append(row)

        except Exception as e:
            log.warning("Failed to fetch fundamentals for %s: %s", sym, e)

    df = pd.DataFrame(all_rows)
    return df

def sync_fundamentals_to_db(symbols: list[str]) -> int:
    """Fetch and upsert fundamentals for given symbols."""
    df = fetch_fundamentals_batch(symbols)
    if df.empty:
        return 0
    return upsert_dataframe(
        df, table="fundamentals",
        conflict_columns=["symbol", "fiscal_date", "filing_date"],
    )


def get_latest_date(symbol: str) -> Optional[date]:
    """Get the most recent OHLCV date for a symbol from the database."""
    try:
        df = read_sql(
            "SELECT MAX(date) as max_date FROM ohlcv WHERE symbol = :symbol",
            params={"symbol": symbol},
        )
        if not df.empty and df.iloc[0]["max_date"] is not None:
            return pd.to_datetime(df.iloc[0]["max_date"]).date()
    except Exception:
        pass
    return None


def incremental_update(symbols: list[str]) -> int:
    """
    Update OHLCV data from the latest available date to today.

    For each symbol, finds the latest date in the DB and downloads
    only the missing data.

    Returns:
        Total number of new rows upserted.
    """
    total = 0
    today = date.today()

    for sym in symbols:
        latest = get_latest_date(sym)
        if latest and latest >= today - timedelta(days=1):
            continue  # Already up to date

        start = (latest + timedelta(days=1)).isoformat() if latest else "2006-01-01"
        count = backfill_ohlcv([sym], start=start, end=today.isoformat())
        total += count

    log.info("Incremental update: %d new rows across %d symbols", total, len(symbols))
    return total
