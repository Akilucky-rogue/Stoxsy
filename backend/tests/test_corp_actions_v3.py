"""Security map (official renames + hand-off check) and the v3 candidate evaluator."""
import numpy as np
import pandas as pd
import pytest

from qsde.ingestion.corp_actions_v3 import evaluate
from qsde.ingestion.security_master import build_security_map

D = pd.bdate_range("2024-01-01", periods=80)


def bars(sym, start, end, close):
    d = D[(D >= start) & (D <= end)]
    c = np.array(close(len(d)), dtype=float)
    return pd.DataFrame({"symbol": sym, "date": d, "open": c, "close": c})


def one_to_ten(n, at):
    return np.where(np.arange(n) < at, 100.0, 10.0)


def first_last(df):
    return df.groupby("symbol")["date"].agg(first_bar="min", last_bar="max").reset_index()


class TestSecurityMap:
    def _setup(self, new_first_offset=1, overlap=False):
        old = bars("OLDCO", D[0], D[39], lambda n: [100] * n)
        new = bars("NEWCO", D[39 + new_first_offset - (5 if overlap else 0)], D[-1], lambda n: [100] * n)
        official = pd.DataFrame([{"old_symbol": "OLDCO", "new_symbol": "NEWCO", "effective_date": D[40]}])
        allb = pd.concat([old, new])
        return build_security_map(official, first_last(allb), D.to_numpy()), allb

    def test_clean_handoff_is_stitched_under_a_stable_id(self):
        m, _ = self._setup()
        assert m.security_of("OLDCO") == m.security_of("NEWCO") == "S:OLDCO"
        assert m.symbol_on("NEWCO", D[10]) == "OLDCO" and m.symbol_on("NEWCO", D[60]) == "NEWCO"
        assert m.symbol_on("OLDCO", D[60]) == "NEWCO" and m.symbol_on("UNRELATED", D[10]) == "UNRELATED"

    def test_overlapping_series_are_not_a_rename(self):
        m, _ = self._setup(overlap=True)
        assert m.security_of("OLDCO") is None and "overlap" in m.rejected.iloc[0]["reason"]

    def test_chain_follows_both_renames_and_keeps_the_first_symbol_as_id(self):
        a, b_, c = bars("A", D[0], D[19], lambda n: [1] * n), bars("B", D[20], D[39], lambda n: [1] * n), bars("C", D[40], D[-1], lambda n: [1] * n)
        off = pd.DataFrame([{"old_symbol": "A", "new_symbol": "B", "effective_date": D[20]},
                            {"old_symbol": "B", "new_symbol": "C", "effective_date": D[40]}])
        m = build_security_map(off, first_last(pd.concat([a, b_, c])), D.to_numpy())
        assert {m.security_of(s) for s in "ABC"} == {"S:A"} and m.symbol_on("C", D[5]) == "A" and m.symbol_on("A", D[30]) == "B"


def listing(sym, ex, purpose="Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"):
    return pd.DataFrame([{"listed_symbol": sym, "listed_ex_date": ex, "purpose": purpose}])


class TestEvaluate:
    def test_clean_split_within_bounds_is_a_candidate(self):
        px = bars("X", D[0], D[-1], lambda n: one_to_ten(n, 40))
        r = evaluate(listing("X", D[41]), px).iloc[0]            # listed 1 session after the real gap
        assert r.classification == "v3_candidate" and pd.Timestamp(r.gap_date) == D[40] and r.shift_sessions == -1

    def test_rename_is_resolved_to_the_symbol_that_traded(self):
        old = bars("OLDCO", D[0], D[49], lambda n: one_to_ten(n, 30))
        new = bars("NEWCO", D[50], D[-1], lambda n: [10] * n)
        off = pd.DataFrame([{"old_symbol": "OLDCO", "new_symbol": "NEWCO", "effective_date": D[50]}])
        px = pd.concat([old, new])
        m = build_security_map(off, first_last(px), D.to_numpy())
        r = evaluate(listing("NEWCO", D[30]), px, m).iloc[0]
        assert r.symbol_of_day == "OLDCO" and r.classification == "v3_candidate" and r.security_id == "S:OLDCO"
        no_map = evaluate(listing("NEWCO", D[30]), px).iloc[0]
        assert no_map.classification == "unresolved"             # without the map there are no bars for NEWCO there

    def test_ndrauto_pattern_is_rejected_gap_far_from_listing_and_factor_off(self):
        c = np.where(np.arange(len(D)) < 60, 100.0, 46.1)        # gap 0.461 vs 0.5, 20 sessions after the listing
        px = pd.DataFrame({"symbol": "N", "date": D, "open": c, "close": c})
        px = px[(px.date < D[40]) | (px.date >= D[60])]           # the stock does not trade in between
        r = evaluate(listing("N", D[40], "Bonus 1:1"), px).iloc[0]
        assert r.classification == "v3_rejected" and "factor error" in r.reason

    def test_long_halt_is_rejected(self):
        c = one_to_ten(len(D), 40)
        px = pd.DataFrame({"symbol": "H", "date": D, "open": c, "close": c})
        px = px[(px.date < D[33]) | (px.date >= D[40])]           # 7 untraded sessions before the gap bar
        mkt = pd.DataFrame({"symbol": "MKT", "date": D, "open": 100.0, "close": 100.0})    # calendar comes from ALL symbols
        r = evaluate(listing("H", D[40]), pd.concat([px, mkt])).iloc[0]
        assert r.classification == "v3_rejected" and "untraded" in r.reason

    def test_no_price_reversal_no_candidate(self):
        c = np.where(np.arange(len(D)) == 40, 10.0, 100.0)        # one-day dip, then back: not a split
        px = pd.DataFrame({"symbol": "R", "date": D, "open": c, "close": c})
        r = evaluate(listing("R", D[40]), px).iloc[0]
        assert r.classification == "v3_rejected" and "persist" in r.reason

    def test_unparseable_purpose_is_unresolved(self):
        px = bars("U", D[0], D[-1], lambda n: [100] * n)
        assert evaluate(listing("U", D[40], "Demerger"), px).iloc[0].classification == "unresolved"


