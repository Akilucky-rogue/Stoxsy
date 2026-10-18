"""Barrier walker + spec-driven labels: one implementation, graded the way paper trading resolves."""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from qsde.ml.barrier import walk_barriers, walk_barriers_matrix
from qsde.ml.labels import make_labels


def _bars(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"]).assign(
        date=pd.bdate_range("2026-01-05", periods=len(rows)))


class TestWalker:
    def test_intrabar_target_and_stop(self):
        b = _bars([(100, 106, 99, 104)])
        assert walk_barriers(b, stop=95, target=105, direction=1)["status"] == "WIN"
        b = _bars([(100, 101, 94, 96)])
        r = walk_barriers(b, stop=95, target=105, direction=1)
        assert r["status"] == "LOSS" and r["exit_price"] == 95 and not r["gap_exit"]

    def test_same_bar_tie_is_a_stop(self):
        b = _bars([(100, 106, 94, 100)])
        assert walk_barriers(b, stop=95, target=105, direction=1)["status"] == "LOSS"

    def test_gap_down_through_stop_fills_at_the_open_not_the_stop(self):
        b = _bars([(100, 101, 99, 100), (90, 92, 88, 91)])          # opens at 90, stop 95
        r = walk_barriers(b, stop=95, target=110, direction=1)
        assert r["status"] == "LOSS" and r["exit_price"] == 90 and r["gap_exit"] and r["exit_idx"] == 1

    def test_gap_up_through_target_fills_at_the_open(self):
        b = _bars([(100, 101, 99, 100), (112, 115, 111, 113)])
        r = walk_barriers(b, stop=95, target=110, direction=1)
        assert r["status"] == "WIN" and r["exit_price"] == 112 and r["gap_exit"]

    def test_short_mirrors_long(self):
        b = _bars([(100, 101, 99, 100), (108, 109, 106, 107)])       # gap UP through a short stop at 105
        r = walk_barriers(b, stop=105, target=90, direction=-1)
        assert r["status"] == "LOSS" and r["exit_price"] == 108 and r["gap_exit"]
        b = _bars([(100, 101, 89, 95)])
        assert walk_barriers(b, stop=105, target=90, direction=-1)["status"] == "WIN"

    def test_time_exit_at_last_close_and_incomplete_window_is_none(self):
        b = _bars([(100, 101, 99, 100), (100, 102, 99, 101), (101, 103, 100, 102)])
        r = walk_barriers(b, stop=90, target=120, direction=1, sessions=3)
        assert r["status"] == "TIME" and r["exit_price"] == 102 and r["exit_idx"] == 2
        assert walk_barriers(b.head(2), stop=90, target=120, direction=1, sessions=3) is None

    def test_without_open_column_gap_rule_is_skipped(self):
        b = _bars([(100, 101, 99, 100), (90, 92, 88, 91)]).drop(columns="open")
        r = walk_barriers(b, stop=95, target=110, direction=1)
        assert r["exit_price"] == 95 and not r["gap_exit"]          # legacy behaviour

    def test_paper_journal_delegates_to_the_shared_walker(self):
        from qsde.execution.paper_journal import _resolve_barrier_daily
        b = _bars([(100, 101, 99, 100), (90, 92, 88, 91)])
        status, px, _ = _resolve_barrier_daily(b, entry_price=100, target=110, stop=95, direction=1)
        assert (status, px) == ("LOSS", 90.0)                        # gap-aware now


