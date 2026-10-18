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
# Official adjusted series = events CONFIRMED BY THE NSE SOURCE (confidence 'high') only.
# Price-only events are 'low' confidence: they are kept in corp_action_events with applied=FALSE and
# applied_research=TRUE, and never alter the official `ohlcv` (owner decision 2026-10-18). Research runs can
# opt in to them via qsde.ml.data.load_prices(policy="research").
ADJ_SOURCE = "nse_adj_v2_src"          # ohlcv.source is varchar(20)

GAP_DOWN_MAX = 0.78         # open/prev_close at/below this: split/bonus candidate. NSE price bands cap a
                            # banded stock's one-day fall at 20%, so a gap to ~0.80 is a circuit move and
                            # indistinguishable from a 1:4 bonus: those need the corporate-actions source.
GAP_UP_MIN = 1.40           # above this: consolidation candidate
SNAP_TOL = 0.06             # tolerance of the open ratio vs a common ratio
TICK = 0.05                 # NSE tick (price rounding on thin prices)
MIN_PRICE = 2.0             # below this a single tick is >=2.5% and gaps are tick noise
DAY_RANGE = (0.75, 1.30)    # plausible close/open on the ex-date
PERSIST_RANGE = (0.70, 1.45)
PERSIST_DAYS = 3
# Rights entitlements and warrants trade as separate "securities" with wild prices.
_EXCLUDED_SYMBOL = r"(?:-RE\d*|-W\d*|-PP\d*)$"

# Ratios that real splits / bonuses actually use (new price / old price). A dense
# grid of a:b bonus ratios over-fits: a 1:5 split (0.2) was snapping to "7:2 bonus"
# (0.222), an 11% factor error. Anything that is not within SNAP_TOL of one of
# these is UNEXPLAINED and NOT applied (a visible cliff beats a wrong factor).
COMMON_DOWN: tuple[float, ...] = (
    1 / 2,      # 1:2 split, 1:1 bonus
    1 / 3,      # 1:3 split, 2:1 bonus
    1 / 4,      # 1:4 split, 3:1 bonus
    1 / 5,      # 1:5 split, 4:1 bonus
    1 / 6,      # 5:1 bonus (e.g. Nykaa 2022)
    1 / 10, 1 / 20, 1 / 25, 1 / 50, 1 / 100,
    2 / 3,      # 1:2 bonus
    3 / 4,      # 1:3 bonus
    # 4/5 (1:4 bonus) is DELIBERATELY absent: it equals a 20% lower circuit. 458 of the first
    # version's 1,195 applied events sat at 0.8 and only 3% matched any NSE action.
    2 / 5,      # 3:2 bonus
    3 / 5,      # 2:3 bonus
)
COMMON_UP: tuple[float, ...] = (2.0, 3.0, 4.0, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)


def _label(r: float) -> str:
    from fractions import Fraction
    f = Fraction(r).limit_denominator(100)
    return f"clean {f.numerator}/{f.denominator}"


def snap_ratio(r_open: float, r_close: float, ref_price: float) -> tuple[float, str, str] | None:
    """-> (price_factor, label, kind) for the nearest COMMON ratio, or None.

    Matches on the OPEN ratio only. Measured on 174 liquid detections (>= Rs100cr
    traded on the ex-date) the open lands within 4% of a common ratio 83% of the
    time and within 6% 93% of the time, whereas the close (which absorbs the
    whole day's move) manages 63% / 75%; blending in the close made things
    worse. `r_close` is accepted for API symmetry and used only by the caller's
    day-range check. The factor returned is the exact common ratio, not the
    noisy measurement.
    """
    if not (np.isfinite(r_open) and r_open > 0) or ref_price < MIN_PRICE:
        return None
    if r_open <= GAP_DOWN_MAX:
        cands, kind = COMMON_DOWN, "SPLIT_BONUS"
    elif r_open >= GAP_UP_MIN:
        cands, kind = COMMON_UP, "CONSOLIDATION"
    else:
        return None
    best = min(cands, key=lambda c: abs(np.log(r_open / c)))
    if abs(r_open - best) / best > SNAP_TOL:
        return None
    return best, _label(best), kind


