"""Evaluation harness: what a model's scores would have earned, net of cost, honestly.

Design rules (ADR 0001):
  * Overlapping-sleeve DAILY returns: each entry date deploys 1/h of the book into the
    top-q names; a position's daily path comes from the real barrier walk. No
    subsampling of rebalances (which left the old outputs with 122 / 30 periods).
  * Costs are the registry's: reported at every COST_STRESS level, never at one number.
  * DSR uses the number of trials (and the spread of their Sharpes) from the
    append-only ledger, failed trials included.
  * Baselines (random, equal-weight, 12-1 momentum) are run through the SAME machinery.
  * Regimes are sliced by the universe's own trend / volatility (no index history yet).
  * Every result carries an `evidence_class`. Anything built on a survivor-selected or
    narrow universe is `PLUMBING_ONLY` and must never receive a verdict.
"""
from __future__ import annotations

import logging
from itertools import combinations
from typing import Optional, Sequence

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

from qsde.ml.registry import COST_STRESS_BPS, get_horizon, to_manifest

log = logging.getLogger(__name__)
ANN = 252.0
EULER_GAMMA = 0.5772156649015329


# ── basic statistics ─────────────────────────────────────────────────

def perf(r: pd.Series) -> dict:
    r = pd.Series(r).dropna()
    n = len(r)
    if n < 2 or r.std() == 0:
        return {"n_days": n, "sharpe_daily": 0.0, "sharpe_ann": 0.0, "mean_ann": float(r.mean() * ANN) if n else 0.0,
                "vol_ann": 0.0, "skew": 0.0, "kurt": 3.0, "max_dd": 0.0, "hit": 0.0}
    sd = float(r.std(ddof=1))
    sh = float(r.mean() / sd)
    cum = r.cumsum()
    return {"n_days": n, "sharpe_daily": sh, "sharpe_ann": sh * np.sqrt(ANN), "mean_ann": float(r.mean() * ANN),
            "vol_ann": sd * np.sqrt(ANN), "skew": float(skew(r)), "kurt": float(kurtosis(r, fisher=False)),
            "max_dd": float((cum - cum.cummax()).min()), "hit": float((r > 0).mean())}


def newey_west_tstat(x: pd.Series, lag: int) -> float:
    x = pd.Series(x).dropna().to_numpy(float)
    n = len(x)
    if n < 3:
        return 0.0
    mu = x.mean()
    e = x - mu
    lag = max(0, min(lag, n - 2))
    var = float(np.dot(e, e) / n)
    for L in range(1, lag + 1):
        w = 1.0 - L / (lag + 1.0)
        var += 2.0 * w * float(np.dot(e[L:], e[:-L]) / n)
    se = np.sqrt(max(var, 1e-18) / n)
    return float(mu / se)


def spearman_by_date(df: pd.DataFrame, score: str = "score", ret: str = "ret_net",
                     date: str = "as_of_date", min_names: int = 10) -> pd.Series:
    def f(g: pd.DataFrame) -> float:
        if len(g) < min_names or g[score].nunique() < 2 or g[ret].nunique() < 2:
            return np.nan
        return g[score].rank().corr(g[ret].rank())
    return df.groupby(date).apply(f, include_groups=False).dropna()


def ic_summary(ic: pd.Series, h: int) -> dict:
    ic = pd.Series(ic).dropna()
    if len(ic) < 3:
        return {"n_dates": len(ic), "mean": 0.0, "std": 0.0, "icir": 0.0, "t_nw": 0.0, "hit": 0.0}
    return {"n_dates": int(len(ic)), "mean": float(ic.mean()), "std": float(ic.std(ddof=1)),
            "icir": float(ic.mean() / ic.std(ddof=1)), "t_nw": newey_west_tstat(ic, lag=h),
            "hit": float((ic > 0).mean())}


# ── portfolio construction: overlapping sleeves ──────────────────────

def _rcols(h: int) -> list[str]:
    return [f"r{k}" for k in range(1, h + 1)]