class TestMatrixTwin:
    def test_vectorised_walker_agrees_with_the_scalar_one_on_random_paths(self):
        rng = np.random.default_rng(0)
        n, h = 3000, 6
        entry = rng.uniform(50, 500, n)
        r = rng.normal(0, 0.02, (n, h))
        C = entry[:, None] * np.cumprod(1 + r, axis=1)
        O = np.hstack([entry[:, None], C[:, :-1]]) * (1 + rng.normal(0, 0.012, (n, h)))
        H = np.maximum(O, C) * (1 + np.abs(rng.normal(0, 0.008, (n, h))))
        L = np.minimum(O, C) * (1 - np.abs(rng.normal(0, 0.008, (n, h))))
        side = rng.choice([-1.0, 1.0], n)
        sig = rng.uniform(0.01, 0.03, n)
        stop = entry * (1 - side * 1.5 * sig)
        target = entry * (1 + side * 2.5 * sig)
        st, idx, px, gap = walk_barriers_matrix(O, H, L, C, stop, target, side)
        for i in range(n):
            bars = pd.DataFrame({"open": O[i], "high": H[i], "low": L[i], "close": C[i]})
            s = walk_barriers(bars, stop[i], target[i], int(side[i]), sessions=h)
            code = {"WIN": 1, "LOSS": -1, "TIME": 0}[s["status"]]
            assert (st[i], idx[i], bool(gap[i])) == (code, s["exit_idx"], s["gap_exit"]), i
            assert px[i] == pytest.approx(s["exit_price"])

    def test_gap_target_wins_over_a_deeper_intrabar_stop_like_the_scalar_walker(self):
        # bar opens above the target AND its low is below the stop: scalar order = gap-target first
        O = np.array([[115.0]]); H = np.array([[120.0]]); L = np.array([[90.0]]); C = np.array([[100.0]])
        st, idx, px, gap = walk_barriers_matrix(O, H, L, C, np.array([95.0]), np.array([110.0]), np.array([1.0]))
        bars = pd.DataFrame({"open": [115.0], "high": [120.0], "low": [90.0], "close": [100.0]})
        s = walk_barriers(bars, 95.0, 110.0, 1)
        assert s["status"] == "WIN" and st[0] == 1 and px[0] == 115.0 and gap[0]


def _market(n_sym=3, n=400, seed=5, start="2025-01-01"):
    rng = np.random.default_rng(seed)
    cal = pd.bdate_range(start, periods=n)
    rows = []
    for s in range(n_sym):
        c = 100 * np.exp(np.cumsum(rng.normal(3e-4, 0.015, n)))
        o = c * (1 + rng.normal(0, 0.004, n))
        h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.006, n)))
        l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.006, n)))
        rows.append(pd.DataFrame({"symbol": f"S{s}", "date": cal, "open": o, "high": h, "low": l, "close": c}))
    return pd.concat(rows, ignore_index=True), cal


