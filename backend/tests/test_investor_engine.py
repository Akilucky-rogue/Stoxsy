"""Investor engine (pure): profile constraint, tolerance gate, bands and rebalance actions, reproducible projections, trade-offs that never raise equity, and the history helpers."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from qsde.investor import history as H
from qsde.investor import plan as P

TODAY = date(2026, 10, 23)


def daily(n=600, seed=0, crash_at=None, crash=-0.5):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2022-01-03", periods=n)
    r = pd.DataFrame({"equity": rng.normal(0.0004, 0.01, n), "gold": rng.normal(0.0002, 0.006, n), "debt": np.full(n, 0.00022)}, index=idx)
    if crash_at is not None:
        r.iloc[crash_at, 0] = crash
    return r


def monthly(n=120, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2016-01-31", periods=n, freq="ME")
    return pd.DataFrame({"equity": rng.normal(0.009, 0.045, n), "gold": rng.normal(0.01, 0.04, n), "debt": np.full(n, 0.005)}, index=idx)


# ── profile ──────────────────────────────────────────────────────────

def test_capacity_follows_the_horizon_and_steps_down_for_uncertain_income_and_dependents():
    assert P.capacity_profile(2, "stable", 0)[0] == "conservative"
    assert P.capacity_profile(6, "stable", 0)[0] == "balanced"
    assert P.capacity_profile(15, "stable", 0)[0] == "aggressive"
    assert P.capacity_profile(15, "uncertain", 0)[0] == "growth"
    assert P.capacity_profile(15, "uncertain", 4)[0] == "balanced"
    assert P.capacity_profile(1, "uncertain", 4)[0] == "conservative"                      # floor


def test_the_profile_is_the_lower_of_capacity_and_tolerance_never_higher():
    assert P.lower_profile("aggressive", "balanced") == "balanced" and P.lower_profile("conservative", "growth") == "conservative"
    cap, tol = P.capacity_profile(15, "stable", 0)[0], P.tolerance_profile("sell_some")[0]
    assert P.lower_profile(cap, tol) == "balanced"
    with pytest.raises(ValueError):
        P.tolerance_profile("panic")


def test_every_target_mix_sums_to_one():
    for name in P.order():
        assert sum(P.target_mix(name).values()) == pytest.approx(1.0)


# ── history stats and the tolerance gate ─────────────────────────────

def test_mix_stats_on_a_known_crash():
    d = daily(crash_at=300)
    d["equity"] = np.where(np.arange(len(d)) == 300, -0.5, 0.0004)               # steady rise, one -50% day: the fall and its date are known exactly
    eq_only = P.mix_stats(d, {"equity": 1.0, "gold": 0.0, "debt": 0.0})
    half = P.mix_stats(d, {"equity": 0.5, "gold": 0.0, "debt": 0.5})
    assert eq_only["max_drawdown"] == pytest.approx(-0.5, abs=1e-6) and eq_only["trough"] == str(d.index[300].date())
    assert -0.26 < half["max_drawdown"] < -0.24                                            # about half of the fall


def test_tolerance_gate_lowers_equity_until_the_fall_is_tolerable_and_never_raises_it():
    d = daily(crash_at=300, crash=-0.4)
    start = P.target_mix("aggressive")
    g = P.tolerance_gate(d, start, tolerated_fall=0.20)
    assert g["binding"] and g["mix"]["equity"] < start["equity"] and g["met"] and -g["final_max_drawdown"] <= 0.20
    assert g["mix"]["gold"] == start["gold"] and sum(g["mix"].values()) == pytest.approx(1.0)
    easy = P.tolerance_gate(d, start, tolerated_fall=0.9)
    assert not easy["binding"] and easy["mix"] == start
    eqs = [s["equity"] for s in g["steps"]]
    assert eqs == sorted(eqs, reverse=True)


def test_an_impossible_tolerance_ends_at_zero_equity_and_says_it_was_not_met():
    d = daily(crash_at=300, crash=-0.4)
    d["gold"] = -0.002                                                                      # gold alone falls steadily
    g = P.tolerance_gate(d, {"equity": 0.5, "gold": 0.5, "debt": 0.0}, tolerated_fall=0.01)
    assert g["mix"]["equity"] == pytest.approx(0.0) and not g["met"]


# ── glide path ───────────────────────────────────────────────────────

def test_glide_path_cuts_two_points_a_year_in_the_last_five_and_moves_to_debt():
    m = P.target_mix("growth")
    assert P.apply_glide(m, 6) == m and P.apply_glide(m, 5) == m
    g3 = P.apply_glide(m, 3)
    assert g3["equity"] == pytest.approx(m["equity"] - 0.04) and g3["debt"] == pytest.approx(m["debt"] + 0.04) and sum(g3.values()) == pytest.approx(1.0)
    assert P.apply_glide(m, 0)["equity"] == pytest.approx(m["equity"] - 0.10)
    assert P.apply_glide({"equity": 0.05, "gold": 0.0, "debt": 0.95}, 0)["equity"] == 0.0     # cannot go below zero
    assert P.glide_equity_cut(3, points_per_year=0) == 0                                    # configurable (off)


# ── bands and rebalance actions ──────────────────────────────────────

def test_band_rule_is_absolute_or_relative_and_the_relative_band_fires_first_for_small_targets():
    t = P.target_mix("balanced")                                                            # 55 / 10 / 35
    assert not any(b["breach"] for b in P.band_status({"equity": 0.57, "gold": 0.10, "debt": 0.33}, t))
    gold = P.band_status({"equity": 0.55, "gold": 0.123, "debt": 0.327}, t)
    g = [b for b in gold if b["class"] == "gold"][0]
    assert [b["class"] for b in gold if b["breach"]] == ["gold"] and g["rule"] == "relative"      # 2.3 points but 23% of its target
    eq = P.band_status({"equity": 0.605, "gold": 0.10, "debt": 0.295}, t)
    assert [b["class"] for b in eq if b["breach"]] == ["equity", "debt"] and eq[0]["rule"] == "absolute"


def test_no_action_needed_is_a_normal_answer():
    t = P.target_mix("balanced")
    r = P.rebalance_plan({"equity": 5_600_000, "gold": 1_000_000, "debt": 3_400_000}, t, 25_000, today=TODAY)
    assert r["action"] == "no_action" and "No action needed" in r["headline"] and r["trades"] == [] and r["next_review"] == "2027-01-23"


def test_new_sip_money_is_used_before_any_sale():
    t = P.target_mix("balanced")
    v = {"equity": 6_300_000, "gold": 900_000, "debt": 2_800_000}                           # equity 63%: outside the band
    r = P.rebalance_plan(v, t, 400_000, today=TODAY)
    assert r["action"] == "direct_sip" and r["trades"] == [] and r["months_to_fix"] <= 12 and "no selling" in r["headline"]
    split = P.sip_split(v, t, 400_000)
    assert split["equity"] < split["debt"] and sum(split.values()) == pytest.approx(400_000)


def test_a_sale_is_proposed_only_when_sip_cannot_fix_it_and_the_trade_is_worth_making():
    t = P.target_mix("balanced")
    v = {"equity": 7_000_000, "gold": 600_000, "debt": 2_400_000}
    r = P.rebalance_plan(v, t, 1_000, today=TODAY)
    assert r["action"] == "rebalance" and {x["class"] for x in r["trades"]} == {"equity", "gold", "debt"}
    sells = [x for x in r["trades"] if x["side"] == "sell"]
    assert [x["class"] for x in sells] == ["equity"] and sells[0]["amount"] == pytest.approx(0.15 * 10_000_000) and "tax" in r["headline"].lower()
    small = P.rebalance_plan({"equity": 6_000, "gold": 700, "debt": 2_300}, t, 0, today=TODAY)
    assert small["action"] == "no_action" and "smaller than the minimum" in small["headline"]


def test_incomplete_data_blocks_sales_and_empty_input_says_so():
    t = P.target_mix("balanced")
    v = {"equity": 7_000_000, "gold": 600_000, "debt": 2_400_000}
    r = P.rebalance_plan(v, t, 0, other_value=2_000_000, today=TODAY)
    assert r["action"] == "data_incomplete" and r["trades"] == [] and r["unclassified_share"] == pytest.approx(2 / 12)
    assert P.rebalance_plan({}, t, 10_000, today=TODAY)["action"] == "no_data"


# ── projection ───────────────────────────────────────────────────────

def test_projection_is_exact_when_returns_are_constant():
    const = pd.DataFrame({c: np.full(100, 0.01) for c in P.CLASSES}, index=pd.date_range("2016-01-31", periods=100, freq="ME"))
    r = P.project(const, P.target_mix("balanced"), 100_000, 10_000, 24, 1_000_000, 2, paths=50, block=6, seed=1, inflation=0.0)
    v = 100_000.0
    for _ in range(24):
        v = (v + 10_000) * 1.01
    assert r["percentiles"]["50"]["nominal"] == pytest.approx(v) and r["percentiles"]["10"]["nominal"] == pytest.approx(v) and r["prob_reach_target"]["nominal"] == 0.0


def test_projection_is_reproducible_ordered_and_states_its_sample():
    m = monthly()
    a = P.project(m, P.target_mix("growth"), 0, 20_000, 120, 4_000_000, 10)
    b = P.project(m, P.target_mix("growth"), 0, 20_000, 120, 4_000_000, 10)
    c = P.project(m, P.target_mix("growth"), 0, 20_000, 120, 4_000_000, 10, seed=7)
    assert a == b and a["run_id"] == b["run_id"] and a["percentiles"] != c["percentiles"] and a["run_id"] != c["run_id"]
    p = a["percentiles"]
    assert p["10"]["nominal"] < p["50"]["nominal"] < p["90"]["nominal"] and p["50"]["real"] < p["50"]["nominal"]
    assert a["inputs"]["history_months"] == 120 and a["inputs"]["seed"] == 20261023 and a["n_blocks_in_history"] == 20 and "not a forecast" in a["label"]
    assert a["fan"]["months"][0] == 0 and a["fan"]["months"][-1] == 120 and len(a["fan"]["50"]) == len(a["fan"]["months"])


def test_too_little_history_is_refused():
    with pytest.raises(ValueError, match="months of history"):
        P.project(monthly(30), P.target_mix("balanced"), 0, 1000, 12, 10_000, 1)


def test_blocks_stay_inside_the_history_and_do_not_wrap():
    rng = np.random.default_rng(3)
    idx = P.block_indices(rng, 50, 37, 6, 400)
    assert idx.shape == (400, 37) and idx.min() >= 0 and idx.max() <= 49
    assert all((np.diff(row[:36].reshape(6, 6), axis=1) == 1).all() for row in idx[:20])   # each block is consecutive months


def test_glide_lowers_the_risk_of_the_projection_and_sensitivity_covers_block_lengths():
    m = monthly()
    flat = P.project(m, P.target_mix("aggressive"), 500_000, 10_000, 60, 2_000_000, 5, glide=False)
    glide = P.project(m, P.target_mix("aggressive"), 500_000, 10_000, 60, 2_000_000, 5, glide=True)
    spread = lambda r: r["percentiles"]["90"]["nominal"] - r["percentiles"]["10"]["nominal"]       # noqa: E731
    assert spread(glide) < spread(flat)
    s = P.sensitivity(m, P.target_mix("balanced"), 0, 10_000, 60, 1_000_000, 5)
    assert [x["block_months"] for x in s] == [3, 6, 12]


def test_tradeoffs_never_raise_equity_and_name_the_levers():
    m = monthly()
    mix = P.target_mix("balanced")
    t = P.tradeoffs(m, mix, 0, 10_000, 120, 6_000_000, 10)
    assert not t["on_track"] and t["monthly_contribution_needed"] > 10_000 and "extra_months_needed" in t
    assert t["target_reachable_at_probability"] < 6_000_000 and "not offered" in t["note"] and "mix" not in t
    ok = P.tradeoffs(m, mix, 0, 50_000, 120, 1_000_000, 10)
    assert ok["on_track"] and "monthly_contribution_needed" not in ok
    assert mix == P.target_mix("balanced")                                                  # the mix was never touched


# ── history helpers ──────────────────────────────────────────────────

def test_a_unit_change_day_is_dropped_not_treated_as_a_return():
    idx = pd.bdate_range("2019-12-16", periods=6)
    p = pd.Series([100.0, 101.0, 102.0, 10.2, 10.3, 10.4], index=idx)                     # 10:1 unit change on the 4th day
    r, dropped = H.daily_returns(p)
    assert dropped == [str(idx[3].date())] and (r.abs() < 0.05).all() and len(r) == 4


def test_daily_to_monthly_compounds_and_debt_accrual_survives_weekends():
    idx = pd.date_range("2024-01-01", "2024-03-31", freq="D")
    debt = pd.Series(0.0002, index=idx)
    days = pd.bdate_range("2024-01-01", "2024-03-29")
    eq = pd.Series(0.001, index=days)
    d = H.daily_aligned({"equity": eq, "gold": eq, "debt": debt})
    n_cal = len(idx[(idx > days[0]) & (idx <= days[-1])])
    assert (1 + d["debt"]).prod() == pytest.approx(1.0002 ** n_cal, rel=1e-9)               # every calendar day after the first trading day accrued, weekends included
    mo = H.to_monthly(pd.Series([0.1, 0.1], index=pd.to_datetime(["2024-01-02", "2024-01-03"])))
    assert mo.iloc[0] == pytest.approx(0.21)


def test_debt_basket_is_the_median_and_ignores_bad_prints():
    idx = pd.date_range("2024-01-01", periods=5)
    nav = pd.DataFrame({1: [100, 100.01, 100.02, 100.03, 100.04], 2: [100, 100.02, 100.04, 100.06, 100.08], 3: [100, 100.015, 100.03, 100.045, 100.06],
                        4: [100, 100, 200, 200, 200]}, index=idx, dtype=float)
    b = H.debt_basket(nav)
    assert b.iloc[0] == pytest.approx(0.000125) and b.iloc[1] == pytest.approx(0.00015, rel=0.02)    # day 2: the +100% print of scheme 4 is excluded, median of the other three
    assert (b.abs() < 0.01).all()


def test_a_return_haircut_lowers_the_outcome_and_is_part_of_the_run_identity():
    m = monthly()
    mix = P.target_mix("growth")
    base = P.project(m, mix, 0, 20_000, 120, 4_000_000, 10)
    cut = P.project(m, mix, 0, 20_000, 120, 4_000_000, 10, haircut_points=3.0)
    assert cut["percentiles"]["50"]["nominal"] < base["percentiles"]["50"]["nominal"] and cut["prob_reach_target"]["nominal"] <= base["prob_reach_target"]["nominal"] and cut["run_id"] != base["run_id"]
    h = P.haircut_sensitivity(m, mix, 0, 20_000, 120, 4_000_000, 10)
    assert [x["points_per_year"] for x in h] == [2, 4] and h[1]["median_nominal"] < h[0]["median_nominal"] < base["percentiles"]["50"]["nominal"]
