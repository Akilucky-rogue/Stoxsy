"""Price and event loading for model-lab runs, with an explicit corporate-action policy.

  official  (default)  `ohlcv` as stored: NSE-source-confirmed adjustments only. Windows touching a
                       price-inferred event are EXCLUDED from labels (they contain a raw cliff), as are
                       UNEXPLAINED / SOURCE_UNCONFIRMED events.
  research             official + the low-confidence price-inferred events applied on the fly. Never used
                       for promotion; the model card must say that corporate-action adjustment is incomplete.
"""
from __future__ import annotations

from typing import Optional, Sequence

import pandas as pd

OFFICIAL = "official"
RESEARCH = "research"
PROMOTION_POLICY = OFFICIAL


def load_prices(symbols: Optional[Sequence[str]] = None, policy: str = OFFICIAL,
                start: Optional[str] = None) -> pd.DataFrame:
    """symbol, date, open, high, low, close, volume (adjusted per `policy`)."""
    from qsde.db.connection import read_sql

    if policy not in (OFFICIAL, RESEARCH):
        raise ValueError(f"unknown corporate-action policy {policy!r}")
    where, params = ["source LIKE 'nse_adj_%'"], {}
    if symbols is not None:
        where.append("symbol = ANY(:s)")
        params["s"] = list(symbols)
    if start:
        where.append("date >= CAST(:st AS DATE)")
        params["st"] = start
    px = read_sql(f"SELECT symbol, date, open, high, low, close, volume FROM ohlcv WHERE {' AND '.join(where)} "
                  f"ORDER BY symbol, date", params=params)
    px["date"] = pd.to_datetime(px["date"])
    if policy == RESEARCH and len(px):
        ev = read_sql("SELECT symbol, ex_date, price_factor FROM corp_action_events "
                      "WHERE applied_research AND NOT applied AND symbol = ANY(:s)",
                      params={"s": sorted(px["symbol"].unique())})
        px = apply_extra_events(px, ev)
    return px


def apply_extra_events(px: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Multiply prices strictly before each event's ex-date by its factor (volume divided)."""
    if events is None or events.empty:
        return px
    out = px.copy()
    ev = events.copy()
    ev["ex_date"] = pd.to_datetime(ev["ex_date"])
    for sym, grp in ev.groupby("symbol"):
        m = out["symbol"] == sym
        if not m.any():
            continue
        f = pd.Series(1.0, index=out.index[m])
        dates = out.loc[m, "date"]
        for r in grp.itertuples():
            f = f.where(~(dates < r.ex_date), f * float(r.price_factor))
        for c in ("open", "high", "low", "close"):
            out.loc[m, c] = out.loc[m, c] * f
        if "volume" in out:
            out["volume"] = out["volume"].astype(float)
            out.loc[m, "volume"] = out.loc[m, "volume"] / f
    return out


def label_exclusion_events(symbols: Sequence[str], policy: str = OFFICIAL) -> pd.DataFrame:
    """Events whose ex-date must not fall inside a label window / shortly before a feature date.

    official: UNEXPLAINED + SOURCE_UNCONFIRMED + every price-inferred event (applied or not: under the
              official series they are raw cliffs)
    research: UNEXPLAINED + SOURCE_UNCONFIRMED only (inferred events are adjusted in the research prices)
    """
    from qsde.db.connection import read_sql

    clause = "kind IN ('UNEXPLAINED','SOURCE_UNCONFIRMED')"
    if policy == OFFICIAL:
        clause += " OR (source = 'price_inferred' AND applied_research)"
    return read_sql(f"SELECT symbol, ex_date FROM corp_action_events WHERE ({clause}) AND symbol = ANY(:s)",
                    params={"s": list(symbols)})