def select_top(lab: pd.DataFrame, top_q: float, min_names: int, score: str = "score") -> pd.DataFrame:
    g = lab.groupby("as_of_date")[score]
    n = g.transform("size")
    rk = g.rank(method="first", ascending=False)
    return lab[(n >= min_names) & (rk <= np.maximum(1, np.round(top_q * n)))]


def cohort_net(sel: pd.DataFrame, h: int, cost_bps: float) -> pd.DataFrame:
    """Per entry date: mean path of the selected names, net of round-trip cost split half on day 1
    and half on the position's exit session. Index = entry date, columns r1..rh."""
    rc = _rcols(h)
    gross = sel.groupby("as_of_date")[rc].mean()
    c = cost_bps / 1e4
    cost = pd.DataFrame(0.0, index=gross.index, columns=rc)
    cost["r1"] += c / 2.0
    for k in range(1, h + 1):
        cost[f"r{k}"] += (c / 2.0) * sel["exit_session"].eq(k).groupby(sel["as_of_date"]).mean().reindex(gross.index).fillna(0.0)
    return gross - cost


def sleeve_returns(cohorts: pd.DataFrame, calendar: pd.DatetimeIndex, h: int) -> pd.Series:
    """1/h of the book per entry date. Day k of the cohort that entered at session p earns on session p+k."""
    cal = pd.DatetimeIndex(calendar)
    pos = cal.get_indexer(cohorts.index)
    if (pos < 0).any():
        raise ValueError("cohort dates missing from the calendar")
    R = np.zeros(len(cal) + h + 1)
    for k in range(1, h + 1):
        np.add.at(R, pos + k, cohorts[f"r{k}"].to_numpy() / h)
    s = pd.Series(R[:len(cal)], index=cal)
    # drop the burn-in (book not yet fully loaded) and the unloaded tail
    return s.iloc[int(pos.min()) + h: int(pos.max()) + 1]


def run_portfolio(lab: pd.DataFrame, calendar: pd.DatetimeIndex, h: int, top_q: float,
                  min_names: int, cost_bps: float, score: str = "score") -> pd.Series:
    sel = select_top(lab.dropna(subset=[score]), top_q, min_names, score)
    if sel.empty:
        return pd.Series(dtype=float)
    return sleeve_returns(cohort_net(sel, h, cost_bps), calendar, h)


# ── deflated Sharpe with ledger trials, PBO, bootstrap ───────────────

def dsr_from_trials(sr_daily: float, n_obs: int, skew_: float, kurt_: float,
                    trial_sr_daily: Sequence[float], n_trials: int) -> dict:
    """Bailey-Lopez de Prado Deflated Sharpe.

    SR0 = sqrt(Var[SR across trials]) * ((1-g) Phi^-1(1-1/N) + g Phi^-1(1-1/(N e))).
    With fewer than 2 finite trial Sharpes the cross-trial variance is unknown, so we fall back to
    the sampling variance of one Sharpe estimate (1/(T-1)) - which is an UNDER-estimate of the
    selection bias and is flagged.
    """
    N = max(int(n_trials), 1)
    ts = np.asarray([x for x in trial_sr_daily if np.isfinite(x)], dtype=float)
    if len(ts) >= 2 and ts.std(ddof=1) > 0:
        v, src = float(ts.var(ddof=1)), "ledger_cross_trial_variance"
    else:
        v, src = 1.0 / max(n_obs - 1, 1), "single_estimate_fallback"
    if N <= 1:
        sr0 = 0.0
    else:
        sr0 = np.sqrt(v) * ((1 - EULER_GAMMA) * norm.ppf(1 - 1.0 / N) + EULER_GAMMA * norm.ppf(1 - 1.0 / (N * np.e)))
    denom = np.sqrt(max(1e-12, 1.0 - skew_ * sr_daily + (kurt_ - 1.0) / 4.0 * sr_daily ** 2))
    z = (sr_daily - sr0) * np.sqrt(max(n_obs - 1, 1)) / denom
    return {"dsr": float(norm.cdf(z)), "sr0_daily": float(sr0), "n_trials": N, "variance_source": src,
            "psr_vs_zero": float(norm.cdf(sr_daily * np.sqrt(max(n_obs - 1, 1)) / denom))}


