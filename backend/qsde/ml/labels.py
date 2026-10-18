"""Spec-driven triple-barrier labels (`tb_nextopen_v1`).

Differences from the legacy `models/triple_barrier.py` labeler, which this
replaces for new work:

  * ENTRY is the open of t+1 (the decision is taken at the close of t). The old
    labeler entered at the close of t and let bar t itself count towards a
    barrier hit, i.e. it used information from before the entry.
  * Window = bars t+1 .. t+h on the MARKET session calendar, so a symbol that did
    not trade on a day cannot hit a barrier that day and a "5-day" label always
    spans 5 market sessions.
  * Barrier walk = `qsde.ml.barrier` (high/low, stop-first ties, gap-through fills
    at the open): the same code the paper journal and the Scorecard resolver use.
  * Exclusions: windows that touch an UNEXPLAINED corporate-action event (a demerger
    shows up as a fake stop hit) and feature dates shortly after one are flagged
    (and dropped by default).

Output carries the per-position daily path (`r1..rh`, gross, side-adjusted, 0 after
exit) so the harness can build overlapping-sleeve daily returns instead of
subsampling rebalances.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ml.barrier import walk_barriers_matrix
from qsde.ml.registry import HorizonSpec, get_horizon, promotion_cost_bps

log = logging.getLogger(__name__)

FEATURE_CONTAMINATION_SESSIONS = 60
MIN_VOL_PERIODS = 25


def _calendar(prices: pd.DataFrame) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(sorted(pd.to_datetime(prices["date"]).unique()))


def _wide(prices: pd.DataFrame, cal: pd.DatetimeIndex, col: str) -> pd.DataFrame:
    d = prices.copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.pivot(index="date", columns="symbol", values=col).reindex(cal)


def make_labels(prices: pd.DataFrame, events: pd.DataFrame, horizon: str | HorizonSpec,
                side: int = 1, cost_bps: Optional[float] = None,
                unexplained: Optional[pd.DataFrame] = None, drop_contaminated: bool = True,
                keep_paths: bool = True) -> pd.DataFrame:
    """prices: symbol,date,open,high,low,close (adjusted). events: symbol,date (decision at close of date).

    Returns one row per labelled event; events without a complete window or a valid
    next-open entry are not returned (they cannot be labelled yet).
    """
    spec = get_horizon(horizon) if isinstance(horizon, str) else horizon
    h = spec.label_horizon_days
    cost_bps = promotion_cost_bps(spec.legacy) if cost_bps is None else float(cost_bps)
    cost_frac = cost_bps / 1e4

    cal = _calendar(prices)
    pos_of = {d: i for i, d in enumerate(cal)}
    O, H, L, C = (_wide(prices, cal, c) for c in ("open", "high", "low", "close"))
    Cf = C.ffill()                                              # carry the last close across non-trading days
    ret = Cf.pct_change()
    sigma = ret.ewm(span=spec.barrier.vol_span, min_periods=MIN_VOL_PERIODS).std()

    ev = events[["symbol", "date"]].copy()
    ev["date"] = pd.to_datetime(ev["date"])
    ev["p"] = ev["date"].map(pos_of)
    ev = ev[ev["p"].notna() & ev["symbol"].isin(C.columns)].copy()
    ev["p"] = ev["p"].astype(int)
    ev = ev[ev["p"] + h < len(cal)]                             # the full window must exist

    out_frames = []
    ks = np.arange(1, h + 1)
    for sym, g in ev.groupby("symbol", sort=False):
        p = g["p"].to_numpy()
        win = p[:, None] + ks[None, :]                          # (n, h) calendar positions t+1..t+h
        Ow, Hw, Lw, Cw = (a[sym].to_numpy(float)[win] for a in (O, H, L, Cf))
        # real bars only: a missing day has NaN O/H/L (cannot hit); closes are carried forward
        Cw_true = C[sym].to_numpy(float)[win]
        entry = Ow[:, 0]
        sig = sigma[sym].to_numpy(float)[p]
        ok = np.isfinite(entry) & (entry > 0) & np.isfinite(sig) & (sig > 0)
        if not ok.any():
            continue
        p, win, Ow, Hw, Lw, Cw, Cw_true, entry, sig = (
            x[ok] for x in (p, win, Ow, Hw, Lw, Cw, Cw_true, entry, sig))
        sd = np.full(len(p), float(side))
        stop = entry * (1.0 - sd * spec.barrier.sl_mult * sig)
        target = entry * (1.0 + sd * spec.barrier.pt_mult * sig)
        status, exit_idx, exit_px, gap = walk_barriers_matrix(Ow, Hw, Lw, Cw, stop, target, sd)

        gross = sd * (exit_px / entry - 1.0)
        rows = np.arange(len(p))
        # per-position daily path (gross, side-adjusted); flat after exit
        paths = np.zeros((len(p), h))
        prev = entry.copy()
        for k in range(h):
            live = k <= exit_idx
            px_k = np.where(k == exit_idx, exit_px, Cw[:, k])
            r_k = sd * (px_k / prev - 1.0)
            paths[:, k] = np.where(live, r_k, 0.0)
            prev = np.where(live, Cw[:, k], prev)
        f = pd.DataFrame({
            "symbol": sym,
            "as_of_date": cal[p],
            "side": int(side),
            "sigma": sig,
            "entry_date": cal[p + 1],
            "entry": entry,
            "stop": stop,
            "target": target,
            "status": np.select([status == 1, status == -1], ["WIN", "LOSS"], default="TIME"),
            "label": status,
            "exit_session": exit_idx + 1,
            "exit_date": cal[win[rows, exit_idx]],
            "exit_price": exit_px,
            "gap_exit": gap,
            "ret_gross": gross,
            "ret_net": gross - cost_frac,
            "label_end_date": cal[win[rows, exit_idx]],
            "label_end_session": win[rows, exit_idx],
        })
        if keep_paths:
            for k in range(h):
                f[f"r{k + 1}"] = paths[:, k]
        # a TIME exit on a day the symbol did not trade uses a carried-forward close: flag it
        f["stale_exit"] = (status == 0) & ~np.isfinite(Cw_true[rows, exit_idx])
        out_frames.append(f)

    if not out_frames:
        return pd.DataFrame()
    lab = pd.concat(out_frames, ignore_index=True)
    lab["cost_bps"] = cost_bps
    lab["horizon_id"] = spec.id
    lab["label_id"] = spec.label

    lab = _flag_exclusions(lab, unexplained, cal, h)
    if drop_contaminated:
        n0 = len(lab)
        lab = lab[~(lab["touches_unexplained"] | lab["feature_contaminated"])].copy()
        if n0 - len(lab):
            log.info("labels: dropped %d of %d events touching an UNEXPLAINED corporate-action event",
                     n0 - len(lab), n0)
    return lab.sort_values(["as_of_date", "symbol"]).reset_index(drop=True)


def _flag_exclusions(lab: pd.DataFrame, unexplained: Optional[pd.DataFrame],
                     cal: pd.DatetimeIndex, h: int) -> pd.DataFrame:
    lab = lab.copy()
    lab["touches_unexplained"] = False
    lab["feature_contaminated"] = False
    if unexplained is None or len(unexplained) == 0:
        return lab
    ue = unexplained[["symbol", "ex_date"]].copy()
    ue["ex_date"] = pd.to_datetime(ue["ex_date"])
    pos = pd.Series(np.arange(len(cal)), index=cal)
    # snap each ex-date to the first calendar session on/after it
    ue["ep"] = pos.reindex(ue["ex_date"], method="bfill").to_numpy()
    ue = ue.dropna(subset=["ep"])
    ue["ep"] = ue["ep"].astype(int)
    by_sym = ue.groupby("symbol")["ep"].apply(np.array).to_dict()
    t_pos = pos.reindex(lab["as_of_date"]).to_numpy()
    e_pos = pos.reindex(lab["label_end_date"]).to_numpy()
    touch = np.zeros(len(lab), dtype=bool)
    contam = np.zeros(len(lab), dtype=bool)
    syms = lab["symbol"].to_numpy()
    for sym, eps in by_sym.items():
        m = syms == sym
        if not m.any():
            continue
        tp, ep_end = t_pos[m], e_pos[m]
        for e in eps:
            touch[m] |= (e >= tp + 1) & (e <= ep_end)          # event inside (t, label end]
            contam[m] |= (e <= tp) & (e > tp - FEATURE_CONTAMINATION_SESSIONS)
    lab["touches_unexplained"] = touch
    lab["feature_contaminated"] = contam
    return lab
