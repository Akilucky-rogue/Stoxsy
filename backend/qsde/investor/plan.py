"""Investor engine (pure): profile -> target mix -> tolerance gate -> glide path -> drift and rebalance action -> reproducible goal projection and trade-offs.

Principles (owner design reviews, 2026-10-23): the risk profile is a CONSTRAINT (the lower of capacity and tolerance), never raised to rescue an off-track goal; targets are starting defaults
gated by tolerance, emergency fund and goal date; "no action needed" is a normal answer; projections are scenarios (a range with its sample length), never a point forecast; sales are
only proposed when the data is complete enough and the trade is worth its cost. No function here reads the database.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from qsde.modes import policy as pol

CLASSES = ("equity", "gold", "debt")


# ── profile ──────────────────────────────────────────────────────────

def order(policy: Optional[dict] = None) -> list:
    return list((policy or pol.load_policy())["investor"]["capacity_rules"]["order"])


def capacity_profile(years_to_goal: float, income_stability: str, dependents: int, policy: Optional[dict] = None) -> tuple[str, list]:
    p = policy or pol.load_policy()
    rules, o = p["investor"]["capacity_rules"], order(p)
    reasons: list = []
    idx = 0
    for yrs, name in rules["horizon_years_to_max_profile"]:
        if years_to_goal >= yrs:
            idx = o.index(name)
    reasons.append(f"{years_to_goal:.1f} years to the goal allows at most '{o[idx]}'")
    sd = rules["step_down"]
    if income_stability == "uncertain" and sd["income_uncertain"]:
        idx = max(0, idx - sd["income_uncertain"])
        reasons.append("uncertain income steps the profile down one notch")
    if dependents >= 3 and sd["dependents_3_plus"]:
        idx = max(0, idx - sd["dependents_3_plus"])
        reasons.append("three or more dependents steps the profile down one notch")
    return o[idx], reasons


def tolerance_profile(reaction: str, policy: Optional[dict] = None) -> tuple[str, float]:
    opt = (policy or pol.load_policy())["investor"]["questionnaire"]["drawdown_reaction"]["options"]
    if reaction not in opt:
        raise ValueError(f"drawdown reaction must be one of {', '.join(opt)}")
    return opt[reaction]["profile_cap"], float(opt[reaction]["tolerated_fall"])


def lower_profile(a: str, b: str, policy: Optional[dict] = None) -> str:
    o = order(policy)
    return o[min(o.index(a), o.index(b))]


def target_mix(profile: str, policy: Optional[dict] = None) -> dict:
    m = (policy or pol.load_policy())["investor"]["risk_profiles"][profile]
    return {k: m[k] / 100.0 for k in CLASSES}


# ── history statistics (daily, monthly rebalanced) ───────────────────

def mix_returns(daily: pd.DataFrame, w: dict) -> pd.Series:
    """Daily portfolio returns of a fixed-weight mix rebalanced at each month start (weights drift inside the month)."""
    wv = np.array([w[c] for c in CLASSES], dtype=float)
    r = daily[list(CLASSES)].to_numpy()
    out = np.empty(len(daily))
    months = daily.index.to_period("M")
    start = 0
    for m in pd.unique(months):
        idx = np.flatnonzero(months == m)
        cum = np.cumprod(1.0 + r[idx], axis=0)                      # growth of 1 in each class from the month start
        wealth = cum @ wv
        prev = np.r_[1.0, wealth[:-1]]
        out[idx] = wealth / prev - 1.0
    return pd.Series(out, index=daily.index)


def mix_stats(daily: pd.DataFrame, w: dict) -> dict:
    r = mix_returns(daily, w)
    eq = (1.0 + r).cumprod()
    dd = eq / eq.cummax() - 1.0
    trough = dd.idxmin()
    peak = eq.loc[:trough].idxmax()
    rec = eq.loc[trough:]
    back = rec[rec >= eq.loc[peak]]
    yrs = (r.index[-1] - r.index[0]).days / 365.25
    y12 = eq.rolling(252).apply(lambda x: x[-1] / x[0] - 1.0, raw=True).dropna()
    return {"cagr": float(eq.iloc[-1] ** (1 / yrs) - 1), "vol": float(r.std() * math.sqrt(252)), "max_drawdown": float(dd.min()), "peak": str(peak.date()), "trough": str(trough.date()),
            "recovery_days": int((back.index[0] - trough).days) if len(back) else None, "worst_12m": float(y12.min()) if len(y12) else None,
            "start": str(r.index[0].date()), "end": str(r.index[-1].date()), "years": float(yrs)}


def tolerance_gate(daily: pd.DataFrame, mix: dict, tolerated_fall: float, step: float = 0.05) -> dict:
    """Lower equity (into debt, gold unchanged) until the mix's worst historical fall is within what the person said they can hold through."""
    cur = dict(mix)
    steps = []
    while True:
        st = mix_stats(daily, cur)
        steps.append({"equity": round(cur["equity"], 4), "max_drawdown": st["max_drawdown"]})
        if -st["max_drawdown"] <= tolerated_fall + 1e-12 or cur["equity"] <= 1e-9:
            break
        move = min(step, cur["equity"])
        cur = {"equity": cur["equity"] - move, "gold": cur["gold"], "debt": cur["debt"] + move}
    return {"mix": cur, "binding": len(steps) > 1, "steps": steps, "final_max_drawdown": steps[-1]["max_drawdown"], "tolerated_fall": tolerated_fall,
            "met": -steps[-1]["max_drawdown"] <= tolerated_fall + 1e-12}