def detect_events(primary: pd.DataFrame) -> pd.DataFrame:
    """Find adjustment events in a raw frame with ONE row per (symbol, date).

    Needs columns: symbol, date, open, close (volume optional, exchange optional).
    Returns one row per event with the columns of `corp_action_events`.
    """
    d = primary.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    d = d[~d["symbol"].str.contains(_EXCLUDED_SYMBOL, regex=True)].reset_index(drop=True)
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
    cand = d[consecutive & (d["c0"] >= MIN_PRICE)
             & ((d["r_open"] <= GAP_DOWN_MAX) | (d["r_open"] >= GAP_UP_MIN))]

    rows = []
    for t in cand.itertuples(index=False):
        snap = snap_ratio(float(t.r_open), float(t.r_close), float(t.c0))
        day_move = float(t.close) / float(t.open) if t.open else np.nan
        fut = [getattr(t, f"n{k}") for k in range(1, PERSIST_DAYS + 1)
               if pd.notna(getattr(t, f"n{k}"))]
        # An event is only CONFIRMED once PERSIST_DAYS later sessions exist: with fewer (the last sessions of
        # the data, i.e. every daily run) a one-day crash that happens to snap to a common ratio would be
        # adjusted away on the day it happens. Such gaps stay "pending" (UNEXPLAINED, not applied) until
        # the new level has held.
        pending = bool(snap) and len(fut) < PERSIST_DAYS
        persist = float(np.median(fut) / (t.c0 * snap[0])) if (len(fut) >= PERSIST_DAYS and snap) else np.nan
        vol_ratio = (float(t.volume) / float(t.v0)) if (getattr(t, "v0", np.nan) and t.v0 and
                                                       pd.notna(t.v0) and t.v0 > 0) else np.nan
        ok = (
            snap is not None
            and not pending
            and DAY_RANGE[0] <= day_move <= DAY_RANGE[1]
            and PERSIST_RANGE[0] <= persist <= PERSIST_RANGE[1]
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
            "applied": bool(ok),            # "would apply": combine_events demotes price-only events
            "method": METHOD,
            "source": "price_inferred", "source_purpose": None, "ex_date_shift": 0,
            "applied_research": bool(ok), "confidence": "low",
        })
    cols = ["exchange", "symbol", "ex_date", "prev_session", "close_prev_session", "open_ex_date",
            "close_ex_date", "open_ratio", "close_ratio", "volume_ratio", "persistence_ratio",
            "kind", "snapped_ratio", "price_factor", "applied", "method", "source", "source_purpose",
            "ex_date_shift", "applied_research", "confidence"]
    return pd.DataFrame(rows, columns=cols)


RECENT_GAP_THRESHOLD = 0.18     # |open/prev close - 1| that makes a fresh gap suspect
RECENT_GAP_SESSIONS = 3         # = PERSIST_DAYS: how long a gap stays unconfirmed


def flag_recent_gaps(recent: pd.DataFrame, applied: pd.DataFrame, n_sessions: int = RECENT_GAP_SESSIONS,
                     thr: float = RECENT_GAP_THRESHOLD) -> list[str]:
    """Symbols that must NOT be scored today: a large overnight gap in the last `n_sessions` sessions that is
    not an applied corporate-action event.

    A price-inferred split needs PERSIST_DAYS later sessions to be confirmed, so on the day it happens (and
    the two after) the series still contains a raw cliff that would feed every indicator. A genuine
    crash is also skipped for the same 3 sessions: conservative by design.

    recent:  symbol, date, open, close (a few sessions per symbol, ascending, adjusted or raw).
    applied: symbol, ex_date of APPLIED events (so already-adjusted gaps do not flag).
    """
    d = recent.sort_values(["symbol", "date"]).copy()
    d["date"] = pd.to_datetime(d["date"])
    d["prev_close"] = d.groupby("symbol")["close"].shift(1)
    d["gap"] = d["open"] / d["prev_close"] - 1.0
    last_dates = sorted(d["date"].unique())[-n_sessions:]
    cand = d[d["date"].isin(last_dates) & (d["gap"].abs() >= thr)]
    if cand.empty:
        return []
    ap = applied.copy()
    ap["ex_date"] = pd.to_datetime(ap["ex_date"])
    keys = set(zip(ap["symbol"], ap["ex_date"]))
    flagged = [s for s, dt in zip(cand["symbol"], cand["date"]) if (s, dt) not in keys]
    return sorted(set(flagged))


def recent_gap_symbols_from_db(n_sessions: int = RECENT_GAP_SESSIONS, thr: float = RECENT_GAP_THRESHOLD) -> list[str]:
    from qsde.db.connection import read_sql

    recent = read_sql("""SELECT symbol, date, open, close FROM ohlcv
                          WHERE date >= (SELECT max(date) FROM ohlcv) - INTERVAL '14 days'
                       ORDER BY symbol, date""")
    if recent.empty:
        return []
    applied = read_sql("SELECT symbol, ex_date FROM corp_action_events WHERE applied")
    return flag_recent_gaps(recent, applied, n_sessions, thr)


