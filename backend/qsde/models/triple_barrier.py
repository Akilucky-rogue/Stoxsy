"""
Triple-barrier method for label generation.

Reference: López de Prado, "Advances in Financial Machine Learning" (2018),
Chapter 3. Code adapted from Snippets 3.1, 3.2, 3.3, and 3.4.

Why this module exists
----------------------
Fixed-horizon labels — "what's the return from t to t+H?" — are the lazy
default and create three problems for ML on returns:

  1. The label sees TIME, not OUTCOME.
  2. Symmetric in profit-take vs stop-loss.
  3. Distribution dominated by mid-zero noise.

Triple-barrier fixes all three: for each event (factor as-of-date), set three
barriers — profit-take, stop-loss, and time — and label the event by whichever
fires first.

Stage 2C.1 fixes (AUDIT_v2 H2 + H4):
  * H2 — t1 is now measured in NSE SESSIONS via positional stepping through
         the price index, not calendar days via Timedelta. A "swing" trade's
         time barrier is 5 sessions (~7 calendar days), not 5 calendar days
         (~3 sessions) as before.
  * H4 — PT/SL hits are detected on the bar's HIGH / LOW (long: high >= PT,
         low <= SL; short mirrors), not closes only. Same-bar tie between
         PT and SL is resolved PESSIMISTICALLY — SL wins — matching the paper
         journal's execution assumption so labels measure what production
         measures.

Public API
----------
  get_daily_volatility(close, span=100)
  get_t1_barriers(close, t_events, num_sessions)
  get_events(bars, t_events, pt_sl, target, t1, side=None, min_ret=0.0)
  get_bins(events, close)
  apply_triple_barrier_labels(dataset, ohlcv, horizon)   <- the one called by dataset.py
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# Per-horizon (pt_mult, sl_mult, t1_sessions) calibration.
# t1_sessions is NSE trading sessions, not calendar days (H2 fix).
from qsde.ml.registry import legacy_barriers

_HORIZON_BARRIERS: dict[str, dict[str, float]] = legacy_barriers()   # derived: qsde.ml.registry


# ── volatility ─────────────────────────────────────────────────────

def get_daily_volatility(close: pd.Series, span: int = 100) -> pd.Series:
    """EWM-std of daily log returns. AFML Snippet 3.1."""
    if close.empty or len(close) < 2:
        return pd.Series(dtype=float)
    df0 = close.index.searchsorted(close.index - pd.Timedelta(days=1))
    df0 = df0[df0 > 0]
    idx_pairs = pd.Series(
        close.index[df0 - 1],
        index=close.index[close.shape[0] - df0.shape[0]:],
    )
    rets = close.loc[idx_pairs.index] / close.loc[idx_pairs.values].values - 1
    return rets.ewm(span=span, min_periods=max(2, span // 4)).std()


# ── time barrier (vertical) — H2 fix: sessions not calendar days ─────

def get_t1_barriers(
    close: pd.Series,
    t_events: pd.DatetimeIndex,
    num_sessions: int,
) -> pd.Series:
    """Time-barrier ts for each event = the session `num_sessions` bars
    forward in `close.index`.

    H2 FIX (AUDIT_v2): prior version used `close.index.searchsorted(
    t_events + pd.Timedelta(days=num_days))`, which counts CALENDAR days.
    For "swing" (5 days) that's roughly 3 trading sessions after weekends
    and holidays — a materially different time barrier than the 5-session
    window that paper_journal, purged_cv, and the drift governor all use.

    Now we step by POSITION in the price index — 5 sessions is always
    5 bars — so the label window matches the execution window.

    Returns a Series indexed by t_events. Rows whose t1 would land past
    the tail of `close.index` are NaT.
    """
    positions = close.index.get_indexer(t_events)
    # get_indexer returns -1 for events not in the index; treat as invalid.
    valid = positions >= 0
    t1_positions = positions + int(num_sessions)
    in_range = valid & (t1_positions < len(close.index))
    out = pd.Series(pd.NaT, index=t_events, dtype="datetime64[ns]")
    if in_range.any():
        out.loc[t_events[in_range]] = close.index[t1_positions[in_range]].values
    return out


# ── horizontal barriers (PT/SL) — H4 fix: high/low + pessimistic tie ───

def _apply_pt_sl_on_t1(
    bars: pd.DataFrame,
    events: pd.DataFrame,
    pt_sl: tuple[float, float],
    cost_frac: float = 0.0,
) -> pd.DataFrame:
    """For each event, find the first bar within [t, t1] whose HIGH crosses
    the profit-take barrier or LOW crosses the stop-loss barrier.

    H4 FIX (AUDIT_v2): prior version compared only `close` against the
    barriers, but paper_journal._resolve_barrier_daily uses high/low with
    pessimistic same-bar ties. Labels systematically hit barriers less
    often than production did, so the trained model's hit-rate was NOT
    what it was measured against. This function now uses high/low too,
    with the SAME pessimistic same-bar tie rule (SL wins ties) — labels
    now measure what execution actually delivers.

    Args:
        bars:   DataFrame indexed by date with columns `high`, `low`, `close`.
        events: DataFrame with columns t1 (time barrier), trgt (per-event σ),
                side (+1 long / -1 short).
        pt_sl:  (pt_mult, sl_mult).

    Returns:
        DataFrame indexed by event with (pt, sl) timestamp columns.
        NaT means that barrier was not hit before t1.
    """
    out = events[["t1"]].copy()
    pt_mult, sl_mult = pt_sl

    for loc, t1 in events["t1"].items():
        if pd.isna(t1):
            continue
        window = bars.loc[loc:t1]
        if window.empty:
            continue
        entry_close = float(bars.at[loc, "close"])
        if entry_close <= 0:
            continue
        side = float(events.at[loc, "side"])
        trgt = float(events.at[loc, "trgt"])

        # Direction-adjusted extreme returns per bar.
        # LONG  (side=+1): PT crossed if high/entry - 1 >= pt*trgt
        #                  SL crossed if low/entry  - 1 <= -sl*trgt
        # SHORT (side=-1): PT crossed if low/entry  - 1 <= -pt*trgt
        #                  SL crossed if high/entry - 1 >= sl*trgt
        high_ret = window["high"].astype(float) / entry_close - 1.0
        low_ret  = window["low"].astype(float)  / entry_close - 1.0

        # C3 opt-in: shift both barriers AWAY from entry by cost_frac.
        # cost_frac=0 preserves the previous gross behavior.
        if side >= 0:
            pt_bars = window.index[high_ret >= (pt_mult * trgt + cost_frac)] if pt_mult > 0 else pd.DatetimeIndex([])
            sl_bars = window.index[low_ret  <= -(sl_mult * trgt + cost_frac)] if sl_mult > 0 else pd.DatetimeIndex([])
        else:
            pt_bars = window.index[low_ret  <= -(pt_mult * trgt + cost_frac)] if pt_mult > 0 else pd.DatetimeIndex([])
            sl_bars = window.index[high_ret >= (sl_mult * trgt + cost_frac)] if sl_mult > 0 else pd.DatetimeIndex([])

        first_pt = pt_bars.min() if len(pt_bars) > 0 else pd.NaT
        first_sl = sl_bars.min() if len(sl_bars) > 0 else pd.NaT

        # PESSIMISTIC same-bar tie: if BOTH hit on the same bar, SL wins
        # (matches paper_journal._resolve_barrier_daily's tie rule).
        if pd.notna(first_pt) and pd.notna(first_sl) and first_sl <= first_pt:
            first_pt = pd.NaT
        # (If first_pt < first_sl strictly, PT wins — no adjustment needed.)

        out.at[loc, "pt"] = first_pt
        out.at[loc, "sl"] = first_sl

    return out


def get_events(
    bars: pd.DataFrame,
    t_events: pd.DatetimeIndex,
    pt_sl: tuple[float, float],
    target: pd.Series,
    t1: pd.Series,
    side: Optional[pd.Series] = None,
    min_ret: float = 0.0,
    cost_frac: float = 0.0,
) -> pd.DataFrame:
    """Triple-barrier events DataFrame. AFML Snippet 3.3.

    `bars` must have columns (high, low, close) indexed by date. H4 fix
    changed the signature — the old API took only `close: pd.Series`.
    """
    target = target.reindex(t_events)
    target = target[target > min_ret] if min_ret > 0 else target
    t_events = target.index

    t1 = t1.reindex(t_events)
    if side is None:
        side_ = pd.Series(1.0, index=t_events)
    else:
        side_ = side.reindex(t_events)

    events = pd.concat({"t1": t1, "trgt": target, "side": side_}, axis=1)
    events = events.dropna(subset=["trgt"])

    if events.empty:
        return events

    df0 = _apply_pt_sl_on_t1(bars=bars, events=events, pt_sl=pt_sl, cost_frac=cost_frac)
    # Earliest-hit barrier becomes the actual close time.
    events["t1"] = df0.dropna(how="all").min(axis=1)
    if side is None:
        events = events.drop(columns=["side"])
    return events


# ── bins: {+1, −1, 0} labels + realized returns ────────────────────

def get_bins(events: pd.DataFrame, close: pd.Series) -> pd.DataFrame:
    """Compute bin labels and realized returns. AFML Snippet 3.5."""
    if events.empty:
        return pd.DataFrame(columns=["ret", "bin"])
    px = events.index.union(events["t1"].dropna()).drop_duplicates()
    px = close.reindex(px).ffill()
    out = pd.DataFrame(index=events.index)
    out["ret"] = (px.loc[events["t1"].values].values
                  / px.loc[events.index].values) - 1.0
    if "side" in events.columns:
        out["ret"] *= events["side"].values
        out["bin"] = np.where(out["ret"] > 0, 1, 0)
    else:
        out["bin"] = np.sign(out["ret"]).astype(int)
    return out


# ── high-level orchestrator ────────────────────────────────────────

def apply_triple_barrier_labels(
    dataset: pd.DataFrame,
    ohlcv: pd.DataFrame,
    horizon: str = "swing",
    vol_span: int = 100,
    min_ret_sigma: float = 0.0,
    cost_bps: float = 0.0,
) -> pd.DataFrame:
    """Replace the `target` column of a (symbol, as_of_date) dataset with
    triple-barrier labels.

    Args:
        dataset: must have columns `symbol`, `as_of_date`, and `target`.
        ohlcv:   long-form OHLCV with columns symbol, date, HIGH, LOW, close.
                 (H4 fix — used to require only `close`; now needs high+low.)
        horizon: 'intraday' | 'swing' | 'long' — picks PT/SL/t1_sessions.
        vol_span: EWM span for the σ scaler.
        min_ret_sigma: drop events whose target σ is below this floor.

    Returns:
        DataFrame with target now in {-1, 0, +1} and target_ret carrying
        the realized return for diagnostics.
    """
    cfg = _HORIZON_BARRIERS.get(horizon)
    if cfg is None:
        raise ValueError(f"Unknown horizon '{horizon}' for triple-barrier")
    pt_sl = (float(cfg["pt_mult"]), float(cfg["sl_mult"]))
    t1_sessions = int(cfg["t1_sessions"])
    # C3 opt-in (Stage 2C.3): cost_bps > 0 shifts PT and SL barriers by
    # cost_frac so a +1 label means the trade cleared its target NET of
    # cost, and a -1 label means it lost more than cost. cost_bps=0 (the
    # default) preserves the previous gross-labeled behavior — the
    # promotion gate already uses dsr_net via _evaluate_oos_with_costs,
    # which is the load-bearing cost check. See AUDIT_v2 C3 + docs/RETRAIN_RUNBOOK.md.
    cost_frac = float(cost_bps) / 1e4 if cost_bps and cost_bps > 0 else 0.0

    dataset = dataset.copy()
    dataset["as_of_date"] = pd.to_datetime(dataset["as_of_date"])

    # Defensive: if the ohlcv frame doesn't carry high/low, degrade to
    # close-only behavior for those cols (high=low=close). Logs a warning.
    ohlcv = ohlcv.copy()
    for col in ("high", "low"):
        if col not in ohlcv.columns:
            log.warning(
                "apply_triple_barrier_labels: ohlcv missing '%s'; "
                "degrading to close-only detection for this bar dim "
                "(H4 fix is partial). Pass full OHLCV for correct labels.",
                col,
            )
            ohlcv[col] = ohlcv["close"]

    out_chunks: list[pd.DataFrame] = []
    for sym, g in dataset.groupby("symbol", sort=False):
        sym_px = ohlcv[ohlcv["symbol"] == sym].sort_values("date")
        if sym_px.empty:
            continue
        idx = pd.to_datetime(sym_px["date"].values)
        bars = pd.DataFrame({
            "high":  sym_px["high"].astype(float).values,
            "low":   sym_px["low"].astype(float).values,
            "close": sym_px["close"].astype(float).values,
        }, index=idx).sort_index()
        px = bars["close"]
        if len(px) < t1_sessions + 5:
            continue
        vol = get_daily_volatility(px, span=vol_span)
        t_events = pd.DatetimeIndex(g["as_of_date"].values)
        t_events = t_events[t_events.isin(px.index)]
        if len(t_events) == 0:
            continue
        t1 = get_t1_barriers(px, t_events, num_sessions=t1_sessions)
        events = get_events(
            bars=bars, t_events=t_events, pt_sl=pt_sl,
            target=vol, t1=t1, min_ret=min_ret_sigma,
            cost_frac=cost_frac,
        )
        if events.empty:
            continue
        bins = get_bins(events, close=px)
        labels = (
            bins.rename(columns={"ret": "target_ret", "bin": "tb_label"})
                .reset_index()
                .rename(columns={"index": "as_of_date"})
        )
        labels["as_of_date"] = pd.to_datetime(labels["as_of_date"])
        merged = g.merge(labels, on="as_of_date", how="inner")
        if "target" in merged.columns:
            merged = merged.drop(columns=["target"])
        merged = merged.rename(columns={"tb_label": "target"})
        out_chunks.append(merged)

    if not out_chunks:
        log.warning("Triple-barrier produced 0 labels — check ohlcv coverage.")
        return dataset.iloc[0:0]

    out = pd.concat(out_chunks, ignore_index=True)
    log.info(
        "Triple-barrier (%s, pt=%.1fσ, sl=%.1fσ, t1=%d sessions): "
        "labels +1=%d, 0=%d, -1=%d (was %d rows)",
        horizon, pt_sl[0], pt_sl[1], t1_sessions,
        int((out["target"] == 1).sum()),
        int((out["target"] == 0).sum()),
        int((out["target"] == -1).sum()),
        len(dataset),
    )
    return out