# ── glide path ───────────────────────────────────────────────────────

def glide_equity_cut(years_left: float, policy: Optional[dict] = None, points_per_year: Optional[float] = None, last_years: Optional[float] = None) -> float:
    g = (policy or pol.load_policy())["investor"]["glide_path"]
    ppy = g["points_per_year"] if points_per_year is None else points_per_year
    ly = g["last_years"] if last_years is None else last_years
    return max(0.0, ppy * (ly - max(years_left, 0.0))) / 100.0


def apply_glide(mix: dict, years_left: float, policy: Optional[dict] = None, **kw) -> dict:
    cut = min(glide_equity_cut(years_left, policy, **kw), mix["equity"])
    return {"equity": mix["equity"] - cut, "gold": mix["gold"], "debt": mix["debt"] + cut}


# ── drift and rebalance ──────────────────────────────────────────────

def weights(values: dict) -> dict:
    tot = sum(values.get(c, 0.0) for c in CLASSES)
    return {c: (values.get(c, 0.0) / tot if tot > 0 else 0.0) for c in CLASSES}


def band_status(w: dict, target: dict, abs_pts: float = 5.0, rel: float = 0.2) -> list:
    """Per class: breach if |drift| >= abs_pts OR |drift| / target >= rel (a target of zero uses the absolute band only).
    For targets below abs_pts/rel (25% at the defaults) the relative band is the tighter one and fires first."""
    rows = []
    for c in CLASSES:
        d = (w[c] - target[c]) * 100.0
        by_abs = abs(d) >= abs_pts - 1e-9
        by_rel = target[c] > 0 and abs(d) / (target[c] * 100.0) >= rel - 1e-9
        rows.append({"class": c, "current": w[c], "target": target[c], "drift_points": d, "breach": bool(by_abs or by_rel), "rule": "absolute" if by_abs else ("relative" if by_rel else None)})
    return rows


def sip_split(values: dict, target: dict, sip: float) -> dict:
    """Send new money to the classes below target (water-filling toward the target weights); anything left over follows the target proportions."""
    tot = sum(values.get(c, 0.0) for c in CLASSES) + sip
    need = {c: max(0.0, target[c] * tot - values.get(c, 0.0)) for c in CLASSES}
    s = sum(need.values())
    if s <= 0 or sip <= 0:
        return {c: sip * target[c] for c in CLASSES}
    if s <= sip:
        left = sip - s
        return {c: need[c] + left * target[c] for c in CLASSES}
    return {c: sip * need[c] / s for c in CLASSES}


