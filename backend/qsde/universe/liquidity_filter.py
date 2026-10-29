"""ADV-based liquidity classification for the universe.

Three tiers (set as `universe.liquidity_tier`):

  tradeable: trailing-60d avg daily value traded >= ₹10cr/day
             The Tier 1 rule engine + paper journal only ever touch these.

  marginal:  ₹1cr <= ADV < ₹10cr
             Ingested for data completeness but flagged as not tradeable
             (retail can't move size; slippage destroys edge).

  illiquid:  ADV < ₹1cr
             May be ingested for ground-truth (bhavcopy cross-check) but
             never appears in signal generation or paper trades.

The cutoffs come from the intraday-stress-test finding that fills become
unreliable below ~₹10cr ADV. They are env-overridable for experimentation.

Run cadence
-----------
Nightly as part of the expansion script (also runnable standalone):
    python backend/scripts/expand_universe_nifty500.py --tag-only

Tier classifications are persisted with a `tier_computed_at` timestamp so
downstream consumers know how fresh the tier is.
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pandas as pd

from qsde.db.connection import execute_sql, read_sql

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Tunables — defaults match the intraday stress test thresholds
# ──────────────────────────────────────────────────────────────────────

TRADEABLE_MIN_RUPEES = float(os.getenv("QSDE_TIER_TRADEABLE_CR", "10")) * 1e7   # ₹10cr
MARGINAL_MIN_RUPEES  = float(os.getenv("QSDE_TIER_MARGINAL_CR",  "1"))  * 1e7   # ₹1cr
ADV_WINDOW_DAYS      = int(os.getenv("QSDE_TIER_ADV_DAYS", "60"))


TIER_TRADEABLE = "tradeable"
TIER_MARGINAL = "marginal"
TIER_ILLIQUID = "illiquid"


# ──────────────────────────────────────────────────────────────────────
# ADV computation
# ──────────────────────────────────────────────────────────────────────

def compute_adv(
    window_days: int = ADV_WINDOW_DAYS,
    as_of_date: Optional[date] = None,
    symbols: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Average daily value traded per symbol over the trailing window.

    Parameters
    ----------
    window_days : int
        Trading sessions to average (default 60).
    as_of_date : date, optional
        Compute as if today were this date (for backfills). Default today.
    symbols : list[str], optional
        Restrict to these symbols. Default: all symbols with OHLCV in window.

    Returns
    -------
    pd.DataFrame
        Columns: symbol, adv_60d, n_sessions, latest_session.
        ADV in rupees (not crores). Symbols with < 10 sessions are
        included but their ADV may be unreliable; downstream classifier
        treats them defensively.
    """
    as_of_date = as_of_date or date.today()
    cutoff = as_of_date - timedelta(days=window_days * 2 + 10)  # buffer for non-trading days

    sym_filter = ""
    params: dict = {"since": cutoff}
    if symbols:
        sym_list = ",".join(f"'{s}'" for s in symbols)
        sym_filter = f"AND symbol IN ({sym_list})"

    df = read_sql(
        f"""
        SELECT symbol, date, close, volume
          FROM ohlcv
         WHERE date >= :since {sym_filter}
        """,
        params=params,
    )
    if df.empty:
        return pd.DataFrame(columns=["symbol", "adv_60d", "n_sessions", "latest_session"])

    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    df["dv"] = df["close"] * df["volume"]
    df = df.sort_values(["symbol", "date"])

    # Per symbol, take the last `window_days` sessions and compute mean DV.
    def _agg(group: pd.DataFrame) -> pd.Series:
        tail = group.tail(window_days)
        return pd.Series({
            "adv_60d": float(tail["dv"].mean()) if not tail.empty else 0.0,
            "n_sessions": int(len(tail)),
            "latest_session": tail["date"].max() if not tail.empty else None,
        })

    out = df.groupby("symbol").apply(_agg, include_groups=False).reset_index()
    return out


# ──────────────────────────────────────────────────────────────────────
# Tier classification
# ──────────────────────────────────────────────────────────────────────

def classify_tier(adv_60d: Optional[float], n_sessions: int) -> str:
    """Single-symbol tier decision."""
    # Defensive — symbols with too little history can't be tradeable
    # regardless of recent ADV (could be a new listing with a hot first day).
    if adv_60d is None or n_sessions < 20:
        return TIER_ILLIQUID
    if adv_60d >= TRADEABLE_MIN_RUPEES:
        return TIER_TRADEABLE
    if adv_60d >= MARGINAL_MIN_RUPEES:
        return TIER_MARGINAL
    return TIER_ILLIQUID


def classify_universe(
    window_days: int = ADV_WINDOW_DAYS,
    as_of_date: Optional[date] = None,
) -> pd.DataFrame:
    """Compute ADV + classify tier for every symbol with OHLCV.

    Returns DataFrame {symbol, adv_60d, n_sessions, latest_session, tier}.
    """
    adv = compute_adv(window_days=window_days, as_of_date=as_of_date)
    if adv.empty:
        return pd.DataFrame(columns=["symbol", "adv_60d", "n_sessions",
                                     "latest_session", "tier"])
    adv["tier"] = adv.apply(
        lambda r: classify_tier(r["adv_60d"], int(r["n_sessions"])), axis=1
    )
    return adv


# ──────────────────────────────────────────────────────────────────────
# Persist to universe table
# ──────────────────────────────────────────────────────────────────────

def tag_universe(classified: pd.DataFrame) -> int:
    """Write liquidity_tier + adv_60d + tier_computed_at to universe.

    UPDATE rows that already exist; do NOT insert. Symbols not in universe
    table are silently skipped (the expansion script seeds them first).
    """
    if classified.empty:
        return 0
    now = datetime.now(tz=timezone.utc)
    n = 0
    for _, r in classified.iterrows():
        execute_sql(
            """
            UPDATE universe
               SET liquidity_tier  = %(t)s,
                   adv_60d         = %(a)s,
                   tier_computed_at = %(ts)s
             WHERE symbol = %(s)s
            """,
            {
                "s": str(r["symbol"]),
                "t": str(r["tier"]),
                "a": float(r["adv_60d"]) if pd.notna(r["adv_60d"]) else None,
                "ts": now,
            },
        )
        n += 1
    log.info("Tagged %d universe rows. Tier counts: %s",
             n, classified["tier"].value_counts().to_dict())
    return n


# ──────────────────────────────────────────────────────────────────────
# Public entry — classify + persist in one call
# ──────────────────────────────────────────────────────────────────────

def refresh_universe_tiers(
    window_days: int = ADV_WINDOW_DAYS,
    as_of_date: Optional[date] = None,
) -> dict:
    """Compute and persist tier classifications for the whole OHLCV universe."""
    classified = classify_universe(window_days=window_days, as_of_date=as_of_date)
    n = tag_universe(classified)
    counts = classified["tier"].value_counts().to_dict() if not classified.empty else {}
    return {
        "symbols_classified": len(classified),
        "rows_updated":       n,
        "tier_counts":        counts,
        "as_of":              (as_of_date or date.today()).isoformat(),
    }


__all__ = [
    "TIER_TRADEABLE", "TIER_MARGINAL", "TIER_ILLIQUID",
    "TRADEABLE_MIN_RUPEES", "MARGINAL_MIN_RUPEES", "ADV_WINDOW_DAYS",
    "compute_adv", "classify_tier", "classify_universe",
    "tag_universe", "refresh_universe_tiers",
]
