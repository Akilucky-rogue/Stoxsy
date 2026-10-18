"""Corporate-action detection and price adjustment from raw NSE bhavcopy.

Method `price_gap_fingerprint_v1`
---------------------------------
Finding that shaped this method: in the archived bhavcopy files PREV_CLOSE is
the previous RAW close, NOT adjusted on corporate-action ex-dates (464 of 465
large single-day drops checked had PREV_CLOSE == previous close; e.g. HDFCBANK
on its 2019-09-19 split prints PREV_CLOSE 2187.75 against an open of 1099.90).
So events must be inferred from the prices themselves.

An overnight gap is flagged as a split / bonus / consolidation only when ALL of:
  1. the open on day t versus the close on day t-1 (r_open) is within tolerance
     of a clean ratio (1:2 split, 1:1 bonus, 1:5 split, 10:1 consolidation, ...);
  2. day t's own range is plausible (close/open within [0.75, 1.30]);
  3. the new level persists: the median of the next three closes is within
     [0.7, 1.45] x (previous close x ratio) (a one-off crash keeps falling or
     rebounds; a re-based price just stays);
  4. both days are consecutive market sessions (a gap in our data, e.g. a
     rejected row or a suspension, is skipped rather than read as an action).
Anything large that fails 1-3 is logged as UNEXPLAINED and NOT applied.

What this method cannot see, by design (documented limitations)
    * Mild adjustments - bonuses/splits with ratio between ~0.80 and 0.97 (1:5,
      1:10 bonus ...), rights issues and cash dividends - are indistinguishable
      from ordinary price moves and are NOT adjusted. `adj_close` equals `close`.
      A reliable corporate-actions source (NSE corporate-actions feed or a
      vendor) is required to close this gap.
    * Mergers, demergers, symbol changes.

Applied adjustment: prices strictly before the ex-date are multiplied by the
event's `price_factor`; volume is divided by it (a 1:2 split doubles
historical share volume). Raw prices are never modified (they live in eod_raw).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

METHOD = "price_gap_fingerprint_v1"
ADJ_SOURCE = "nse_bhavcopy_adj_v1"

GAP_DOWN_MAX = 0.82         # open/prev_close below this: split/bonus candidate
GAP_UP_MIN = 1.40           # above this: consolidation candidate
SNAP_TOL = 0.06             # tolerance of r_open vs a clean ratio
TICK = 0.05                 # NSE tick (price rounding on thin prices)
DAY_RANGE = (0.75, 1.30)    # plausible close/open on the ex-date
PERSIST_RANGE = (0.70, 1.45)
PERSIST_DAYS = 3


def _candidates() -> list[tuple[float, str]]:
    out: dict[float, str] = {}
    for k in (2, 3, 4, 5, 8, 10, 20, 25, 50, 100):               # face-value split 1:k
        out[round(1.0 / k, 6)] = f"1:{k} split"
    for a in range(1, 11):                                         # bonus a new for b held
        for b in range(1, 11):
            r = round(b / (a + b), 6)
            if r <= 0.80:
                out.setdefault(r, f"{a}:{b} bonus")
    return sorted((r, lab) for r, lab in out.items())


_DOWN = _candidates()
_UP = [(float(k), f"{k}:1 consolidation") for k in (2, 3, 4, 5, 8, 10, 20, 25, 50, 100)]


def snap_ratio(r_open: float, r_close: float, ref_price: float) -> tuple[float, str, str] | None:
    """-> (price_factor, label, kind) for the nearest clean ratio, or None.

    Compares the geometric mean of the open and close ratios (the close adds
    information, the open is the cleaner signal) to each candidate.
    """
    if not (np.isfinite(r_open) and r_open > 0):
        return None
    if r_open <= GAP_DOWN_MAX:
        cands, kind = _DOWN, "SPLIT_BONUS"
    elif r_open >= GAP_UP_MIN:
        cands, kind = _UP, "CONSOLIDATION"
    else:
        return None
    r = float(np.sqrt(r_open * r_close)) if (np.isfinite(r_close) and r_close > 0) else r_open
    best = min(cands, key=lambda c: abs(np.log(r / c[0])))
    tol = max(SNAP_TOL, 2 * TICK / max(ref_price, TICK))
    if abs(r_open - best[0]) / best[0] > tol and abs(r - best[0]) / best[0] > tol:
        return None
    return best[0], best[1], kind


def detect_events(primary: pd.DataFrame) -> pd.DataFrame:
    """Find adjustment events in a raw frame with ONE row per (symbol, date).

    Needs columns: symbol, date, open, close (volume optional, exchange optional).
    Returns one row per event with the columns of `corp_action_events`.
    """
    d = primary.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    sessions = np.sort(d["date"].unique())
    sess_pos = {x: i for i, x in enumerate(sessions)}
    d["sess"] = d["date"].map(sess_pos)

    g = d.groupby("symbol", sort=False)
    d["c0"] = g["close"].shift(1)
    d["d0"] = g["date"].shift(1)
    d["s0"] = g["sess"].shift(1)
    d["v0"] = g["volume"].shift(1) if "volume" in d else np.nan
    for k in range(1, PERSIST_DAYS + 1):
        d[f"n{k}"] = g["close"].shift(-k)
        d[f"ns{k}"] = g["sess"].shift(-k)

    consecutive = d["s0"].notna() & (d["sess"] - d["s0"] == 1) & (d["c0"] > 0) & (d["open"] > 0)
    d["r_open"] = d["open"] / d["c0"]
    d["r_close"] = d["close"] / d["c0"]
    cand = d[consecutive & ((d["r_open"] <= GAP_DOWN_MAX) | (d["r_open"] >= GAP_UP_MIN))]

    rows = []
    for t in cand.itertuples(index=False):
        snap = snap_ratio(float(t.r_open), float(t.r_close), float(t.c0))
        day_move = float(t.close) / float(t.open) if t.open else np.nan
        fut = [getattr(t, f"n{k}") for k in range(1, PERSIST_DAYS + 1)
               if pd.notna(getattr(t, f"n{k}"))]
        persist = float(np.median(fut) / (t.c0 * snap[0])) if (fut and snap) else np.nan
        vol_ratio = (float(t.volume) / float(t.v0)) if (getattr(t, "v0", np.nan) and t.v0 and
                                                       pd.notna(t.v0) and t.v0 > 0) else np.nan
        ok = (
            snap is not None
            and DAY_RANGE[0] <= day_move <= DAY_RANGE[1]
            and (np.isnan(persist) or PERSIST_RANGE[0] <= persist <= PERSIST_RANGE[1])
        )
        kind = snap[2] if ok else "UNEXPLAINED"
        rows.append({
            "exchange": getattr(t, "exchange", "NSE") or "NSE",
            "symbol": t.symbol,
            "ex_date": t.date,
            "prev_session": t.d0,
            "close_prev_session": float(t.c0),
            "open_ex_date": float(t.open),
            "close_ex_date": float(t.close),
            "open_ratio": float(t.r_open),
            "close_ratio": float(t.r_close),
            "volume_ratio": vol_ratio,
            "persistence_ratio": persist,
            "kind": kind,
            "snapped_ratio": snap[1] if snap else None,
            "price_factor": snap[0] if ok else None,
            "applied": bool(ok),
            "method": METHOD,
        })
    cols = ["exchange", "symbol", "ex_date", "prev_session", "close_prev_session", "open_ex_date",
            "close_ex_date", "open_ratio", "close_ratio", "volume_ratio", "persistence_ratio",
            "kind", "snapped_ratio", "price_factor", "applied", "method"]
    return pd.DataFrame(rows, columns=cols)


def cumulative_factors(events: pd.DataFrame) -> dict[str, list[tuple]]:
    """symbol -> [(ex_date, price_factor)] for APPLIED events only, newest first."""
    ap = events[events["applied"]] if len(events) else events
    out: dict[str, list[tuple]] = {}
    for sym, grp in ap.groupby("symbol"):
        out[sym] = sorted(((r.ex_date, float(r.price_factor)) for r in grp.itertuples()),
                          key=lambda t: t[0], reverse=True)
    return out


def adjust_prices(primary: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """Return `primary` with open/high/low/close/volume adjusted for applied events.

    Output columns: symbol, date, open, high, low, close, adj_close, volume,
    delivery_pct, source. adj_close == close (no dividend adjustment).
    """
    d = primary.copy()
    factor = pd.Series(1.0, index=d.index)
    for sym, evs in cumulative_factors(events).items():
        m = d["symbol"] == sym
        if not m.any():
            continue
        dates = d.loc[m, "date"]
        f = pd.Series(1.0, index=dates.index)
        for ex_date, pf in evs:
            f = f.where(~(dates < ex_date), f * pf)
        factor.loc[m] = f
    for c in ("open", "high", "low", "close"):
        d[c] = d[c] * factor
    vol = d["volume"].astype(float) / factor
    d["volume"] = vol.round().astype("Int64")
    out = pd.DataFrame({
        "symbol": d["symbol"], "date": d["date"],
        "open": d["open"], "high": d["high"], "low": d["low"], "close": d["close"],
        "adj_close": d["close"], "volume": d["volume"],
        "delivery_pct": d["deliv_per"] if "deliv_per" in d else np.nan,
        "source": ADJ_SOURCE,
    })
    return out.reset_index(drop=True)


@dataclass
class EventSummary:
    total: int
    by_kind: dict
    applied: int


def summarize(events: pd.DataFrame) -> EventSummary:
    return EventSummary(
        total=int(len(events)),
        by_kind=events["kind"].value_counts().to_dict() if len(events) else {},
        applied=int(events["applied"].sum()) if len(events) else 0,
    )


# ── database ─────────────────────────────────────────────────────────

_EVENT_COLS = ["exchange", "symbol", "ex_date", "prev_session", "close_prev_session", "open_ex_date",
               "close_ex_date", "open_ratio", "close_ratio", "volume_ratio", "persistence_ratio",
               "kind", "snapped_ratio", "price_factor", "applied", "method"]


def _py(df: pd.DataFrame) -> list[tuple]:
    o = df.astype(object).where(df.notna(), None)
    return [tuple(None if v is None else (v.item() if hasattr(v, "item") else v) for v in r)
            for r in o.itertuples(index=False, name=None)]


def rebuild_from_db(symbols: list[str] | None = None) -> dict:
    """Read eod_raw -> detect events -> upsert corp_action_events -> write ohlcv.

    Idempotent. Detection always runs over the whole market (a symbol filter
    only narrows what is *written* to ohlcv), because the session calendar
    comes from all symbols.
    """
    from psycopg2.extras import execute_values

    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.ingestion.nse_eod import pick_primary_series

    raw = read_sql(
        "SELECT exchange, symbol, series, date, open, high, low, close, prev_close, "
        "volume, deliv_per FROM eod_raw WHERE exchange = 'NSE'"
    )
    if raw.empty:
        return {"events": 0, "ohlcv_rows": 0}
    primary = pick_primary_series(raw)
    events = detect_events(primary)

    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM corp_action_events WHERE method = %s", (METHOD,))
        if len(events):
            execute_values(cur, f"""
                INSERT INTO corp_action_events ({', '.join(_EVENT_COLS)}) VALUES %s
            """, _py(events[_EVENT_COLS]), page_size=2000)

        adj = adjust_prices(primary, events)
        if symbols:
            adj = adj[adj["symbol"].isin(symbols)]
        too_long = int((adj["symbol"].str.len() > 20).sum())         # ohlcv.symbol is VARCHAR(20)
        adj = adj[adj["symbol"].str.len() <= 20]
        cols = ["symbol", "date", "open", "high", "low", "close", "adj_close", "volume",
                "delivery_pct", "source"]
        recs = _py(adj[cols])
        execute_values(cur, """
            INSERT INTO ohlcv (symbol, date, open, high, low, close, adj_close, volume,
                               delivery_pct, source)
            VALUES %s
            ON CONFLICT (symbol, date) DO UPDATE SET
                open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, adj_close = EXCLUDED.adj_close,
                volume = EXCLUDED.volume, delivery_pct = EXCLUDED.delivery_pct,
                source = EXCLUDED.source, fetched_at = NOW()
             WHERE ohlcv.source = EXCLUDED.source
        """, recs, page_size=5000)
    s = summarize(events)
    return {"events": s.total, "events_by_kind": s.by_kind, "events_applied": s.applied,
            "ohlcv_rows": len(recs), "symbols_skipped_len_gt_20_rows": too_long}