def pbo_cscv(returns: pd.DataFrame, n_slices: int = 16) -> dict:
    """Probability of Backtest Overfitting (Bailey et al.) by combinatorially symmetric CV.

    returns: T x N daily net returns of N candidate strategies (trials). PBO is the share of
    IS/OOS splits in which the strategy that was best in-sample ranks at or below the median OOS.
    ~0.5 means selection among these candidates is no better than chance.
    """
    M = returns.dropna(how="any")
    T, N = M.shape
    if N < 2 or T < n_slices * 2:
        return {"pbo": float("nan"), "n_strategies": N, "n_combos": 0, "reason": "needs >=2 strategies and enough days"}
    S = n_slices - (n_slices % 2)
    slices = np.array_split(np.arange(T), S)
    arr = M.to_numpy()
    logits = []
    for is_ids in combinations(range(S), S // 2):
        is_idx = np.concatenate([slices[i] for i in is_ids])
        oos_idx = np.concatenate([slices[i] for i in range(S) if i not in is_ids])
        a, b = arr[is_idx], arr[oos_idx]
        sr_is = a.mean(0) / np.where(a.std(0, ddof=1) > 0, a.std(0, ddof=1), np.nan)
        sr_oos = b.mean(0) / np.where(b.std(0, ddof=1) > 0, b.std(0, ddof=1), np.nan)
        if np.isnan(sr_is).all() or np.isnan(sr_oos).all():
            continue
        best = int(np.nanargmax(sr_is))
        rank = (np.nan_to_num(sr_oos, nan=-np.inf) < sr_oos[best]).sum() + 1      # 1 = worst
        w = rank / (N + 1.0)
        logits.append(np.log(w / (1.0 - w)))
    logits = np.asarray(logits)
    return {"pbo": float((logits <= 0).mean()), "n_strategies": N, "n_combos": int(len(logits))}


def block_bootstrap(r: pd.Series, block: int, stat, n_boot: int = 500, seed: int = 0) -> np.ndarray:
    x = pd.Series(r).dropna().to_numpy(float)
    n = len(x)
    if n < 2 * block:
        return np.array([])
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(n / block))
    out = np.empty(n_boot)
    for i in range(n_boot):
        starts = rng.integers(0, n - block + 1, nb)
        samp = np.concatenate([x[s:s + block] for s in starts])[:n]
        out[i] = stat(samp)
    return out


def _sharpe_ann(x: np.ndarray) -> float:
    sd = x.std(ddof=1)
    return float(x.mean() / sd * np.sqrt(ANN)) if sd > 0 else 0.0


# ── baselines & regimes ──────────────────────────────────────────────

def momentum_12_1(prices: pd.DataFrame) -> pd.DataFrame:
    """Classic 12-1 momentum score from (adjusted) closes: close[t-21] / close[t-252] - 1."""
    c = prices.pivot(index="date", columns="symbol", values="close").sort_index()
    sc = (c.shift(21) / c.shift(252) - 1.0).stack().rename("score").reset_index()
    sc.columns = ["as_of_date", "symbol", "score"]
    sc["as_of_date"] = pd.to_datetime(sc["as_of_date"])
    return sc


def universe_daily_return(prices: pd.DataFrame) -> pd.Series:
    c = prices.pivot(index="date", columns="symbol", values="close").sort_index()
    c.index = pd.to_datetime(c.index)
    return c.pct_change().mean(axis=1)


