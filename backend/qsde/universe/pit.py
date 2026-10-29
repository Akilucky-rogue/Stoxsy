"""Point-in-time liquid universe built from raw exchange EOD (`eod_raw`).

For every trading date T a symbol is a member if, using ONLY data up to and
including T:
  * trailing-`window`-session median traded value ranks in the top `top_n`,
  * raw close >= `min_price`,
  * it has at least `min_history` sessions of history,
  * it is plain equity (rights entitlements, warrants and ETFs are excluded).

Because membership is recomputed from each date's own trailing data, delisted
and fallen names are members while they were liquid - the survivorship bias of a
fixed current-day list is removed (AUDIT H6).

Traded value uses the exchange-reported TURNOVER_LACS (1 lakh = 1e5 rupees).
"""
from __future__ import annotations

import logging
import re

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_EXCLUDE = re.compile(r"(?:-RE\d*|-W\d*|-PP\d*)$|ETF|BEES")
LAKH = 1e5
CRORE = 1e7


def build_universe_pit(primary: pd.DataFrame, top_n: int = 500, window: int = 60,
                       min_price: float = 10.0, min_history: int = 252,
                       min_periods: int | None = None) -> pd.DataFrame:
    """primary: one row per (symbol, date) with symbol, date, close, turnover_lacs.

    Returns columns date, symbol, rank, adv_cr, close, top_n, method.
    """
    min_periods = min_periods or max(20, int(window * 2 / 3))
    d = primary[~primary["symbol"].str.contains(_EXCLUDE)].copy()
    d["tv_cr"] = d["turnover_lacs"].astype(float) * LAKH / CRORE

    tv = d.pivot(index="date", columns="symbol", values="tv_cr").sort_index()
    close = d.pivot(index="date", columns="symbol", values="close").sort_index()

    # A symbol that did not trade on a date contributes 0 value that day (it was
    # not liquid then); sessions before its first print stay NaN.
    first_seen = tv.notna().cumsum() > 0
    tv = tv.fillna(0.0).where(first_seen)
    adv = tv.rolling(window, min_periods=min_periods).median()
    history = tv.notna().cumsum()

    eligible = (adv.notna() & (history >= min_history)
                & (close.ffill() >= min_price) & close.notna())
    ranked = adv.where(eligible).rank(axis=1, ascending=False, method="first")
    member = ranked <= top_n

    long = (
        ranked.where(member).stack().rename("rank").reset_index()
        .rename(columns={"level_1": "symbol"})
    )
    long.columns = ["date", "symbol", "rank"]
    long["rank"] = long["rank"].astype(int)
    long["adv_cr"] = [adv.at[dt, s] for dt, s in zip(long["date"], long["symbol"])]
    long["close"] = [close.at[dt, s] for dt, s in zip(long["date"], long["symbol"])]
    long["top_n"] = top_n
    long["method"] = f"adv{window}_top{top_n}_v1"
    return long.sort_values(["date", "rank"]).reset_index(drop=True)


def write_universe_pit(df: pd.DataFrame) -> int:
    """Replace the rows of this method, then bulk insert."""
    from psycopg2.extras import execute_values

    from qsde.db.connection import get_sync_conn

    if df.empty:
        return 0
    method = df["method"].iloc[0]
    cols = ["date", "symbol", "rank", "adv_cr", "close", "top_n", "method"]
    o = df[cols].astype(object).where(df[cols].notna(), None)
    recs = [tuple(None if v is None else (v.item() if hasattr(v, "item") else v) for v in r)
            for r in o.itertuples(index=False, name=None)]
    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM universe_pit WHERE method = %s", (method,))
        execute_values(cur, f"INSERT INTO universe_pit ({', '.join(cols)}) VALUES %s",
                       recs, page_size=10000)
    return len(recs)


def build_from_db(top_n: int = 500, window: int = 60) -> dict:
    from qsde.db.connection import read_sql
    from qsde.ingestion.nse_eod import pick_primary_series

    raw = read_sql("SELECT exchange, symbol, series, date, close, turnover_lacs "
                   "FROM eod_raw WHERE exchange = 'NSE'")
    prim = pick_primary_series(raw)
    uni = build_universe_pit(prim, top_n=top_n, window=window)
    n = write_universe_pit(uni)
    per_day = uni.groupby("date").size()
    return {"rows": n, "dates": int(per_day.shape[0]), "symbols_ever": int(uni["symbol"].nunique()),
            "median_members_per_day": float(per_day.median()),
            "first_member_date": str(uni["date"].min()), "last_member_date": str(uni["date"].max())}
