"""Synthetic worlds with a KNOWN answer, to prove the harness before it judges a real model.

  planted alpha -> must be detected      noise -> must be rejected
  future leak   -> must be caught        shuffled labels -> IC must collapse to ~0
"""
from __future__ import annotations

from typing import Callable, Optional

import numpy as np
import pandas as pd

from qsde.ml import cv as cvmod
from qsde.ml.eval import evaluate_scores
from qsde.ml.labels import make_labels
from qsde.ml.registry import get_horizon


def make_panel(n_sym: int = 60, n_days: int = 900, edge_bps: float = 0.0, seed: int = 0,
               start: str = "2020-01-06", vol: float = 0.015) -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    """Returns (prices, features, calendar). The hidden factor f (AR(1), cross-sectionally z-scored)
    drives next-day returns by `edge_bps` basis points per unit f; the feature x = f + noise sees it,
    `noise_*` features never do."""
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range(start, periods=n_days)
    f = np.zeros((n_days, n_sym))
    f[0] = rng.normal(size=n_sym)
    for t in range(1, n_days):
        f[t] = 0.9 * f[t - 1] + np.sqrt(1 - 0.81) * rng.normal(size=n_sym)
    f = (f - f.mean(1, keepdims=True)) / f.std(1, keepdims=True)
    eps = rng.normal(0, vol, (n_days, n_sym))
    ret = np.zeros((n_days, n_sym))
    ret[1:] = eps[1:] + (edge_bps / 1e4) * f[:-1]                  # f at t predicts return at t+1
    close = 100.0 * np.exp(np.cumsum(ret, axis=0))
    prev = np.vstack([close[:1], close[:-1]])
    opn = prev * (1 + rng.normal(0, 0.003, close.shape))
    hi = np.maximum(opn, close) * (1 + np.abs(rng.normal(0, 0.004, close.shape)))
    lo = np.minimum(opn, close) * (1 - np.abs(rng.normal(0, 0.004, close.shape)))
    syms = [f"S{i:03d}" for i in range(n_sym)]
    prices = pd.DataFrame({
        "symbol": np.tile(syms, n_days), "date": np.repeat(cal, n_sym),
        "open": opn.ravel(), "high": hi.ravel(), "low": lo.ravel(), "close": close.ravel()})
    feats = pd.DataFrame({
        "symbol": np.tile(syms, n_days), "as_of_date": np.repeat(cal, n_sym),
        "x_signal": (f + rng.normal(0, 0.7, f.shape)).ravel(),
        "noise_a": rng.normal(size=n_days * n_sym), "noise_b": rng.normal(size=n_days * n_sym)})
    return prices, feats, cal


def labelled(prices: pd.DataFrame, feats: pd.DataFrame, horizon: str = "swing_5d") -> pd.DataFrame:
    ev = feats[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"})
    lab = make_labels(prices, ev, horizon)
    return lab.merge(feats, on=["symbol", "as_of_date"], how="inner").reset_index(drop=True)


def ridge_fit_predict(alpha: float = 10.0) -> Callable:
    def fp(Xtr, ytr, wtr, Xte):
        Xm, Xs = Xtr.mean(0), Xtr.std(0) + 1e-9
        A = (Xtr - Xm) / Xs
        w = np.ones(len(A)) if wtr is None else wtr
        W = A.T * w
        beta = np.linalg.solve(W @ A + alpha * np.eye(A.shape[1]), W @ (ytr - np.average(ytr, weights=w)))
        return ((Xte - Xm) / Xs) @ beta
    return fp


def cpcv_scores(lab: pd.DataFrame, feature_cols: list[str], calendar: pd.DatetimeIndex,
                fit_predict: Optional[Callable] = None, n_groups: int = 6, k_test: int = 2,
                horizon: str = "swing_5d") -> tuple[list[pd.DataFrame], dict]:
    """Run a model through CPCV and return one scored label frame per backtest path."""
    spec = get_horizon(horizon)
    pos = pd.Series(np.arange(len(calendar)), index=pd.DatetimeIndex(calendar))
    t0 = pos.reindex(lab["as_of_date"]).to_numpy().astype(int)
    t1 = lab["label_end_session"].to_numpy().astype(int)
    w = cvmod.average_uniqueness(lab["symbol"].to_numpy(), t0, t1)
    out = cvmod.run_cpcv(lab[feature_cols].to_numpy(float), lab["ret_net"].to_numpy(float), t0, t1,
                         fit_predict or ridge_fit_predict(), weights=w, n_groups=n_groups, k_test=k_test,
                         embargo=spec.embargo_days)
    frames = []
    for s in out["paths"]:
        f = lab.copy()
        f["score"] = s
        frames.append(f)
    return frames, out


def evaluate_world(edge_bps: float, seed: int = 0, n_trials: int = 1, trial_sharpes=(),
                   n_sym: int = 60, n_days: int = 900, horizon: str = "swing_5d",
                   features: tuple = ("x_signal", "noise_a", "noise_b")) -> dict:
    prices, feats, cal = make_panel(n_sym, n_days, edge_bps, seed)
    lab = labelled(prices, feats, horizon)
    frames, _ = cpcv_scores(lab, list(features), cal, horizon=horizon)
    res = [evaluate_scores(f, cal, horizon, prices=prices, universe_version="synthetic-selftest",
                           n_trials=n_trials, trial_sharpes_daily=trial_sharpes, baseline_seeds=4)
           for f in frames[:3]]
    return {"results": res, "lab": lab, "prices": prices, "calendar": cal}