def combine_events(source_events: pd.DataFrame, inferred: pd.DataFrame, source_all: pd.DataFrame,
                   sessions: "pd.DatetimeIndex | np.ndarray", window: int = 2) -> pd.DataFrame:
    """Merge source-confirmed and price-inferred events into the one set that gets applied.

      * a price-inferred event next to a SOURCE split/bonus/consolidation row (+-window sessions) is
        dropped: the source row already decided (applied or SOURCE_UNCONFIRMED);
      * next to a source DEMERGER / scheme row it becomes UNEXPLAINED and is NOT applied (a
        demerger's price cliff is real value leaving the stock, not a ratio we can trust);
      * otherwise it stands as a price-inferred event.
    """
    sess = np.sort(np.asarray(pd.to_datetime(pd.Series(sessions)).unique()))

    def pos(dates) -> np.ndarray:
        return np.searchsorted(sess, np.asarray(pd.to_datetime(pd.Series(dates)), dtype="datetime64[ns]"))

    inf = inferred.copy()
    if "applied_research" not in inf:                      # tolerate frames from older detectors
        inf["applied_research"] = inf["applied"].astype(bool) if len(inf) else pd.Series(dtype=bool)
    if "confidence" not in inf:
        inf["confidence"] = "low"
    if len(inf):
        inf["_p"] = pos(inf["ex_date"])
        src_sb = source_all[source_all["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"])]
        src_dm = source_all[source_all["category"].isin(["DEMERGER"])]
        sb_by = {s: pos(g["ex_date"]) for s, g in src_sb.groupby("symbol")}
        dm_by = {s: pos(g["ex_date"]) for s, g in src_dm.groupby("symbol")}
        keep, demote = [], []
        for i, r in inf.iterrows():
            near = lambda arr: arr is not None and bool((np.abs(arr - r["_p"]) <= window).any())  # noqa: E731
            if near(sb_by.get(r["symbol"])):
                keep.append(False)
            else:
                keep.append(True)
            demote.append(near(dm_by.get(r["symbol"])))
        inf = inf[np.array(keep)].copy()
        dm = np.array(demote)[np.array(keep)] if len(keep) else np.array([], bool)
        if dm.any():
            inf.loc[dm, ["kind", "applied", "price_factor"]] = ["UNEXPLAINED", False, np.nan]
            inf.loc[dm, "applied_research"] = False
            inf.loc[dm, "snapped_ratio"] = "demerger/scheme in source"
        inf = inf.drop(columns="_p")
    if len(source_events):
        src = source_events.copy()
        src["applied_research"] = src["applied"]
        src["confidence"] = "high"
    else:
        src = source_events
    out = pd.concat([src, inf], ignore_index=True) if len(src) else inf
    if len(out):
        # POLICY: only NSE-source-confirmed events alter the official adjusted series. A price-only event is
        # 'low' confidence: it stays visible (and usable by explicit research runs) but is NOT applied officially.
        low = out["source"].eq("price_inferred")
        out.loc[low, "applied"] = False
        out.loc[low, "confidence"] = "low"
        out["applied"] = out["applied"].astype(bool)
        out["applied_research"] = out["applied_research"].astype(bool)
    return out[EVENT_COLS_ALL].reset_index(drop=True) if len(out) else out


EVENT_COLS_ALL = ["exchange", "symbol", "ex_date", "prev_session", "close_prev_session", "open_ex_date",
                  "close_ex_date", "open_ratio", "close_ratio", "volume_ratio", "persistence_ratio",
                  "kind", "snapped_ratio", "price_factor", "applied", "method", "source", "source_purpose",
                  "ex_date_shift", "applied_research", "confidence"]


def cumulative_factors(events: pd.DataFrame, flag: str = "applied") -> dict[str, list[tuple]]:
    """symbol -> [(ex_date, price_factor)] for events with `flag` True ('applied' = official series,
    'applied_research' = official + low-confidence price-inferred), newest first."""
    ap = events[events[flag]] if len(events) else events
    out: dict[str, list[tuple]] = {}
    for sym, grp in ap.groupby("symbol"):
        out[sym] = sorted(((r.ex_date, float(r.price_factor)) for r in grp.itertuples()),
                          key=lambda t: t[0], reverse=True)
    return out