class TestLabels:
    def test_entry_is_next_open_and_bar_t_is_not_in_the_window(self):
        prices, cal = _market()
        ev = prices[prices["symbol"] == "S0"][["symbol", "date"]].iloc[150:160]
        lab = make_labels(prices, ev, "swing_5d")
        px = prices.set_index(["symbol", "date"])
        for r in lab.itertuples():
            assert r.entry == px.loc[("S0", r.entry_date), "open"]
            assert r.entry_date == cal[cal.get_loc(r.as_of_date) + 1]
            assert r.label_end_date > r.as_of_date                      # nothing on/before t
            assert 1 <= r.exit_session <= 5

    def test_barriers_scale_with_sigma_and_registry_multipliers(self):
        prices, _ = _market()
        ev = prices[prices["symbol"] == "S1"][["symbol", "date"]].iloc[200:205]
        lab = make_labels(prices, ev, "swing_5d")
        up = (lab["target"] / lab["entry"] - 1) / lab["sigma"]
        dn = (1 - lab["stop"] / lab["entry"]) / lab["sigma"]
        assert np.allclose(up, 2.5) and np.allclose(dn, 1.5)

    def test_label_equals_scorecard_resolver_on_the_same_path(self):
        """The harness grades what paper trading resolves."""
        from qsde.scorecard.resolver import resolve_idea_outcome

        prices, cal = _market(n_sym=4, n=500, seed=11)
        ev = prices[["symbol", "date"]].groupby("symbol").apply(lambda g: g.iloc[150:420:3]).reset_index(drop=True)
        lab = make_labels(prices, ev, "swing_5d")
        assert len(lab) > 200
        px = prices.set_index(["symbol", "date"]).sort_index()
        for r in lab.sample(120, random_state=1).itertuples():
            i = cal.get_loc(r.as_of_date)
            win_dates = cal[i + 1:i + 6]
            bars = px.loc[r.symbol].loc[win_dates].rename_axis("date").reset_index()[
                ["date", "open", "high", "low", "close"]]
            out = resolve_idea_outcome(r.entry, r.stop, r.target, r.side, "swing", bars)
            assert out["status"] == r.status
            assert out["exit_price"] == pytest.approx(r.exit_price)
            assert pd.Timestamp(out["exit_date"]) == r.exit_date
            assert out["realized_ret"] == pytest.approx(r.ret_gross, abs=1e-6)
            assert out["realized_ret_net"] == pytest.approx(r.ret_net, abs=1e-6)       # same paper cost

    def test_path_returns_compound_to_the_total_gross_return(self):
        prices, _ = _market()
        ev = prices[prices["symbol"] == "S2"][["symbol", "date"]].iloc[100:140]
        lab = make_labels(prices, ev, "swing_5d")
        rc = [f"r{k}" for k in range(1, 6)]
        compounded = (1 + lab[rc]).prod(axis=1) - 1
        assert np.allclose(compounded, lab["ret_gross"], atol=1e-9)
        # flat after the exit session
        for r in lab.itertuples():
            assert all(getattr(r, f"r{k}") == 0.0 for k in range(r.exit_session + 1, 6))

    def test_incomplete_tail_windows_are_not_labelled(self):
        prices, cal = _market(n=300)
        ev = prices[prices["symbol"] == "S0"][["symbol", "date"]].tail(3)
        assert make_labels(prices, ev, "swing_5d").empty

    def test_unexplained_event_inside_the_window_is_dropped_or_flagged(self):
        prices, cal = _market()
        ev = prices[prices["symbol"] == "S0"][["symbol", "date"]].iloc[150:170]
        ue = pd.DataFrame({"symbol": ["S0"], "ex_date": [cal[160]]})
        kept = make_labels(prices, ev, "swing_5d", unexplained=ue)
        flagged = make_labels(prices, ev, "swing_5d", unexplained=ue, drop_contaminated=False)
        e = cal[160]
        # touched <=> the ex-date falls inside (t, actual label end]; a trade that already
        # exited before the event is unaffected by it
        expect_touch = (flagged["as_of_date"] < e) & (flagged["label_end_date"] >= e)
        assert (flagged["touches_unexplained"] == expect_touch).all() and expect_touch.sum() >= 1
        # feature dates on/after the ex-date (within 60 sessions) are contaminated
        assert (flagged["feature_contaminated"] == (flagged["as_of_date"] >= e)).all()
        assert len(kept) == len(flagged) - flagged[["touches_unexplained", "feature_contaminated"]].any(axis=1).sum()

    def test_missing_symbol_days_cannot_hit_barriers_and_missing_entry_is_unlabelled(self):
        prices, cal = _market()
        dropped = prices[~((prices["symbol"] == "S0") & (prices["date"] == cal[151]))]
        ev = pd.DataFrame({"symbol": "S0", "date": [cal[150], cal[152]]})
        lab = make_labels(dropped, ev, "swing_5d")
        assert list(lab["as_of_date"]) == [cal[152]]                   # entry bar (t+1 = 151) missing -> no label

    def test_costs_come_from_the_registry_by_default(self):
        from qsde.ml.registry import promotion_cost_bps
        prices, _ = _market()
        ev = prices[prices["symbol"] == "S0"][["symbol", "date"]].iloc[100:105]
        lab = make_labels(prices, ev, "swing_5d")
        assert (lab["cost_bps"] == promotion_cost_bps("swing")).all() and (lab["cost_bps"] == 30.0).all()
        assert np.allclose(lab["ret_gross"] - lab["ret_net"], 0.003)