def regime_slices(daily: pd.Series, universe_ret: pd.Series) -> dict:
    """Sharpe by universe trend (63-session return sign) and volatility tercile (21-session std)."""
    u = universe_ret.reindex(daily.index).ffill()
    trend = (1 + universe_ret).rolling(63).apply(np.prod, raw=True).sub(1).reindex(daily.index) > 0
    vol = universe_ret.rolling(21).std().reindex(daily.index)
    terc = pd.qcut(vol, 3, labels=["low", "mid", "high"], duplicates="drop")
    out = {}
    for name, mask in {"trend_up": trend, "trend_down": ~trend, **{f"vol_{t}": terc == t for t in ("low", "mid", "high")}}.items():
        r = daily[mask.fillna(False)]
        p = perf(r)
        out[name] = {"n_days": p["n_days"], "sharpe_ann": p["sharpe_ann"], "mean_ann": p["mean_ann"]}
    return out


# ── evidence stamp ───────────────────────────────────────────────────

MIN_EVIDENCE_NAMES = 30
MIN_EVIDENCE_DAYS = 750


def evidence_class(universe_version: str, median_names: float, n_days: int) -> tuple[str, list[str]]:
    why = []
    uv = str(universe_version).upper()
    if "PILOT" in uv or "SURVIVOR" in uv:
        why.append("universe is survivor-selected (pilot)")
    if "SYNTHETIC" in uv:
        why.append("synthetic data: a self-test world, never evidence about a market")
    if median_names < MIN_EVIDENCE_NAMES:
        why.append(f"median names per date {median_names:.0f} < {MIN_EVIDENCE_NAMES}")
    if n_days < MIN_EVIDENCE_DAYS:
        why.append(f"{n_days} daily observations < {MIN_EVIDENCE_DAYS}")
    return ("PLUMBING_ONLY" if why else "EVIDENCE_CANDIDATE"), why


# ── leakage guard ────────────────────────────────────────────────────

def leak_check(X: pd.DataFrame, lab: pd.DataFrame, ret: str = "ret_net", ic_limit: float = 0.30,
               min_names: int = 10) -> list[dict]:
    """Flag any single feature whose |mean per-date IC| against the label return is implausibly high.

    X must align row-for-row with lab. A real daily-bar alpha does not exceed |IC| ~ 0.1;
    a feature at 0.3+ almost always encodes the future.
    """
    ic = ic_matrix_by_date(X.reset_index(drop=True), lab[ret].reset_index(drop=True),
                           lab["as_of_date"].reset_index(drop=True), min_names)
    mean_ic = ic.mean()
    return [{"feature": c, "mean_ic": float(mean_ic[c])} for c in X.columns
            if np.isfinite(mean_ic[c]) and abs(mean_ic[c]) > ic_limit]


def ic_matrix_by_date(X: pd.DataFrame, y: pd.Series, dates: pd.Series, min_names: int = 10) -> pd.DataFrame:
    """Per-date rank correlation of EVERY column of X with y, fully vectorised (date x feature).

    Ranks are taken within each date; y's rank is over all rows of that date (rows where a feature is NaN
    only drop out of that feature's sums). Used by the leakage guard, where exactness is not needed.
    """
    rx = X.groupby(dates).rank()
    ry = y.groupby(dates).rank()
    m = rx.notna().astype(float)
    rx0 = rx.fillna(0.0)
    ryv = ry.to_numpy()[:, None]
    ym = m.to_numpy() * ryv
    parts = {
        "n": m, "sx": rx0, "sxx": rx0 ** 2,
        "sy": pd.DataFrame(ym, index=X.index, columns=X.columns),
        "syy": pd.DataFrame(ym * ryv, index=X.index, columns=X.columns),
        "sxy": pd.DataFrame(rx0.to_numpy() * ryv, index=X.index, columns=X.columns),
    }
    s = {k: v.groupby(dates).sum() for k, v in parts.items()}
    num = s["n"] * s["sxy"] - s["sx"] * s["sy"]
    den = np.sqrt((s["n"] * s["sxx"] - s["sx"] ** 2) * (s["n"] * s["syy"] - s["sy"] ** 2))
    ic = (num / den.replace(0, np.nan))
    return ic.where(s["n"] >= min_names)


# ── the report ───────────────────────────────────────────────────────

