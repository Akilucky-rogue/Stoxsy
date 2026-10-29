"""Tests for the PairsStatArbEngine (StatArb family) — cointegration math, the
defined-risk PAIR idea, and forward spread resolution. All pure (no DB/network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Structure
from qsde.engines.pairs import PairsStatArbEngine
from qsde.scorecard.resolver import resolve_pair_outcome
from qsde.stats.pairs_math import evaluate_pair, half_life, hedge_ratio


# ── synthetic cointegrated pair ───────────────────────────────────────

def _coint_pair(n=320, beta=2.0, intercept=5.0, phi=0.7, last_z=2.6, seed=0):
    """b = random walk; a = beta*b + intercept + AR(1) stationary residual.
    The final residual is pushed to ~last_z sigmas so the spread is 'stretched'."""
    rng = np.random.default_rng(seed)
    b = 100 + np.cumsum(rng.normal(0, 1, n))
    resid = np.zeros(n)
    for t in range(1, n):
        resid[t] = phi * resid[t - 1] + rng.normal(0, 1)
    resid[-1] = last_z * float(np.std(resid))          # force a stretched last point
    a = beta * b + intercept + resid
    return pd.Series(a), pd.Series(b)


# ── pairs_math ────────────────────────────────────────────────────────

def test_hedge_ratio_recovers_beta():
    b = np.linspace(50, 150, 100)
    a = 2.5 * b + 7.0
    beta, alpha = hedge_ratio(a, b)
    assert beta == pytest.approx(2.5, abs=1e-6) and alpha == pytest.approx(7.0, abs=1e-4)


def test_half_life_from_ar1():
    # AR(1) phi=0.5 -> spread reverts; hl = -ln2 / (phi-1) = -ln2/-0.5
    rng = np.random.default_rng(1)
    s = np.zeros(500)
    for t in range(1, 500):
        s[t] = 0.5 * s[t - 1] + rng.normal(0, 1)
    hl = half_life(s)
    assert 0.5 < hl < 2.0          # theoretical ~1.0


def test_evaluate_pair_cointegrated_is_tradeable():
    a, b = _coint_pair()
    ev = evaluate_pair(a, b, lookback=250)
    assert ev["ok"] and ev["tradeable"]
    assert ev["beta"] == pytest.approx(2.0, abs=0.1)
    assert ev["pvalue"] <= 0.05 and 1.0 <= ev["half_life"] <= 30.0
    assert abs(ev["z"]) >= 2.0


def test_evaluate_pair_independent_walks_not_tradeable():
    rng = np.random.default_rng(3)
    a = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    b = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))   # unrelated random walk
    ev = evaluate_pair(a, b, lookback=250)
    assert ev["ok"] and not ev["tradeable"]                # high p-value -> not cointegrated


# ── engine ────────────────────────────────────────────────────────────

def _engine(sa, sb, **kw):
    return PairsStatArbEngine(pairs=[("A", "B")],
                              price_source=lambda: {"A": sa, "B": sb}, **kw)


def test_engine_emits_defined_risk_pair_when_stretched():
    a, b = _coint_pair(last_z=2.6)
    ideas = _engine(a, b, entry_z=2.0, stop_z=3.5, risk_rupees=2000.0).scan()
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.structure == Structure.PAIR and idea.direction == 0
    assert len(idea.legs) == 2
    # z>0 (a rich) -> SHORT spread: short A (side -1), long B (side +1)
    a_leg = next(l for l in idea.legs if l.symbol == "A")
    b_leg = next(l for l in idea.legs if l.symbol == "B")
    assert a_leg.side == -1 and b_leg.side == +1
    assert idea.max_loss_rupees > 0 and idea.max_loss_rupees <= 2000.0 + 1
    assert 0.0 <= idea.confidence <= 1.0
    idea.validate()
    plan = json.loads(idea.adjustment_plan)
    assert plan["side"] == -1 and plan["sym_a"] == "A" and plan["spread_sigma"] > 0


def test_engine_skips_when_not_stretched():
    a, b = _coint_pair(last_z=0.3)                          # spread near its mean
    assert _engine(a, b, entry_z=2.0).scan() == []


def test_engine_skips_non_cointegrated():
    rng = np.random.default_rng(7)
    a = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    b = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    assert _engine(a, b).scan() == []


# ── resolver (forward spread replay) ──────────────────────────────────

def _plan(side=-1, spread_0=2.5, qty_a=10, max_loss=10.0, max_hold=20, gross=0.0):
    # beta=2, alpha=5, mu=0, sigma=1 -> spread_t = a - 2b - 5, z = spread_t
    return {"sym_a": "A", "sym_b": "B", "beta": 2.0, "alpha": 5.0, "spread_mean": 0.0,
            "spread_sigma": 1.0, "spread_0": spread_0, "exit_z": 0.5, "stop_z": 3.5,
            "side": side, "qty_a": qty_a, "qty_b": 20, "max_loss": max_loss,
            "gross_notional": gross, "max_hold": max_hold}


def _bars(spreads):
    """Forward bars where spread_t = close_a - 2*close_b - 5; fix close_b=50."""
    dates = pd.date_range("2026-02-02", periods=len(spreads), freq="D")
    b = pd.DataFrame({"date": dates, "close": [50.0] * len(spreads)})
    a = pd.DataFrame({"date": dates, "close": [105.0 + s for s in spreads]})
    return a, b


def test_pair_resolves_win_on_reversion():
    # entered short at z=2.5; spread reverts to 0 on session 3 -> WIN, +pnl
    a, b = _bars([2.0, 1.0, 0.0, -0.2])
    out = resolve_pair_outcome(_plan(), a, b)
    assert out["status"] == "WIN"
    assert out["realized_ret"] == pytest.approx(10 * (2.5 - 0.0) / 10, abs=1e-6)   # +2.5


def test_pair_resolves_loss_on_stop():
    # spread runs UP to z=4 (>stop 3.5) -> LOSS, clamped to -max_loss
    a, b = _bars([3.0, 4.0])
    out = resolve_pair_outcome(_plan(), a, b)
    assert out["status"] == "LOSS" and out["realized_ret"] == pytest.approx(-1.0)


def test_pair_resolves_time_at_max_hold():
    a, b = _bars([2.0] * 20)                                # never exits/stops
    out = resolve_pair_outcome(_plan(max_hold=20), a, b)
    assert out["status"] == "TIME"
    assert out["realized_ret"] == pytest.approx(10 * (2.5 - 2.0) / 10, abs=1e-6)   # +0.5


def test_pair_not_resolvable_without_enough_bars():
    a, b = _bars([2.0, 2.1])                                # no exit/stop, < max_hold
    assert resolve_pair_outcome(_plan(max_hold=20), a, b) is None


def test_pair_net_subtracts_cost():
    a, b = _bars([0.0])
    gross = out_gross = resolve_pair_outcome(_plan(gross=0.0), a, b)["realized_ret_net"]
    netted = resolve_pair_outcome(_plan(gross=100000.0), a, b)["realized_ret_net"]
    assert netted < out_gross                              # cost drags net down
