"""NSE corporate-actions source: text parsing, price confirmation, merge with the price detector."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from qsde.ingestion import corp_actions as ca, corp_actions_source as cas, nse_eod


class TestParsePurpose:
    @pytest.mark.parametrize("text,cat,factor", [
        ("Bonus 1:1", "SPLIT_BONUS", 0.5),
        ("Bonus 5:1", "SPLIT_BONUS", 1 / 6),
        ("Bonus 1:4", "SPLIT_BONUS", 0.8),
        ("Bonus 3:2", "SPLIT_BONUS", 0.4),
        ("Bonus Issue 2 : 1", "SPLIT_BONUS", 1 / 3),
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", "SPLIT_BONUS", 0.2),
        ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "SPLIT_BONUS", 0.1),
        ("Face Value Split From Rs 2 To Re 1", "SPLIT_BONUS", 0.5),
        ("Face Value Split From Rs.10/- To Rs.5/-", "SPLIT_BONUS", 0.5),
        ("Sub-Division From Rs 5/- Per Share To Rs 1/- Per Share", "SPLIT_BONUS", 0.2),
        ("Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", "SPLIT_BONUS", 0.25),
        ("Annual General Meeting / Dividend - Re 1/- Per Share / Bonus 1 : 2", "SPLIT_BONUS", 2 / 3),
        ("Face Value Consolidation From Rs 1/- To Rs 10/-", "CONSOLIDATION", 10.0),
    ])
    def test_equity_splits_bonuses_and_combinations(self, text, cat, factor):
        p = cas.parse_purpose(text)
        assert p["category"] == cat and p["factor"] == pytest.approx(factor)

    @pytest.mark.parametrize("text,cat", [
        ("Demerger", "DEMERGER"), ("Scheme Of Arrangement", "DEMERGER"), ("Scheme Of Demerger", "DEMERGER"),
        ("Composite Scheme Of Arrangement", "DEMERGER"), ("Capital Reduction", "DEMERGER"),
        ("Scheme Of Arangement- Bonus - 1 Debenture For 1 Equity Share Held", "DEMERGER"),
        ("Bonus Preference Shares 1:1", "OTHER"), ("Bonus Ncrps 1:2", "OTHER"),
        ("Interim Dividend Re.0.10 Per Share", "OTHER"), ("Rights 91:10 @ Premium Rs.10/- Per Share", "OTHER"),
        ("Annual General Meeting", "OTHER"), ("", "OTHER"), (None, "OTHER"),
    ])
    def test_things_that_are_not_adjustable_equity_ratios(self, text, cat):
        p = cas.parse_purpose(text)
        assert p["category"] == cat and p["factor"] is None

    def test_absurd_factor_is_rejected(self):
        assert cas.parse_purpose("Face Value Split From Rs 1 To Rs 100000")["factor"] is None


def _bars(sym, closes, opens=None, start=date(2026, 1, 5)):
    rows, d = [], start
    for i, c in enumerate(closes):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        o = opens[i] if opens else c
        rows.append(dict(symbol=sym, date=d, open=o, close=c, high=max(o, c) * 1.01, low=min(o, c) * 0.99,
                         volume=1000, deliv_per=50.0, exchange="NSE", series="EQ"))
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def _src(sym, ex, purpose):
    p = cas.parse_purpose(purpose)
    return pd.DataFrame([dict(symbol=sym, ex_date=pd.Timestamp(ex), category=p["category"], factor=p["factor"],
                              purpose=purpose, detail="; ".join(x[2] for x in p["parts"]), src_type="x")])


class TestReconcile:
    def test_listed_split_with_matching_gap_is_confirmed_and_applied(self):
        px = _bars("SPL", [100.0] * 8 + [20.0] * 8)
        ex = px["date"].iloc[8]
        ev = cas.reconcile(_src("SPL", ex, "Face Value Split From Rs 10 To Rs 2"), px)
        assert len(ev) == 1 and ev.iloc[0].kind == "SPLIT_BONUS" and bool(ev.iloc[0].applied)
        assert ev.iloc[0].price_factor == pytest.approx(0.2) and ev.iloc[0].ex_date == ex
        assert ev.iloc[0].source == "nse_corp_actions" and ev.iloc[0].ex_date_shift == 0

    def test_listed_date_off_by_one_session_is_corrected_to_the_gap_session(self):
        px = _bars("SPL", [100.0] * 8 + [20.0] * 8)
        listed = px["date"].iloc[9]                                   # source says a day AFTER the real gap
        ev = cas.reconcile(_src("SPL", listed, "Bonus 1:1"), _bars("SPL", [100.0] * 8 + [50.0] * 8))
        assert ev.iloc[0].ex_date == _bars("SPL", [1] * 9)["date"].iloc[8] and ev.iloc[0].ex_date_shift == -1

    def test_listed_split_without_a_price_gap_is_unconfirmed_and_not_applied(self):
        px = _bars("FLAT", [100.0] * 16)
        ev = cas.reconcile(_src("FLAT", px["date"].iloc[8], "Bonus 1:1"), px)
        assert ev.iloc[0].kind == "SOURCE_UNCONFIRMED" and not bool(ev.iloc[0].applied)
        assert np.isnan(ev.iloc[0].price_factor)

    def test_mild_bonus_1_to_4_is_applied_when_source_and_gap_agree(self):
        px = _bars("MILD", [100.0] * 8 + [80.0] * 8)
        ev = cas.reconcile(_src("MILD", px["date"].iloc[8], "Bonus 1:4"), px)
        assert ev.iloc[0].kind == "SPLIT_BONUS" and ev.iloc[0].price_factor == pytest.approx(0.8)

    def test_wrong_ratio_in_prices_is_not_confirmed(self):
        px = _bars("X", [100.0] * 8 + [70.0] * 8)                      # 0.70 gap vs a listed 1:1 bonus (0.5)
        ev = cas.reconcile(_src("X", px["date"].iloc[8], "Bonus 1:1"), px)
        assert ev.iloc[0].kind == "SOURCE_UNCONFIRMED"

    def test_demergers_and_dividends_produce_no_adjustment_events(self):
        px = _bars("D", [100.0] * 8 + [60.0] * 8)
        ex = px["date"].iloc[8]
        src = pd.concat([_src("D", ex, "Demerger"), _src("D", ex, "Interim Dividend Rs 5 Per Share")])
        assert cas.reconcile(src, px).empty

    def test_consolidation_confirmed(self):
        px = _bars("C", [5.0] * 8 + [50.0] * 8)
        ev = cas.reconcile(_src("C", px["date"].iloc[8], "Face Value Consolidation From Rs 1 To Rs 10"), px)
        assert ev.iloc[0].kind == "CONSOLIDATION" and ev.iloc[0].price_factor == pytest.approx(10.0)


class TestPendingAndServingGuard:
    def test_gap_in_the_last_sessions_is_pending_until_the_level_has_held(self):
        # split gap on the second to last session: only 1 later session exists -> NOT applied yet
        px = _bars("NEW", [100.0] * 10 + [50.0] * 2)
        ev = ca.detect_events(px)
        assert len(ev) == 1 and ev.iloc[0].kind == "UNEXPLAINED" and not bool(ev.iloc[0].applied)
        # the same gap with 3 later sessions confirms
        ev3 = ca.detect_events(_bars("NEW", [100.0] * 10 + [50.0] * 4))
        assert bool(ev3.iloc[0].applied) and ev3.iloc[0].price_factor == pytest.approx(0.5)

    def test_one_day_crash_on_the_final_session_is_never_adjusted_away(self):
        ev = ca.detect_events(_bars("CRASHDAY", [100.0] * 12 + [50.0]))
        assert ev.empty or not ev["applied"].any()

    def test_recent_gap_guard_flags_unapplied_large_gaps_only(self):
        px = pd.concat([_bars("GAPPY", [100.0] * 8 + [50.0] * 2), _bars("CALM", [100.0] * 10),
                        _bars("SPLITOK", [100.0] * 8 + [50.0] * 2)], ignore_index=True)
        last2 = sorted(px["date"].unique())[8]
        applied = pd.DataFrame({"symbol": ["SPLITOK"], "ex_date": [last2]})
        flagged = ca.flag_recent_gaps(px, applied)
        assert flagged == ["GAPPY"]
        # a gap older than 3 sessions no longer flags (the pending window has passed)
        old = pd.concat([_bars("OLDGAP", [100.0] * 3 + [50.0] * 9)], ignore_index=True)
        assert ca.flag_recent_gaps(old, applied) == []


class TestSameDayListings:
    def test_two_actions_on_one_ex_date_multiply_into_one_event(self, tmp_path):
        p = tmp_path / "ca.parquet"
        pd.DataFrame({"symbol": ["TWO", "TWO", "ONE"], "ex_date": pd.to_datetime(["2024-03-04"] * 2 + ["2024-03-05"]),
                      "action_type": ["bonus", "unsupported", "bonus"],
                      "ratio_old": 1.0, "ratio_new": 1.0, "adjustment_factor": 1.0,
                      "raw_purpose": ["Bonus 1:1", "Face Value Split From Rs 10 To Rs 5", "Bonus 1:1"]}).to_parquet(p)
        src = cas.load_source(p)
        two = src[src["symbol"] == "TWO"]
        assert len(two) == 1 and two.iloc[0].factor == pytest.approx(0.25) and two.iloc[0].category == "SPLIT_BONUS"
        assert len(src[src["symbol"] == "ONE"]) == 1

    def test_reconcile_never_returns_two_events_for_one_symbol_session(self):
        px = _bars("DUP", [100.0] * 8 + [50.0] * 8)
        d = px["date"]
        src = pd.concat([_src("DUP", d.iloc[8], "Bonus 1:1"), _src("DUP", d.iloc[9], "Bonus 1:1")])
        ev = cas.reconcile(src, px)
        assert not ev.duplicated(["symbol", "ex_date"]).any()


class TestCircuitLimitIsNotABonus:
    def test_a_20_percent_lower_circuit_without_a_source_row_is_never_adjusted(self):
        px = _bars("CIRC", [100.0] * 8 + [80.0] * 8)                   # exactly 0.8, persists
        assert ca.detect_events(px).empty                              # below the detector's gap trigger
        assert ca.GAP_DOWN_MAX < 0.80
        assert ca.snap_ratio(0.80, 0.80, 500.0) is None
        assert 0.8 not in [round(x, 4) for x in ca.COMMON_DOWN]

    def test_a_bigger_unlisted_gap_is_still_inferred_from_prices(self):
        px = _bars("BIG", [100.0] * 8 + [50.0] * 8)
        ev = ca.detect_events(px)
        assert len(ev) == 1 and bool(ev.iloc[0].applied) and ev.iloc[0].source == "price_inferred"


class TestCombine:
    @staticmethod
    def _inf(sym, ex, factor=0.5, applied=True):
        return pd.DataFrame([dict(exchange="NSE", symbol=sym, ex_date=ex, prev_session=ex, close_prev_session=100.0,
                                  open_ex_date=50.0, close_ex_date=50.0, open_ratio=0.5, close_ratio=0.5,
                                  volume_ratio=np.nan, persistence_ratio=1.0,
                                  kind="SPLIT_BONUS" if applied else "UNEXPLAINED", snapped_ratio="clean 1/2",
                                  price_factor=factor if applied else np.nan, applied=applied, method=ca.METHOD,
                                  source="price_inferred", source_purpose=None, ex_date_shift=0)])

    def test_inferred_event_next_to_a_source_split_is_dropped(self):
        px = _bars("A", [100.0] * 8 + [50.0] * 8)
        ex = px["date"].iloc[8]
        conf = cas.reconcile(_src("A", ex, "Bonus 1:1"), px)
        src_all = _src("A", ex, "Bonus 1:1")
        out = ca.combine_events(conf, self._inf("A", ex), src_all, px["date"].unique())
        assert len(out) == 1 and out.iloc[0].source == "nse_corp_actions"

    def test_inferred_event_next_to_a_demerger_becomes_unexplained(self):
        px = _bars("B", [100.0] * 8 + [50.0] * 8)
        ex = px["date"].iloc[8]
        src_all = _src("B", ex, "Demerger")
        out = ca.combine_events(pd.DataFrame(columns=ca.EVENT_COLS_ALL), self._inf("B", ex), src_all, px["date"].unique())
        assert len(out) == 1 and out.iloc[0].kind == "UNEXPLAINED" and not bool(out.iloc[0].applied)

    def test_unrelated_inferred_event_survives(self):
        px = _bars("C", [100.0] * 8 + [50.0] * 8)
        out = ca.combine_events(pd.DataFrame(columns=ca.EVENT_COLS_ALL), self._inf("C", px["date"].iloc[8]),
                                _src("OTHERSYM", px["date"].iloc[8], "Bonus 1:1"), px["date"].unique())
        assert len(out) == 1 and bool(out.iloc[0].applied)


ARCHIVE = nse_eod.DEFAULT_BASE


def _real(sym, a, b):
    frames = []
    for d, f in nse_eod.list_files(a, b, ARCHIVE):
        frames.append(nse_eod.normalize_legacy_frame(pd.read_parquet(f), d, f.name))
    raw = pd.concat(frames, ignore_index=True)
    prim = nse_eod.pick_primary_series(nse_eod.validate_eod(raw).valid)
    return prim[prim["symbol"] == sym]


@pytest.mark.skipif(not (ARCHIVE / "legacy" / "parsed").exists() or not cas.SOURCE_FILE.exists(),
                    reason="local archive / corporate-actions source not present")
class TestRealSource:
    @pytest.mark.parametrize("sym,ex,a,b,factor", [
        ("HDFCBANK", date(2019, 9, 19), date(2019, 9, 5), date(2019, 10, 3), 0.5),
        ("ADANIPOWER", date(2025, 9, 22), date(2025, 9, 8), date(2025, 10, 6), 0.2),
        ("IRCTC", date(2021, 10, 28), date(2021, 10, 14), date(2021, 11, 11), 0.2),
        ("EICHERMOT", date(2020, 8, 24), date(2020, 8, 10), date(2020, 9, 8), 0.1),
        ("NYKAA", date(2022, 11, 10), date(2022, 10, 27), date(2022, 11, 25), 1 / 6),
        ("RELIANCE", date(2017, 9, 7), date(2017, 8, 24), date(2017, 9, 22), 0.5),
    ])
    def test_known_splits_and_bonuses_are_source_confirmed_with_exact_factors(self, sym, ex, a, b, factor):
        px = _real(sym, a, b)
        src = cas.load_source()
        src = src[(src["symbol"] == sym) & src["ex_date"].between(pd.Timestamp(a), pd.Timestamp(b))]
        ev = cas.reconcile(src, px)
        row = ev[ev["kind"].isin(["SPLIT_BONUS", "CONSOLIDATION"])]
        assert len(row) == 1, ev
        assert row.iloc[0].ex_date == ex and bool(row.iloc[0].applied)
        assert row.iloc[0].price_factor == pytest.approx(factor, rel=1e-9)

    def test_source_covers_what_the_old_parser_missed(self):
        src = cas.load_source()
        # the old parser tagged only 6 rows 'split'; re-parsing the text finds hundreds
        assert (src["category"] == "SPLIT_BONUS").sum() > 800
        assert src[(src["symbol"] == "HDFCBANK") & (src["ex_date"] == "2019-09-19")]["factor"].iloc[0] == 0.5
