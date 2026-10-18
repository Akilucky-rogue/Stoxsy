"""Corporate-action detection / adjustment tests (method price_gap_fingerprint_v1).

Background: PREV_CLOSE in the archived bhavcopy is NOT adjusted on ex-dates, so
events are inferred from the overnight price gap + persistence checks.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from qsde.ingestion import corp_actions as ca, nse_eod
from qsde.ingestion.corp_actions import adjust_prices, detect_events, snap_ratio


# ── ratio snapping ───────────────────────────────────────────────────

class TestSnap:
    @pytest.mark.parametrize("r,label,factor", [
        (0.5, "clean 1/2", 0.5),                 # 1:2 split or 1:1 bonus
        (0.2, "clean 1/5", 0.2),
        (0.1, "clean 1/10", 0.1),
        (2 / 3, "clean 2/3", 2 / 3),
        (0.75, "clean 3/4", 0.75),
    ])
    def test_common_down_ratios(self, r, label, factor):
        s = snap_ratio(r, r, 500.0)
        assert s is not None and s[2] == "SPLIT_BONUS" and s[1] == label
        assert s[0] == pytest.approx(factor, rel=1e-9)

    def test_one_to_five_split_does_not_snap_to_an_odd_bonus_ratio(self):
        # regression: ADANIPOWER's 1:5 split measured ~0.215 used to snap to a 7:2 bonus (0.222)
        s = snap_ratio(0.209, 0.240, 700.0)       # the real open/close ratios of that day
        assert s is not None and s[0] == 0.2

    @pytest.mark.parametrize("r", [0.356, 0.45, 0.55, 0.36, 0.28])
    def test_odd_ratios_are_not_forced_onto_a_common_one(self, r):
        assert snap_ratio(r, r, 500.0) is None

    def test_five_for_one_bonus_snaps(self):
        # Nykaa 2022-11-10: open ratio 0.158 against the exact 1/6
        s = snap_ratio(0.158, 0.178, 1500.0)
        assert s is not None and s[0] == pytest.approx(1 / 6) and s[1] == "clean 1/6"

    def test_penny_stock_tick_noise_is_ignored(self):
        assert snap_ratio(2.0, 2.0, 0.10) is None          # Rs0.10 -> Rs0.20 is one tick

    def test_consolidation(self):
        s = snap_ratio(10.0, 10.1, 5.0)
        assert s and s[2] == "CONSOLIDATION" and s[0] == 10.0

    @pytest.mark.parametrize("r", [0.83, 0.9, 1.0, 1.1, 1.39])
    def test_ordinary_and_mild_gaps_are_not_candidates(self, r):
        assert snap_ratio(r, r, 500.0) is None

    def test_nonfinite_is_none(self):
        assert snap_ratio(float("nan"), 0.5, 100.0) is None

    def test_noise_in_open_is_tolerated(self):
        # 1:2 split with a +3% overnight gap
        assert snap_ratio(0.515, 0.50, 500.0)[0] == 0.5


# ── detection ────────────────────────────────────────────────────────

def _series(sym: str, closes: list[float], opens: list[float] | None = None,
            start=date(2026, 1, 5), skip: set[int] | None = None) -> pd.DataFrame:
    rows, d = [], start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if not (skip and i in skip):
            o = opens[i] if opens else c
            rows.append(dict(symbol=sym, date=d, open=o, close=c, high=max(o, c) * 1.01,
                             low=min(o, c) * 0.99, volume=1000, deliv_per=50.0,
                             exchange="NSE", series="EQ"))
        d += timedelta(days=1)
    return pd.DataFrame(rows)


class TestDetect:
    def test_split_is_detected_with_evidence(self):
        ev = detect_events(_series("SPL", [100.0] * 6 + [50.0] * 6))
        assert len(ev) == 1
        e = ev.iloc[0]
        assert e.kind == "SPLIT_BONUS" and bool(e.applied) and e.price_factor == pytest.approx(0.5)
        assert e.open_ratio == pytest.approx(0.5) and e.close_prev_session == 100.0
        assert e.persistence_ratio == pytest.approx(1.0)

    def test_consolidation_detected(self):
        ev = detect_events(_series("CON", [10.0] * 6 + [100.0] * 6))
        assert len(ev) == 1 and ev.iloc[0].kind == "CONSOLIDATION" and ev.iloc[0].price_factor == 10.0

    def test_ordinary_days_produce_no_events(self):
        rng = np.random.default_rng(0)
        closes = list(100 * np.exp(np.cumsum(rng.normal(0, 0.012, 80))))
        assert detect_events(_series("CALM", closes)).empty

    def test_mild_bonus_is_a_documented_blind_spot(self):
        # 1:10 bonus -> ratio 0.909: indistinguishable from a -9% move. Not adjusted.
        assert detect_events(_series("MILD", [100.0] * 6 + [90.9] * 6)).empty

    def test_crash_that_rebounds_is_unexplained_and_not_applied(self):
        closes = [100.0] * 6 + [38.0, 95.0, 97.0, 99.0, 100.0, 100.0]
        ev = detect_events(_series("CRASH", closes))
        # the crash day and the rebound day both look like jumps; neither may be applied
        assert len(ev) >= 1 and (ev["kind"] == "UNEXPLAINED").all() and not ev["applied"].any()

    def test_intraday_collapse_after_gap_is_not_a_split(self):
        # gaps to 50 at the open then keeps falling to 20 the same day
        opens = [100.0] * 6 + [50.0] * 6
        closes = [100.0] * 6 + [20.0, 20.0, 20.0, 20.0, 20.0, 20.0]
        ev = detect_events(_series("FALL", closes, opens))
        assert ev.empty or not bool(ev.iloc[0].applied)

    def test_a_missing_prior_day_is_skipped_not_misread(self):
        # GAP is missing day 5 (e.g. rejected row). A second symbol keeps day 5 in the calendar.
        gap = _series("GAP", [100.0] * 5 + [50.0] + [50.0, 50.0], skip={5})
        cal = _series("CAL", [10.0] * 8)
        out = detect_events(pd.concat([gap, cal], ignore_index=True))
        assert out[out["symbol"] == "GAP"].empty

    def test_multiple_symbols_independent(self):
        a = _series("AAA", [100.0] * 6 + [50.0] * 6)
        b = _series("BBB", [20.0] * 12)
        ev = detect_events(pd.concat([a, b], ignore_index=True))
        assert list(ev["symbol"]) == ["AAA"]

    def test_rights_entitlement_symbols_are_excluded(self):
        ev = detect_events(_series("GODHA-RE", [100.0] * 6 + [50.0] * 6))
        assert ev.empty

    def test_penny_stock_tick_gap_is_not_a_corporate_action(self):
        # 0.10 -> 0.15 -> 0.30: the former false "consolidation" population
        assert detect_events(_series("PENNY", [0.10] * 6 + [0.20] * 6)).empty

    def test_odd_ratio_gap_is_unexplained_and_not_applied(self):
        # a persistent -64% gap whose ratio (0.36) matches no common action: a cliff, not a guess
        ev = detect_events(_series("ODD", [100.0] * 6 + [36.0] * 6))
        assert len(ev) == 1 and ev.iloc[0].kind == "UNEXPLAINED" and not bool(ev.iloc[0].applied)


# ── adjustment ───────────────────────────────────────────────────────

class TestAdjust:
    def test_prices_before_exdate_scaled_volume_inversely_after_unchanged(self):
        df = _series("SPL", [100.0] * 6 + [50.0] * 6)
        ev = detect_events(df)
        adj = adjust_prices(df, ev).set_index("date")
        pre = adj[adj.index < ev.iloc[0].ex_date]
        post = adj[adj.index >= ev.iloc[0].ex_date]
        assert np.allclose(pre["close"], 50.0) and np.allclose(post["close"], 50.0)
        assert (pre["volume"] == 2000).all() and (post["volume"] == 1000).all()
        assert (adj["close"].pct_change().abs().dropna() < 1e-9).all()      # no jump left
        assert (adj["adj_close"] == adj["close"]).all()                      # no dividend adjustment
        assert (adj["source"] == ca.ADJ_SOURCE).all()

    def test_two_events_compound(self):
        df = _series("TWO", [400.0] * 5 + [200.0] * 5 + [100.0] * 5)
        ev = detect_events(df)
        assert len(ev) == 2
        assert np.allclose(adjust_prices(df, ev)["close"], 100.0)

    def test_unapplied_events_leave_prices_untouched(self):
        df = _series("CRASH", [100.0] * 6 + [38.0, 95.0, 97.0, 99.0, 100.0, 100.0])
        adj = adjust_prices(df, detect_events(df))
        assert np.allclose(adj["close"].to_numpy(), df["close"].to_numpy())

    def test_input_frame_not_mutated(self):
        df = _series("SPL", [100.0] * 6 + [50.0] * 6)
        before = df.copy()
        adjust_prices(df, detect_events(df))
        pd.testing.assert_frame_equal(df, before)

    def test_only_the_symbol_with_an_event_changes(self):
        a = _series("AAA", [100.0] * 6 + [50.0] * 6)
        b = _series("BBB", [20.0] * 12)
        both = pd.concat([a, b], ignore_index=True)
        adj = adjust_prices(both, detect_events(both))
        assert np.allclose(adj[adj.symbol == "BBB"]["close"], 20.0)


# ── real archive: known splits ───────────────────────────────────────

ARCHIVE = nse_eod.DEFAULT_BASE


def _real_window(start: date, end: date) -> pd.DataFrame:
    frames = []
    for d, f in nse_eod.list_files(start, end, ARCHIVE):
        frames.append(nse_eod.normalize_legacy_frame(pd.read_parquet(f), d, f.name))
    raw = pd.concat(frames, ignore_index=True)
    return nse_eod.pick_primary_series(nse_eod.validate_eod(raw).valid)


@pytest.mark.skipif(not (ARCHIVE / "legacy" / "parsed").exists(), reason="local archive not present")
class TestKnownRealActions:
    def test_hdfcbank_1_to_2_split_2019_09_19(self):
        prim = _real_window(date(2019, 9, 10), date(2019, 9, 27))
        ev = detect_events(prim)
        e = ev[ev["symbol"] == "HDFCBANK"]
        assert len(e) == 1, e
        e = e.iloc[0]
        assert e.ex_date == date(2019, 9, 19) and e.kind == "SPLIT_BONUS" and bool(e.applied)
        assert e.price_factor == pytest.approx(0.5, rel=0.01)
        adj = adjust_prices(prim, ev)
        h = adj[adj["symbol"] == "HDFCBANK"].sort_values("date").set_index("date")
        r = h["close"].pct_change()
        # the 50% cliff on the ex-date is gone (a genuine +9% rally followed the next day)
        assert abs(r.loc[date(2019, 9, 19)]) < 0.03

    def test_reliance_1_to_1_bonus_2017_09_07(self):
        prim = _real_window(date(2017, 8, 28), date(2017, 9, 15))
        ev = detect_events(prim)
        e = ev[ev["symbol"] == "RELIANCE"]
        assert len(e) == 1 and e.iloc[0].ex_date == date(2017, 9, 7)
        assert e.iloc[0].price_factor == pytest.approx(0.5, rel=0.01) and bool(e.iloc[0].applied)

    def test_prev_close_is_not_adjusted_in_this_archive(self):
        """Guard for the finding that shaped the method. If this ever fails the
        archive changed and a simpler PREV_CLOSE-based detector becomes possible."""
        prim = _real_window(date(2019, 9, 17), date(2019, 9, 20)).set_index(["symbol", "date"])
        row = prim.loc[("HDFCBANK", date(2019, 9, 19))]
        prev = prim.loc[("HDFCBANK", date(2019, 9, 18))]
        assert row["prev_close"] == pytest.approx(prev["close"])          # raw, not halved