def evaluate_scores(lab: pd.DataFrame, calendar: pd.DatetimeIndex, horizon: str = "swing_5d",
                    score: str = "score", top_q: float = 0.2, min_names: int = 10,
                    cost_levels: Sequence[float] = COST_STRESS_BPS, prices: Optional[pd.DataFrame] = None,
                    universe_version: str = "unknown", n_trials: int = 1,
                    trial_sharpes_daily: Sequence[float] = (), baseline_seeds: int = 10, seed: int = 0) -> dict:
    """lab: output of make_labels + a `score` column (out-of-sample). calendar: market sessions."""
    spec = get_horizon(horizon)
    h = spec.label_horizon_days
    d = lab.dropna(subset=[score]).copy()
    if d.empty:
        raise ValueError("no scored rows")
    names_per_date = d.groupby("as_of_date").size()
    ic = spearman_by_date(d, score, "ret_net", min_names=min_names)

    headline_cost = float(d["cost_bps"].iloc[0]) if "cost_bps" in d else cost_levels[-1]
    stress = {}
    series_by_cost = {}
    for c in sorted(set(list(cost_levels) + [headline_cost])):
        s = run_portfolio(d, calendar, h, top_q, min_names, c, score)
        series_by_cost[c] = s
        stress[f"{c:g}"] = perf(s)
    head = series_by_cost[headline_cost]
    hp = perf(head)

    boot = block_bootstrap(head, block=max(h, 5), stat=_sharpe_ann, seed=seed)
    ci = ({"sharpe_ann_lo95": float(np.percentile(boot, 2.5)), "sharpe_ann_hi95": float(np.percentile(boot, 97.5))}
          if len(boot) else {})
    dsr = dsr_from_trials(hp["sharpe_daily"], hp["n_days"], hp["skew"], hp["kurt"], trial_sharpes_daily, n_trials)

    base = {}
    rng = np.random.default_rng(seed)
    rs = []
    for _ in range(baseline_seeds):
        dd = d[["as_of_date", "symbol", "ret_net", "exit_session", *_rcols(h)]].copy()
        dd["score"] = rng.random(len(dd))
        rs.append(perf(run_portfolio(dd, calendar, h, top_q, min_names, headline_cost))["sharpe_ann"])
    base["random_top_q"] = {"sharpe_ann_mean": float(np.mean(rs)), "sharpe_ann_std": float(np.std(rs)), "n": baseline_seeds}
    ew = d.assign(score=1.0)
    base["equal_weight_universe"] = perf(run_portfolio(ew, calendar, h, 1.0, min_names, headline_cost, "score"))
    if prices is not None:
        mom = momentum_12_1(prices)
        dm = d.drop(columns=[score]).merge(mom, on=["as_of_date", "symbol"], how="inner")
        if len(dm):
            base["momentum_12_1_top_q"] = perf(run_portfolio(dm, calendar, h, top_q, min_names, headline_cost))

    if prices is not None and len(head):
        # Context the sleeves cannot give: what simply holding the universe (no trading, no cost) did.
        base["universe_buy_and_hold_gross"] = perf(universe_daily_return(prices).reindex(head.index))
    regimes = regime_slices(head, universe_daily_return(prices)) if prices is not None and len(head) > 100 else {}
    ev_class, why = evidence_class(universe_version, float(names_per_date.median()), hp["n_days"])
    flags = []
    if dsr["variance_source"] == "single_estimate_fallback" and n_trials > 1:
        flags.append("DSR used the single-estimate variance fallback (no trial Sharpes in the ledger): optimistic")
    return {
        "evidence_class": ev_class, "evidence_reasons": why,
        "verdict_allowed": ev_class == "EVIDENCE_CANDIDATE",
        "horizon": spec.id, "cost_bps_headline": headline_cost, "top_q": top_q,
        "n_samples": int(len(d)), "n_dates": int(names_per_date.size),
        "median_names_per_date": float(names_per_date.median()),
        "ic": ic_summary(ic, h), "headline": hp, "sharpe_ci": ci, "dsr": dsr,
        "cost_stress": stress, "baselines": base, "regimes": regimes, "flags": flags,
        "registry": to_manifest()["cost_stress_bps"],
    }