def rebalance_plan(values: dict, target: dict, sip: float, other_value: float = 0.0, today: Optional[date] = None, policy: Optional[dict] = None) -> dict:
    """values: classified holdings by class (INR). other_value: holdings that could not be classified (unknown ETFs). Returns the action and its reasons."""
    p = policy or pol.load_policy()
    rb, band = p["investor"]["rebalance_policy"], p["investor"]["rebalance_band"]
    today = today or date.today()
    classified = sum(values.get(c, 0.0) for c in CLASSES)
    out = {"classified_value": classified, "unclassified_value": other_value, "tax_note": rb["tax_note"], "band_rule": band["rule"]}
    if classified <= 0:
        return {**out, "action": "no_data", "headline": "No classified holdings yet: import holdings or add balances to see where you stand.", "bands": [], "sip_split": sip_split(values, target, sip) if sip > 0 else {},
                "trades": [], "next_review": str(today + pd.DateOffset(months=rb["review_months"]))[:10]}
    w = weights(values)
    bands = band_status(w, target, band["absolute_points"], band["relative_fraction"])
    out["bands"] = bands
    out["sip_split"] = sip_split(values, target, sip) if sip > 0 else {}
    unseen = other_value / (classified + other_value) if (classified + other_value) > 0 else 0.0
    out["unclassified_share"] = unseen
    breached = [b for b in bands if b["breach"]]
    if not breached:
        return {**out, "action": "no_action", "headline": "No action needed: every asset class is inside its band.", "trades": [],
                "next_review": str(today + pd.DateOffset(months=rb["review_months"]))[:10]}
    # can new money fix it? simulate monthly SIP
    v = dict(values)
    months_to_fix = None
    if sip > 0:
        for m in range(1, 61):
            add = sip_split(v, target, sip)
            v = {c: v.get(c, 0.0) + add[c] for c in CLASSES}
            if not any(b["breach"] for b in band_status(weights(v), target, band["absolute_points"], band["relative_fraction"])):
                months_to_fix = m
                break
    nxt = str(today + pd.DateOffset(months=1))[:10]
    if months_to_fix is not None and months_to_fix <= 12:
        return {**out, "action": "direct_sip", "months_to_fix": months_to_fix, "trades": [], "next_review": nxt,
                "headline": f"Outside a band, but new SIP money alone brings it back in about {months_to_fix} month(s): send each SIP to the under-weight class, no selling."}
    if unseen > rb["unseen_or_unclassified_share_blocks_sales"]:
        return {**out, "action": "data_incomplete", "trades": [], "next_review": nxt,
                "headline": f"Outside a band, but {unseen:.0%} of the holdings could not be classified, so no sale is proposed. Classify them or add balances first. Send new SIP to the under-weight class meanwhile."}
    total = classified
    trades = []
    for c in CLASSES:
        amt = (target[c] - w[c]) * total
        if abs(amt) >= rb["min_trade_value_inr"] and abs(amt) / total >= rb["min_trade_share_of_portfolio"]:
            trades.append({"class": c, "side": "buy" if amt > 0 else "sell", "amount": float(abs(amt))})
    if not trades:
        return {**out, "action": "no_action", "trades": [], "next_review": nxt,
                "headline": "Outside a band, but the trade needed is smaller than the minimum worth making (costs and tax would outweigh it). Direct new SIP to the under-weight class."}
    return {**out, "action": "rebalance", "trades": trades, "next_review": nxt,
            "headline": "Rebalance: " + "; ".join(f"{t['side']} about ₹{t['amount']:,.0f} of {t['class']}" for t in trades) + ". Selling may trigger capital-gains tax this plan cannot estimate (tax lots are not loaded)."}


# ── projection ───────────────────────────────────────────────────────

