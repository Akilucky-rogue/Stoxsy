"""Compose the Investor Plan from a profile, goals, balances, priced holdings and the proxy history. Pure given its inputs (the API route fetches them), so every number is reproducible.

One portfolio, several goals: each goal gets its own profile (the lower of capacity from ITS horizon and the person's tolerance), tolerance-gated mix and projection. The portfolio-level
target used for drift and rebalancing is the corpus-weighted blend of the goal mixes. Emergency goals are held in debt and are outside the investment-pot allocation.
Missing questionnaire answers are reported as inputs needed, never filled with defaults.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Optional

import numpy as np

from qsde.investor import assets as A
from qsde.investor import history as H
from qsde.investor import plan as P
from qsde.modes import policy as pol

REQUIRED = ("drawdown_reaction", "income_stability", "dependents")


def years_left(target: date, today: date) -> float:
    return (target - today).days / 365.25


def missing_inputs(profile: dict) -> list:
    return [k for k in REQUIRED if profile.get(k) is None]


def goal_plan(g: dict, profile: dict, hist: H.History, today: date, policy: dict, glide: bool = True) -> dict:
    yl = years_left(g["target_date"], today)
    out = {"id": g["id"], "name": g["name"], "kind": g["kind"], "target_amount": float(g["target_amount"]), "target_date": str(g["target_date"]), "current_amount": float(g["current_amount"]),
           "monthly_sip": float(g["monthly_sip"]), "years_left": yl, "is_test": bool(g.get("is_test"))}
    if yl <= 0:
        return {**out, "status": "past_due", "headline": "The target date has passed: update the date or archive the goal."}
    months = max(1, math.ceil(yl * 12))
    if g["kind"] == "emergency":
        mix = {"equity": 0.0, "gold": 0.0, "debt": 1.0}
        prof, reasons = "emergency", ["An emergency fund is held in liquid debt only: it must be there when needed, so no equity or gold."]
        gated = None
    else:
        cap, cap_reasons = P.capacity_profile(yl, profile["income_stability"], int(profile["dependents"]), policy)
        tol, tolerated = P.tolerance_profile(profile["drawdown_reaction"], policy)
        prof = P.lower_profile(cap, tol, policy)
        reasons = cap_reasons + [f"tolerance ('{profile['drawdown_reaction']}' after a 30% fall) caps the profile at '{tol}'"]
        chosen = profile.get("risk_profile")
        if chosen:
            o = P.order(policy)
            if o.index(chosen) < o.index(prof):
                prof = chosen
                reasons.append(f"you chose the lower profile '{chosen}'")
            elif o.index(chosen) > o.index(prof):
                reasons.append(f"your choice '{chosen}' is above what capacity and tolerance allow, so it was not used")
        start_mix = P.target_mix(prof, policy)
        gated = P.tolerance_gate(hist.daily, start_mix, tolerated)
        mix = gated["mix"]
        if gated["binding"]:
            reasons.append(f"the '{prof}' mix fell {-gated['steps'][0]['max_drawdown']:.0%} in the worst historical stretch, more than the {tolerated:.0%} you said you could hold through, so equity was lowered from {start_mix['equity']:.0%} to {mix['equity']:.0%}")
        if not gated["met"]:
            reasons.append("even with no equity the worst historical fall was above your tolerance; the plan shows this instead of hiding it")
    stats = P.mix_stats(hist.daily, mix)
    proj = P.project(hist.monthly, mix, float(g["current_amount"]), float(g["monthly_sip"]), months, float(g["target_amount"]), yl, glide=glide, policy=policy)
    want = policy["investor"]["projection"]["on_track_probability"]
    prob = proj["prob_reach_target"]["nominal"]
    res = {**out, "status": "on_track" if prob >= want else "off_track", "profile": prof, "profile_reasons": reasons, "mix": mix, "gate": gated, "stress": stats, "projection": proj, "glide": glide,
           "haircut": P.haircut_sensitivity(hist.monthly, mix, float(g["current_amount"]), float(g["monthly_sip"]), months, float(g["target_amount"]), yl, glide, policy=policy),
           "sensitivity": P.sensitivity(hist.monthly, mix, float(g["current_amount"]), float(g["monthly_sip"]), months, float(g["target_amount"]), yl, glide, policy=policy)}
    if prob < want:
        res["tradeoffs"] = P.tradeoffs(hist.monthly, mix, float(g["current_amount"]), float(g["monthly_sip"]), months, float(g["target_amount"]), yl, glide, want_prob=want, policy=policy)
    res["headline"] = (f"In {prob:.0%} of resampled scenarios this goal reaches ₹{float(g['target_amount']):,.0f} by {g['target_date']} (range of outcomes ₹{proj['percentiles']['10']['nominal']:,.0f} to "
                       f"₹{proj['percentiles']['90']['nominal']:,.0f}, middle ₹{proj['percentiles']['50']['nominal']:,.0f}). A scenario, not a forecast.")
    return res


def blended_target(goals: list) -> Optional[dict]:
    """Corpus-weighted blend of the non-emergency goal mixes (SIP-weighted when no goal has a corpus yet)."""
    gs = [g for g in goals if g.get("mix") and g["kind"] != "emergency"]
    if not gs:
        return None
    w = np.array([g["current_amount"] for g in gs], dtype=float)
    if w.sum() <= 0:
        w = np.array([g["monthly_sip"] for g in gs], dtype=float)
    if w.sum() <= 0:
        w = np.ones(len(gs))
    w = w / w.sum()
    return {c: float(sum(wi * g["mix"][c] for wi, g in zip(w, gs))) for c in P.CLASSES}


def compose(profile: dict, goals: list, balances: list, holdings: list, hist: H.History, inst: dict, today: date, policy: Optional[dict] = None) -> dict:
    policy = policy or pol.load_policy()
    alloc = A.current_allocation(holdings, balances, inst)
    notes = A.completeness_notes(alloc, bool(balances))
    base = {"as_of": str(today), "policy_version": policy["version"], "pot": "investment", "label": "SCENARIO and risk framing from resampled history: not a forecast, not investment advice. No engine is approved.",
            "history": {**hist.window, "proxies": hist.proxies, "notes": hist.notes, "monthly_blocks_of_6": hist.window["months"] // 6},
            "current": {"values": alloc["values"], "parts": alloc["parts"], "unclassified": alloc["unclassified"], "unpriced": alloc["unpriced"], "total_classified": alloc["total_classified"]},
            "notes": notes, "emergency_fund": profile.get("emergency_fund")}
    if profile.get("emergency_fund") is None:
        base["notes"].append("Emergency fund is not recorded, so this plan cannot say whether it is adequate before investing.")
    miss = missing_inputs(profile)
    if miss:
        return {**base, "status": "needs_input", "needs": miss, "goals": [], "headline": "Answer the profile questions to get a plan: " + ", ".join(m.replace("_", " ") for m in miss) + "."}
    if not goals:
        return {**base, "status": "needs_goal", "goals": [], "headline": "Add at least one goal (amount, date, current savings, monthly SIP) to get a plan."}
    gp = [goal_plan(g, profile, hist, today, policy) for g in goals]
    target = blended_target(gp)
    out = {**base, "status": "ok", "goals": gp, "target": target}
    inv_sip = sum(g["monthly_sip"] for g in gp if g["kind"] != "emergency" and g["status"] != "past_due")
    if target:
        rb = P.rebalance_plan(alloc["values"], target, inv_sip, alloc["unclassified_value"], today, policy)
        out["rebalance"] = rb
        out["stress"] = P.mix_stats(hist.daily, target)
        out["headline"] = rb["headline"]
    else:
        out["headline"] = "Only emergency goals so far: add a long-term goal to get an allocation."
    return out
