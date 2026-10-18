"""
Factor engine orchestrator — computes all factors for the universe.

Calls technical, fundamental, flow, and macro sub-modules.
Writes through PIT schema. Tracks rolling 63-day IC per factor.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

from qsde.db import read_sql, get_sync_engine
from qsde.factors.technical import compute_all_technical
from qsde.factors.fundamental import compute_all_fundamental
from qsde.factors.flow import compute_all_flow
from qsde.factors.smc import compute_smc_features
from qsde.factors.patterns import compute_patterns
from qsde.factors.macro import compute_all_macro
from qsde.factors.sentiment import compute_all_sentiment

log = logging.getLogger(__name__)


def load_ohlcv(symbol: str, start: str = "2006-01-01", end: Optional[str] = None) -> pd.DataFrame:
    """Load OHLCV data for a symbol from the database."""
    if end is None:
        end = date.today().isoformat()
    df = read_sql(
        """SELECT date, open, high, low, close, volume
           FROM ohlcv WHERE symbol = :symbol AND date BETWEEN :start AND :end
           ORDER BY date""",
        params={"symbol": symbol, "start": start, "end": end},
    )
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


def compute_factors_for_symbol(symbol: str, start: str = "2006-01-01") -> pd.DataFrame:
    """
    Compute all factors for a single symbol -- technical + fundamental + flow.

    Returns:
        DataFrame indexed by date with one column per factor and a `symbol`
        column. Empty if the symbol has < 252 days of OHLCV.
    """
    df = load_ohlcv(symbol, start=start)
    if df.empty or len(df) < 252:
        log.warning("Insufficient data for %s (%d rows)", symbol, len(df))
        return pd.DataFrame()

    # 1. Technical factors (~33 columns prefixed tech_)
    tech = compute_all_technical(df)

    # 1b. SMC / price-action structure factors (~35 columns prefixed smc_):
    #     liquidity sweeps, volume profile (POC/VAH/VAL), fair-value gaps,
    #     order blocks, BOS/CHoCH, supply/demand, EMA stack, trendline breaks,
    #     candle primitives. Lookback-safe. Never let one module break the panel.
    try:
        smc = compute_smc_features(df)
    except Exception as e:
        log.warning("SMC factors failed for %s: %s", symbol, e)
        smc = pd.DataFrame(index=df.index)

    # 1c. Candlestick pattern factors (~11 columns prefixed pattern_):
    #     engulfing, hammer, stars, soldiers/crows, doji + net counts.
    try:
        pat = compute_patterns(df)
    except Exception as e:
        log.warning("pattern factors failed for %s: %s", symbol, e)
        pat = pd.DataFrame(index=df.index)

    # 2. Fundamental factors (~15 columns prefixed fund_), PIT-correct
    #    via merge_asof on filing_date <= ohlcv_date.
    fund = compute_all_fundamental(symbol, df.index)

    # 3. Flow factors (4 columns prefixed flow_) from bulk_deals, lagged
    #    one day to avoid same-day lookahead.
    flow = compute_all_flow(symbol, df.index)

    # 4. Macro factors (market-wide FRED) — same per date, joined onto the index.
    try:
        macro = compute_all_macro(df.index)
    except Exception as e:
        log.warning("macro factors failed for %s: %s", symbol, e)
        macro = pd.DataFrame(index=df.index)

    # 5. Sentiment factors (Finnhub company-news buzz + polarity), per symbol.
    try:
        senti = compute_all_sentiment(symbol, df.index)
    except Exception as e:
        log.warning("sentiment factors failed for %s: %s", symbol, e)
        senti = pd.DataFrame(index=df.index)

    # Concat side-by-side along the date index. Empty frames from
    # missing fundamentals/bulk-deals just contribute no columns.
    parts = [tech]
    if not smc.empty:
        parts.append(smc)
    if not pat.empty:
        parts.append(pat)
    if not fund.empty:
        parts.append(fund)
    if not flow.empty:
        parts.append(flow)
    if not macro.empty:
        parts.append(macro)
    if not senti.empty:
        parts.append(senti)
    factors = pd.concat(parts, axis=1)
    factors["symbol"] = symbol

    return factors


def compute_ffd_panel(
    symbols: list[str],
    start: str = "2006-01-01",
    write_tail_days: "int | None" = None,
    today: "pd.Timestamp | None" = None,
    universe: "pd.DataFrame | None" = None,
) -> "tuple[pd.DataFrame, pd.DataFrame, str]":
    """
    Compute factors for all symbols and return (combined, rows_to_persist, version).

    Pure computation: nothing is written. `compute_factors_batch` persists the
    rows to the Parquet feature store; `scripts/compute_factors_pit.py` collects
    many chunks into ONE full snapshot.

    Args:
        symbols:         universe to compute.
        start:           earliest OHLCV date to load (needs >= 252d before the
                         first date you care about so rolling factors are
                         correct).
        write_tail_days: if set, only PERSIST the last N calendar days of
                         factors to factor_pit (the freshly-changed rows),
                         while still computing the full series for correctness.
                         For a daily EOD refresh this turns a 14M-row rewrite
                         into a ~0.5M-row append — minutes -> seconds. None
                         (default) writes everything, as a full backfill wants.
        today:           reference date for the write_tail_days cutoff
                         (default: the real today; tests pin it).
        universe:        optional DataFrame with columns (date, symbol), e.g. rows of
                         `universe_pit`. When given, only those symbol-dates are
                         PERSISTED (the full history is still used to compute them),
                         which keeps factor_pit to the tradable universe.

    Returns:
        (combined, to_write, version): the FULL wide frame (date-indexed), the
        rows selected for persistence (after fracdiff, tail and universe
        filters), and the factor_version tag they must be stored under.
    """
    all_frames = []
    for i, sym in enumerate(symbols):
        if (i + 1) % 20 == 0:
            log.info("Computing factors: %d/%d symbols", i + 1, len(symbols))
        factors = compute_factors_for_symbol(sym, start=start)
        if not factors.empty:
            all_frames.append(factors)

    if not all_frames:
        return pd.DataFrame(), pd.DataFrame(), ""

    combined = pd.concat(all_frames)
    log.info("Computed factors for %d symbols, %d total rows", len(all_frames), len(combined))

    # C1 FIX (AUDIT_v2, redone in Phase 1): fractionally difference the FULL
    # per-symbol history with one fixed kernel, THEN slice the rows to persist.
    # Differencing only the tail slice (the previous behaviour) used a different,
    # auto-widened kernel than the backfill, so rows written by the daily job
    # disagreed with the rows the model was trained on (train/serve skew).
    from qsde.factors.versions import FFD_VERSION, RAW_VERSION
    try:
        ffd_full = to_ffd_frame(combined)
        version = FFD_VERSION
    except Exception as e:  # noqa: BLE001
        log.error("fracdiff at ingest failed (%s) - writing raw (version=%s), "
                  "which training and serving will NOT read.", e, RAW_VERSION)
        ffd_full = combined
        version = RAW_VERSION

    # Decide what to persist. Default = everything; daily refresh = tail only.
    to_write = ffd_full
    if write_tail_days is not None:
        base = pd.Timestamp(today) if today is not None else pd.Timestamp.today()
        cutoff = base.normalize() - pd.Timedelta(days=write_tail_days)
        if isinstance(ffd_full.index, pd.DatetimeIndex):
            to_write = ffd_full[ffd_full.index >= cutoff]
        elif "date" in ffd_full.columns:
            to_write = ffd_full[pd.to_datetime(ffd_full["date"]) >= cutoff]
        log.info("write_tail_days=%d -> persisting %d of %d rows (last %dd only)",
                 write_tail_days, len(to_write), len(ffd_full), write_tail_days)

    if universe is not None:
        wanted = pd.MultiIndex.from_arrays(
            [pd.to_datetime(universe["date"]), universe["symbol"].astype(str)])
        if isinstance(to_write.index, pd.DatetimeIndex):
            have = pd.MultiIndex.from_arrays([to_write.index, to_write["symbol"].astype(str)])
        else:
            have = pd.MultiIndex.from_arrays(
                [pd.to_datetime(to_write["date"]), to_write["symbol"].astype(str)])
        to_write = to_write[have.isin(wanted)]
        log.info("universe filter -> persisting %d symbol-days", len(to_write))

    return combined, to_write, version


def _persist(to_write: pd.DataFrame, version: str) -> None:
    """Default sink: an incremental snapshot in the Parquet feature store.

    (The old factor_pit long-form writer is no longer used for ffd_v2: two live
    stores would reintroduce the train/serve skew.)  Tests monkeypatch this.
    """
    from qsde.features.store import persist_increment

    snap = persist_increment(to_write, version)
    if snap is not None:
        log.info("Persisted %d symbol-days x %d factors to feature snapshot %s (version=%s).",
                 snap.n_rows, len(snap.columns), snap.snapshot_id, version)


def compute_factors_batch(
    symbols: list[str],
    start: str = "2006-01-01",
    write_tail_days: "int | None" = None,
    today: "pd.Timestamp | None" = None,
    universe: "pd.DataFrame | None" = None,
) -> pd.DataFrame:
    """Compute factors (see compute_ffd_panel) and persist the selected rows to the
    Parquet feature store as an incremental snapshot. Returns the FULL wide frame."""
    combined, to_write, version = compute_ffd_panel(symbols, start, write_tail_days, today, universe)
    if len(to_write):
        _persist(to_write, version)
    return combined


def to_ffd_frame(combined: pd.DataFrame) -> pd.DataFrame:
    """Fractionally difference level-form factors over each symbol's full history.

    `combined` is the engine's wide frame (date-indexed, one row per
    symbol-date, `symbol` column). Uses the fixed kernel in
    `qsde.factors.versions` (no auto-widen) and `strict=True`, so any column
    that cannot be differenced aborts the whole conversion instead of being
    stored raw under a differenced tag.
    """
    from qsde.factors.versions import FFD_D, FFD_THRES
    from qsde.models.fracdiff import apply_fracdiff_to_features

    out, _ = apply_fracdiff_to_features(
        combined.copy(),
        feature_cols=[c for c in combined.columns if c not in {"symbol", "yf_symbol", "source"}],
        default_d=FFD_D,
        thres=FFD_THRES,
        auto_widen=False,
        strict=True,
    )
    return out