class TestHandoffPriceGate:
    def test_a_rename_that_coincides_with_a_split_is_not_stitched(self):
        old = bars("OLDCO", D[0], D[39], lambda n: [100] * n)
        new = bars("NEWCO", D[40], D[-1], lambda n: [20] * n)               # 1:5 split on the rename date
        off = pd.DataFrame([{"old_symbol": "OLDCO", "new_symbol": "NEWCO", "effective_date": D[40]}])
        fl = first_last(pd.concat([old, new]))
        fl["first_open"] = [100.0, 20.0]
        fl["last_close"] = [100.0, 20.0]
        m = build_security_map(off, fl, D.to_numpy())
        assert m.security_of("OLDCO") is None and "price discontinuity" in m.rejected.iloc[0]["reason"]

    def test_a_clean_rename_still_stitches_with_prices_supplied(self):
        old = bars("OLDCO", D[0], D[39], lambda n: [100] * n)
        new = bars("NEWCO", D[40], D[-1], lambda n: [101] * n)
        off = pd.DataFrame([{"old_symbol": "OLDCO", "new_symbol": "NEWCO", "effective_date": D[40]}])
        fl = first_last(pd.concat([old, new]))
        fl["first_open"] = [100.0, 101.0]
        fl["last_close"] = [100.0, 101.0]
        assert build_security_map(off, fl, D.to_numpy()).security_of("NEWCO") == "S:OLDCO"


class TestHandoffGateUsesTheRescaledSeries:
    def _fl(self, old_close, new_open):
        old = bars("OLDCO", D[0], D[39], lambda n: [100] * n)
        new = bars("NEWCO", D[40], D[-1], lambda n: [new_open] * n)
        fl = first_last(pd.concat([old, new]))
        fl["first_open"] = fl["symbol"].map({"OLDCO": 100.0, "NEWCO": float(new_open)})
        fl["last_close"] = fl["symbol"].map({"OLDCO": float(old_close), "NEWCO": float(new_open)})
        off = pd.DataFrame([{"old_symbol": "OLDCO", "new_symbol": "NEWCO", "effective_date": D[40]}])
        return off, fl

    def test_a_later_split_of_the_new_symbol_is_not_mistaken_for_a_cliff_at_the_rename(self):
        off, fl = self._fl(old_close=100, new_open=20)                         # new symbol's bars are on the post-1:5-split basis
        ev = pd.DataFrame([{"symbol": "NEWCO", "ex_date": D[70], "price_factor": 0.2, "applied": True}])
        assert build_security_map(off, fl, D.to_numpy(), ev).security_of("NEWCO") == "S:OLDCO"
        assert build_security_map(off, fl, D.to_numpy()).security_of("NEWCO") is None      # without the event it still looks like a cliff

    def test_a_split_that_really_coincides_with_the_rename_is_still_refused(self):
        off, fl = self._fl(old_close=100, new_open=20)
        unrelated = pd.DataFrame([{"symbol": "OTHER", "ex_date": D[70], "price_factor": 0.2, "applied": True}])
        assert build_security_map(off, fl, D.to_numpy(), unrelated).security_of("NEWCO") is None