def adjust_prices(primary: pd.DataFrame, events: pd.DataFrame, flag: str = "applied") -> pd.DataFrame:
    """Return `primary` with open/high/low/close/volume adjusted for the events flagged `flag`.

    Output columns: symbol, date, open, high, low, close, adj_close, volume,
    delivery_pct, source. adj_close == close (no dividend adjustment).
    """
    d = primary.copy()
    factor = pd.Series(1.0, index=d.index)
    for sym, evs in cumulative_factors(events, flag).items():
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
               "kind", "snapped_ratio", "price_factor", "applied", "method", "source", "source_purpose",
               "ex_date_shift", "applied_research", "confidence",
               "approval_tier", "isin", "source_url", "retrieved_at", "evidence_ref"]


def _py(df: pd.DataFrame) -> list[tuple]:
    o = df.astype(object).where(df.notna(), None)
    return [tuple(None if v is None else (v.item() if hasattr(v, "item") else v) for v in r)
            for r in o.itertuples(index=False, name=None)]


def _source_events(primary: pd.DataFrame, inferred: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Source-confirmed events + price-inferred ones that the source does not already cover."""
    from qsde.ingestion import corp_actions_source as cas

    if not cas.SOURCE_FILE.exists():
        log.warning("corporate-actions source %s not found: NO event is applied to the official series "
                    "(price-only events are low confidence); they remain available as applied_research",
                    cas.SOURCE_FILE)
        ev = inferred.copy()
        if len(ev):
            ev["applied_research"] = ev["applied"].astype(bool)
            ev["applied"] = False
            ev["confidence"] = "low"
        return ev, {"source": "absent"}
    src = cas.load_source()
    confirmed = cas.reconcile(src, primary)
    sessions = np.sort(primary["date"].unique())
    events = combine_events(confirmed, inferred, src, sessions)
    from qsde.ingestion.corp_actions_approvals import apply_approvals
    try:
        from qsde.db.connection import read_sql
        nse = read_sql("SELECT symbol, category, factor, ex_date, isin, known_at FROM nse_corporate_actions "
                       "WHERE category IN ('SPLIT_BONUS', 'CONSOLIDATION') AND factor IS NOT NULL")
        ren = read_sql("SELECT old_symbol, new_symbol FROM symbol_changes "
                       "WHERE verification_status = 'official' AND change_type = 'symbol_change'")
    except Exception as e:  # noqa: BLE001 - without the NSE tables only the price-confirmed tier can apply
        log.warning("official-record tables unavailable (%s): official_confirmed approvals will be refused", e)
        nse = ren = None
    events, approval_report = apply_approvals(events, primary, nse_records=nse, renames=ren)
    for c in ("approval_tier", "isin", "source_url", "retrieved_at", "evidence_ref"):
        if c not in events.columns:
            events[c] = None
    summ = {"source_rows": int(len(src)),
            "source_split_bonus_rows": int(src["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"]).sum()),
            "source_confirmed": int(confirmed["applied"].sum()) if len(confirmed) else 0,
            "source_unconfirmed": int((confirmed["kind"] == "SOURCE_UNCONFIRMED").sum()) if len(confirmed) else 0,
            "price_inferred_kept": int((events["source"] == "price_inferred").sum()) if len(events) else 0,
            "owner_approvals": approval_report}
    return events, summ


def rebuild_from_db(symbols: list[str] | None = None) -> dict:
    """Read eod_raw -> source-confirmed + price-inferred events -> corp_action_events -> write ohlcv.

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
    inferred = detect_events(primary)
    events, src_summary = _source_events(primary, inferred)

    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM corp_action_events")        # derived table: always rebuilt whole
        if len(events):
            execute_values(cur, f"""
                INSERT INTO corp_action_events ({', '.join(_EVENT_COLS)}) VALUES %s
            """, _py(events[_EVENT_COLS]), page_size=2000)

        # Official series: NSE-source-confirmed events only. Replace earlier adjusted rows wholesale (their
        # `source` tag changes with the policy, so ON CONFLICT .. WHERE source = source would skip them).
        if symbols:
            cur.execute("DELETE FROM ohlcv WHERE (source LIKE 'nse_bhavcopy_adj_%%' OR source LIKE 'nse_adj_%%') AND symbol = ANY(%s)", (symbols,))
        else:
            cur.execute("DELETE FROM ohlcv WHERE source LIKE 'nse_bhavcopy_adj_%%' OR source LIKE 'nse_adj_%%'")
        adj = adjust_prices(primary, events, "applied")
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
    return {"events": s.total, "events_by_kind": s.by_kind, "events_applied_official": s.applied,
            "events_applied_research_only": int((events["applied_research"] & ~events["applied"]).sum())
            if len(events) and "applied_research" in events else 0,
            "source": src_summary,
            "ohlcv_rows": len(recs), "symbols_skipped_len_gt_20_rows": too_long}
