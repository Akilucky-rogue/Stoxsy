"""Performance monitor and retrain trigger for the closed prediction loop. Pure functions over RESOLVED predictions only.

Four levels, as in the design: calibration (do stated probabilities match reality), ranking (does it select better names),
economics (net of cost), stability (drift). `retrain_decision` only ever recommends a CANDIDATE retrain: promotion is a separate,
human, gated decision, and a live result confirms an offline validation, it never replaces it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ml.eval import spearman_by_date


# ── calibration ──────────────────────────────────────────────────────

def brier(p, y) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2))


def log_loss(p, y, eps: float = 1e-6) -> float:
    p, y = np.clip(np.asarray(p, float), eps, 1 - eps), np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def calibration_slope_intercept(p, y, iters: int = 50) -> tuple[float, float]:
    """Logistic recalibration y ~ sigmoid(a + b*logit(p)): b = 1, a = 0 is perfect; b < 1 means over-confident. (a, b) via Newton."""
    p = np.clip(np.asarray(p, float), 1e-6, 1 - 1e-6)
    y = np.asarray(y, float)
    x = np.log(p / (1 - p))
    X = np.column_stack([np.ones_like(x), x])
    def nll(b):
        z = X @ b
        return float(np.sum(np.logaddexp(0.0, z) - y * z))

    beta = np.array([0.0, 1.0])
    for _ in range(iters):
        mu = 1 / (1 + np.exp(-(X @ beta)))
        W = mu * (1 - mu) + 1e-9
        H = X.T @ (X * W[:, None]) + 1e-8 * np.eye(2)
        step = np.linalg.solve(H, X.T @ (y - mu))
        t, f0 = 1.0, nll(beta)
        while nll(beta + t * step) > f0 and t > 1e-6:          # damped Newton: never accept a worse likelihood
            t *= 0.5
        beta = beta + t * step
        if np.abs(t * step).max() < 1e-9:
            break
    return float(beta[1]), float(beta[0])          # slope, intercept


def reliability_table(p, y, bins: int = 10) -> pd.DataFrame:
    d = pd.DataFrame({"p": np.asarray(p, float), "y": np.asarray(y, float)})
    d["bin"] = pd.qcut(d["p"].rank(method="first"), bins, labels=False, duplicates="drop")
    return d.groupby("bin").agg(n=("y", "size"), mean_p=("p", "mean"), observed=("y", "mean")).reset_index()


# ── ranking ──────────────────────────────────────────────────────────

def rank_ic(df: pd.DataFrame, score: str = "score", ret: str = "ret_net", min_names: int = 10) -> pd.Series:
    return spearman_by_date(df.rename(columns={"as_of_date": "as_of_date"}), score, ret, min_names=min_names)


def precision_and_lift(df: pd.DataFrame, top_q: float = 0.2, min_names: int = 10) -> dict:
    """Share of the daily top-q (by score) that ended net-positive, against the same share for the whole eligible set."""
    hits, base, n = [], [], 0
    for _, g in df.groupby("as_of_date"):
        if len(g) < min_names:
            continue
        k = max(int(np.ceil(top_q * len(g))), 1)
        top = g.nlargest(k, "score")
        hits.append((top["ret_net"] > 0).mean())
        base.append((g["ret_net"] > 0).mean())
        n += 1
    if not n:
        return {"precision_at_q": np.nan, "base_rate": np.nan, "lift": np.nan, "n_dates": 0}
    p, b = float(np.mean(hits)), float(np.mean(base))
    return {"precision_at_q": p, "base_rate": b, "lift": p / b if b > 0 else np.nan, "n_dates": n}


# ── economics ────────────────────────────────────────────────────────

def economics(df: pd.DataFrame, top_q: float = 0.2, min_names: int = 10) -> dict:
    """Net result of taking the daily top-q by score, one equal-weight sleeve per date, measured on the realised net barrier
    returns. Dates are NOT independent (windows overlap), so Sharpe is deliberately not reported here: use the harness for that."""
    rows = []
    for d, g in df.groupby("as_of_date"):
        if len(g) < min_names:
            continue
        k = max(int(np.ceil(top_q * len(g))), 1)
        rows.append((d, g.nlargest(k, "score")["ret_net"].mean(), g["ret_net"].mean()))
    if not rows:
        return {"n_dates": 0}
    s = pd.DataFrame(rows, columns=["date", "top", "universe"]).set_index("date").sort_index()
    cum = s["top"].cumsum()
    gains, losses = s["top"][s["top"] > 0].sum(), -s["top"][s["top"] < 0].sum()
    return {"n_dates": int(len(s)), "net_mean_top": float(s["top"].mean()), "net_mean_universe": float(s["universe"].mean()),
            "excess_mean": float((s["top"] - s["universe"]).mean()), "hit_rate": float((s["top"] > 0).mean()),
            "profit_factor": float(gains / losses) if losses > 0 else np.inf,
            "max_drawdown_cum": float((cum - cum.cummax()).min())}


# ── stability ────────────────────────────────────────────────────────

def psi(expected, actual, bins: int = 10) -> float:
    """Population stability index of `actual` against the `expected` (reference) distribution, quantile bins from the reference."""
    e, a = np.asarray(expected, float), np.asarray(actual, float)
    e, a = e[np.isfinite(e)], a[np.isfinite(a)]
    if len(e) < bins * 5 or len(a) < bins * 5:
        return float("nan")
    edges = np.unique(np.quantile(e, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return 0.0
    edges[0], edges[-1] = -np.inf, np.inf
    pe = np.histogram(e, edges)[0] / len(e)
    pa = np.histogram(a, edges)[0] / len(a)
    pe, pa = np.clip(pe, 1e-4, None), np.clip(pa, 1e-4, None)
    return float(np.sum((pa - pe) * np.log(pa / pe)))


def feature_drift(ref: pd.DataFrame, cur: pd.DataFrame, cols: list, threshold: float = 0.25) -> dict:
    vals = {c: psi(ref[c], cur[c]) for c in cols if c in ref and c in cur}
    bad = {c: v for c, v in vals.items() if np.isfinite(v) and v > threshold}
    return {"n_features": len(vals), "n_above": len(bad), "worst": dict(sorted(bad.items(), key=lambda kv: -kv[1])[:10]),
            "max_psi": float(np.nanmax(list(vals.values()))) if vals else float("nan")}


# ── the report ───────────────────────────────────────────────────────

def health_report(joined: pd.DataFrame, reference_ic: Optional[float] = None, rolling_dates: int = 60,
                  top_q: float = 0.2, drift: Optional[dict] = None) -> dict:
    """joined = predictions + outcomes (resolved only; `excluded` rows are dropped)."""
    d = joined[~joined["excluded"].fillna(False)] if "excluded" in joined else joined
    rep: dict = {"n_resolved": int(len(d)), "n_dates": int(d["as_of_date"].nunique()) if len(d) else 0}
    if len(d) == 0:
        return rep
    ic = rank_ic(d)
    rep["ranking"] = {"ic_mean": float(ic.mean()) if len(ic) else np.nan, "ic_hit": float((ic > 0).mean()) if len(ic) else np.nan,
                      "ic_rolling": float(ic.tail(rolling_dates).mean()) if len(ic) else np.nan,
                      "reference_ic": reference_ic, **precision_and_lift(d, top_q)}
    rep["economics"] = economics(d, top_q)
    if "p_win" in d and d["p_win"].notna().sum() >= 50:
        c = d.dropna(subset=["p_win"])
        y = (c["ret_net"] > 0).astype(float)
        slope, icpt = calibration_slope_intercept(c["p_win"], y)
        rep["calibration"] = {"n": int(len(c)), "brier": brier(c["p_win"], y), "brier_base": brier(np.full(len(c), y.mean()), y),
                              "log_loss": log_loss(c["p_win"], y), "slope": slope, "intercept": icpt}
    if "regime" in d and d["regime"].notna().any():
        rep["by_regime"] = {str(r): {"n": int(len(g)), "ic": float(rank_ic(g).mean()) if g["as_of_date"].nunique() > 5 else np.nan,
                                     "net_mean_top": economics(g, top_q).get("net_mean_top", np.nan)}
                            for r, g in d.groupby("regime")}
    if drift is not None:
        rep["drift"] = drift
    return rep


# ── retrain trigger and kill switch ──────────────────────────────────

@dataclass(frozen=True)
class Thresholds:
    min_resolved: int = 200                      # below this the live evidence is too thin to trigger on
    min_dates: int = 40
    max_features_drifting: int = 5               # features with PSI > psi_feature
    psi_feature: float = 0.25
    slope_low: float = 0.7
    slope_high: float = 1.3
    ic_collapse_ratio: float = 0.3               # rolling IC below this share of the reference IC
    underperform_dates: int = 60
    scheduled_days: dict = field(default_factory=lambda: {"intraday_1d": 7, "swing_5d": 14, "swing_10d": 30, "position_20d": 30})
    # kill switch
    kill_slope_low: float = 0.3
    kill_drawdown_cum: float = -0.5              # cumulative top-q net return drawdown (sum of per-date means)
    kill_max_psi: float = 1.0


def retrain_decision(report: dict, horizon_id: str, now: pd.Timestamp, last_retrain: Optional[pd.Timestamp] = None,
                     new_confirmed_ca: int = 0, universe_changed: bool = False, th: Thresholds = Thresholds()) -> dict:
    """What kind of CANDIDATE retrain (if any) the evidence justifies. It never promotes and never retrains on unresolved data."""
    reasons: list = []
    kinds: set = set()
    days = th.scheduled_days.get(horizon_id, 30)
    if last_retrain is None or (pd.Timestamp(now) - pd.Timestamp(last_retrain)).days >= days:
        kinds.add("scheduled")
        reasons.append(f"scheduled: >= {days} days since the last candidate")
    if new_confirmed_ca > 0:
        kinds.add("data")
        reasons.append(f"data: {new_confirmed_ca} newly confirmed corporate actions")
    if universe_changed:
        kinds.add("data")
        reasons.append("data: the point-in-time universe definition changed")
    thin = report.get("n_resolved", 0) < th.min_resolved or report.get("n_dates", 0) < th.min_dates
    blocked = None
    if thin:
        blocked = f"live drift triggers suppressed: {report.get('n_resolved', 0)} resolved over {report.get('n_dates', 0)} dates (< {th.min_resolved}/{th.min_dates})"
    else:
        r = report.get("ranking", {})
        if report.get("drift", {}).get("n_above", 0) > th.max_features_drifting:
            kinds.add("drift")
            reasons.append(f"drift: {report['drift']['n_above']} features with PSI > {th.psi_feature}")
        cal = report.get("calibration")
        if cal and not (th.slope_low <= cal["slope"] <= th.slope_high):
            kinds.add("drift")
            reasons.append(f"calibration slope {cal['slope']:.2f} outside [{th.slope_low}, {th.slope_high}]")
        ref = r.get("reference_ic")
        if ref and np.isfinite(r.get("ic_rolling", np.nan)) and r["ic_rolling"] < th.ic_collapse_ratio * ref:
            kinds.add("drift")
            reasons.append(f"rolling IC {r['ic_rolling']:.3f} < {th.ic_collapse_ratio:.0%} of reference {ref:.3f}")
        e = report.get("economics", {})
        if e.get("n_dates", 0) >= th.underperform_dates and e.get("excess_mean", 0) < 0:
            kinds.add("drift")
            reasons.append(f"top-q underperforms the eligible universe (excess {e['excess_mean']:+.4f}/date over {e['n_dates']} dates)")
    return {"trigger": bool(kinds), "kinds": sorted(kinds), "reasons": reasons, "suppressed": blocked,
            "action": "train a CANDIDATE, evaluate under the same harness, owner approval required for promotion" if kinds else "none"}


def kill_switch(report: dict, data_quality_ok: bool = True, th: Thresholds = Thresholds()) -> list:
    """Reasons to halt this model's live signals (empty = keep running). Halting is always safe; resuming needs the owner."""
    out = []
    if not data_quality_ok:
        out.append("data-quality check failed")
    cal = report.get("calibration")
    if cal and cal["slope"] < th.kill_slope_low:
        out.append(f"calibration slope {cal['slope']:.2f} < {th.kill_slope_low}")
    e = report.get("economics", {})
    if e.get("n_dates", 0) >= th.min_dates and e.get("max_drawdown_cum", 0) < th.kill_drawdown_cum:
        out.append(f"cumulative drawdown {e['max_drawdown_cum']:.2f} below {th.kill_drawdown_cum}")
    d = report.get("drift", {})
    if np.isfinite(d.get("max_psi", np.nan)) and d["max_psi"] > th.kill_max_psi:
        out.append(f"feature PSI {d['max_psi']:.2f} > {th.kill_max_psi}")
    return out
