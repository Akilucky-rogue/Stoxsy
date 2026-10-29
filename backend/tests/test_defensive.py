"""Defensive allocation signal: point-in-time scores, weights, lagged execution, costs."""
import numpy as np
import pandas as pd
import pytest

from qsde.defensive import signal as dfs
from qsde.ml.selftest import make_panel

PRICES, _F, CAL = make_panel(n_sym=40, n_days=420, edge_bps=0.0, seed=5)
PRICES = PRICES.assign(volume=1e6)


def _scores(prices):
    return dfs.defensive_scores(dfs.risk_features(prices))


def test_scores_are_point_in_time_future_bars_cannot_change_a_past_score():
    full = _scores(PRICES)
    cut = CAL[300]
    part = _scores(PRICES[PRICES["date"] <= cut])
    a = full[full["date"] <= cut].set_index(["date", "symbol"])["defensive_score"]
    b = part.set_index(["date", "symbol"])["defensive_score"]
    assert len(b) > 0 and np.allclose(a.reindex(b.index), b)


def test_a_calmer_stock_scores_more_defensive_than_a_wild_one():
    p = PRICES.copy()
    rng = np.random.default_rng(0)
    for sym, scale in (("S000", 0.2), ("S001", 6.0)):                       # shrink / blow up S000 / S001 daily moves
        m = p["symbol"] == sym
        base = p.loc[m, "close"].to_numpy()
        ret = np.diff(np.log(base), prepend=np.log(base[0]))
        newc = 100 * np.exp(np.cumsum(ret * scale))
        for c in ("open", "high", "low", "close"):
            p.loc[m, c] = newc * (p.loc[m, c].to_numpy() / base)
    s = _scores(p)
    last = s[s["date"] == s["date"].max()].set_index("symbol")["defensive_score"]
    assert last["S000"] > 0.8 and last["S001"] < 0.2


def test_weights_sum_to_one_tilt_is_monotone_and_gating_only_bites_in_risk_off():
    s = _scores(PRICES)
    d = s[s["date"] == s["date"].max()]
    for scheme in ("ew", "tilt", "tilt_gated", "top_defensive"):
        assert abs(dfs.weights(d, scheme).sum() - 1.0) < 1e-12
    w = dfs.weights(d, "tilt").reindex(d["symbol"])
    assert w.corr(d.set_index("symbol")["defensive_score"].reindex(w.index)) > 0.99
    assert w.max() / w.min() <= 3.0 + 1e-9                                      # 0.5x..1.5x of equal weight: bounded tilt
    assert dfs.weights(d, "tilt_gated", "neutral").equals(dfs.weights(d, "tilt"))
    off, base = dfs.weights(d, "tilt_gated", "risk_off"), dfs.weights(d, "tilt")
    worst = d.nsmallest(5, "defensive_score")["symbol"]
    assert (off[worst] < base[worst]).all()
    assert dfs.weights(d, "top_defensive").gt(0).sum() == pytest.approx(len(d) * 0.2, abs=2)


def test_regime_state_flags_a_crash_as_risk_off_without_lookahead():
    rng = np.random.default_rng(1)
    r = pd.Series(rng.normal(0.0005, 0.008, 900), index=pd.bdate_range("2018-01-01", periods=900))
    r.iloc[700:760] = rng.normal(-0.01, 0.03, 60)                                # 3-month crash
    st = dfs.regime_state(r)
    assert (st["regime"].iloc[730:760] == "risk_off").mean() > 0.5
    st2 = dfs.regime_state(r.iloc[:720])
    assert st2["regime"].iloc[500:720].equals(st["regime"].iloc[500:720])      # the future does not rewrite the past


def test_backtest_is_lagged_costed_and_equal_weight_matches_the_universe_when_free():
    s = _scores(PRICES)
    reg = dfs.regime_state(PRICES.pivot(index="date", columns="symbol", values="close").pct_change().mean(axis=1))
    r0, t0 = dfs.backtest(PRICES, s, reg, "ew", one_way_cost_bps=0.0)
    r1, t1 = dfs.backtest(PRICES, s, reg, "ew", one_way_cost_bps=25.0)
    assert (r0 - r1).sum() == pytest.approx(t1.sum() * 25.0 / 1e4, rel=1e-6)    # cost = traded notional x one-way bps
    assert t1.iloc[0] == pytest.approx(1.0)                                     # the first rebalance buys the whole book
    # lag: weights set from the close of day t first earn the return of day t+2 (t+1 is the execution close)
    dates = list(PRICES.pivot(index="date", columns="symbol", values="close").index)
    first = s["date"].min()
    assert r0.index[0] == dates[dates.index(first) + 2]
    # a spike on a day strictly before the first return date cannot reach the series at all
    px = PRICES.copy()
    px.loc[(px["symbol"] == "S002") & (px["date"] == first), "close"] *= 1.3
    ra, _ = dfs.backtest(PRICES, s, reg, "top_defensive", one_way_cost_bps=0.0)
    rb, _ = dfs.backtest(px, s, reg, "top_defensive", one_way_cost_bps=0.0)
    assert ra.index[0] > first
    assert np.allclose(ra.iloc[1:], rb.iloc[1:])                                 # only the day right after the spike may differ
    assert dfs.stats(r0)["n_days"] == len(r0)