def weight_schedule(mix: dict, months: int, years_left_at_start: float, glide: bool, policy: Optional[dict] = None) -> np.ndarray:
    """(months x 3) weights per month; with `glide` the equity share falls along the glide path as the goal date nears."""
    rows = []
    for t in range(months):
        m = apply_glide(mix, years_left_at_start - t / 12.0, policy) if glide else mix
        rows.append([m[c] for c in CLASSES])
    return np.array(rows)


def block_indices(rng: np.random.Generator, n_hist: int, months: int, block: int, paths: int) -> np.ndarray:
    nb = math.ceil(months / block)
    starts = rng.integers(0, n_hist - block + 1, size=(paths, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(paths, nb * block)
    return idx[:, :months]


def simulate(hist_monthly: pd.DataFrame, sched: np.ndarray, start_value: float, monthly_contribution: float, paths: int, block: int, seed: int, haircut_per_year: float = 0.0) -> np.ndarray:
    """Terminal path matrix (paths x (months+1)) of portfolio value: contribute at the start of each month, then earn that month's bootstrapped return."""
    R = hist_monthly[list(CLASSES)].to_numpy()
    months = sched.shape[0]
    rng = np.random.default_rng(seed)
    idx = block_indices(rng, len(R), months, block, paths)
    pr = (R[idx] * sched[None, :, :]).sum(axis=2) - haircut_per_year / 12.0         # paths x months portfolio return, less any stress haircut
    v = np.empty((paths, months + 1))
    v[:, 0] = start_value
    for t in range(months):
        v[:, t + 1] = (v[:, t] + monthly_contribution) * (1.0 + pr[:, t])
    return v


def project(hist_monthly: pd.DataFrame, mix: dict, start_value: float, monthly_contribution: float, months: int, target: float, years_left: float, glide: bool = False,
            paths: Optional[int] = None, block: Optional[int] = None, seed: Optional[int] = None, inflation: Optional[float] = None, policy: Optional[dict] = None,
            haircut_points: float = 0.0) -> dict:
    p = policy or pol.load_policy()
    cfg = p["investor"]["projection"]
    paths, block, seed = paths or cfg["paths"], block or cfg["block_months"], cfg["seed_default"] if seed is None else seed
    inflation = cfg["inflation_assumption"] if inflation is None else inflation
    if len(hist_monthly) < cfg["min_history_months"]:
        raise ValueError(f"only {len(hist_monthly)} months of history; at least {cfg['min_history_months']} are needed")
    sched = weight_schedule(mix, months, years_left, glide, p)
    v = simulate(hist_monthly, sched, start_value, monthly_contribution, paths, block, seed, haircut_points / 100.0)
    end = v[:, -1]
    deflator = (1.0 + inflation) ** (months / 12.0)
    pct = lambda a, q: float(np.percentile(a, q))                          # noqa: E731
    qs = cfg["percentiles"]
    fan_idx = sorted(set(list(range(0, months + 1, max(1, months // 60))) + [months]))
    inputs = {"mix": {k: round(mix[k], 6) for k in CLASSES}, "start_value": start_value, "monthly_contribution": monthly_contribution, "months": months, "target": target, "glide": glide,
              "paths": paths, "block_months": block, "seed": seed, "inflation": inflation, "history_start": str(hist_monthly.index[0].date()), "history_end": str(hist_monthly.index[-1].date()),
              "history_months": int(len(hist_monthly)), "haircut_points_per_year": haircut_points, "contribution_timing": "start of each month", "return_basis": "nominal monthly proxy returns (equity and gold price return, debt total return)"}
    run_id = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    contributed = start_value + monthly_contribution * months
    return {
        "run_id": run_id, "inputs": inputs, "n_blocks_in_history": int(len(hist_monthly) // block),
        "percentiles": {str(q): {"nominal": pct(end, q), "real": pct(end, q) / deflator} for q in qs},
        "prob_reach_target": {"nominal": float((end >= target).mean()), "real": float((end >= target * deflator).mean())},
        "contributed": contributed, "prob_below_contributed": float((end < contributed).mean()),
        "fan": {"months": fan_idx, **{str(q): [float(np.percentile(v[:, i], q)) for i in fan_idx] for q in qs}},
        "label": "SCENARIO from resampled history, not a forecast or a guarantee",
    }


def sensitivity(hist_monthly: pd.DataFrame, mix: dict, start_value: float, monthly_contribution: float, months: int, target: float, years_left: float, glide: bool = False,
                seed: Optional[int] = None, policy: Optional[dict] = None) -> list:
    """Probability of reaching the target under different block lengths: if it moves a lot the answer rests on how the history is cut."""
    p = policy or pol.load_policy()
    out = []
    for b in p["investor"]["projection"]["block_sensitivity_months"]:
        r = project(hist_monthly, mix, start_value, monthly_contribution, months, target, years_left, glide, block=b, seed=seed, policy=p)
        out.append({"block_months": b, "prob_reach_target_nominal": r["prob_reach_target"]["nominal"], "median_nominal": r["percentiles"]["50"]["nominal"]})
    return out


def tradeoffs(hist_monthly: pd.DataFrame, mix: dict, start_value: float, monthly_contribution: float, months: int, target: float, years_left: float, glide: bool = False,
              want_prob: float = 0.5, seed: Optional[int] = None, policy: Optional[dict] = None) -> dict:
    """When a goal is off track: what each lever would take to reach `want_prob`, holding the risk profile FIXED. Equity is never raised here."""
    p = policy or pol.load_policy()
    cfg = p["investor"]["projection"]
    sd = cfg["seed_default"] if seed is None else seed
    base = project(hist_monthly, mix, start_value, monthly_contribution, months, target, years_left, glide, seed=sd, policy=p)
    prob = base["prob_reach_target"]["nominal"]
    out = {"want_probability": want_prob, "current_probability": prob, "on_track": prob >= want_prob}
    if prob >= want_prob:
        return out

    def prob_for(sip=monthly_contribution, mths=months, tgt=target):
        return project(hist_monthly, mix, start_value, sip, mths, tgt, years_left + (mths - months) / 12.0, glide, seed=sd, policy=p)["prob_reach_target"]["nominal"]

    lo, hi = monthly_contribution, max(monthly_contribution * 2, 1000.0)
    for _ in range(12):
        if prob_for(sip=hi) >= want_prob:
            break
        hi *= 2
    else:
        hi = None
    if hi is not None:
        for _ in range(16):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if prob_for(sip=mid) >= want_prob else (mid, hi)
        out["monthly_contribution_needed"] = float(hi)
    out["extra_months_needed"] = None                                          # None: not reachable within 10 more years at this mix and SIP
    for extra in range(6, 121, 6):
        if prob_for(mths=months + extra) >= want_prob:
            out["extra_months_needed"] = extra
            break
    out["target_reachable_at_probability"] = float(np.percentile(simulate(hist_monthly, weight_schedule(mix, months, years_left, glide, p), start_value, monthly_contribution,
                                                                               cfg["paths"], cfg["block_months"], sd)[:, -1], (1 - want_prob) * 100))
    out["note"] = "Levers shown with the risk profile held fixed: a higher SIP, a later date, or a smaller target. Taking more equity risk to chase the target is not offered."
    return out


def haircut_sensitivity(hist_monthly: pd.DataFrame, mix: dict, start_value: float, monthly_contribution: float, months: int, target: float, years_left: float, glide: bool = False,
                        seed: Optional[int] = None, policy: Optional[dict] = None) -> list:
    """Probability of reaching the target if every return is lower by a fixed number of points a year: how much the answer leans on the last decade being generous."""
    p = policy or pol.load_policy()
    out = []
    for pts in p["investor"]["projection"]["haircut_points_per_year"]:
        r = project(hist_monthly, mix, start_value, monthly_contribution, months, target, years_left, glide, seed=seed, policy=p, haircut_points=pts)
        out.append({"points_per_year": pts, "prob_reach_target_nominal": r["prob_reach_target"]["nominal"], "median_nominal": r["percentiles"]["50"]["nominal"]})
    return out
