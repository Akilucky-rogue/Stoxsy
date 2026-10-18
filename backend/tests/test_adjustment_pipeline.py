"""Reviewed adjustment pipeline: split/bonus adjusted prices, deterministic materialization, validation, dividend total return."""
import numpy as np
import pandas as pd
import pytest

from qsde.adjust import pipeline as ap

D = pd.bdate_range("2024-01-01", periods=40)


def raw(sym="AAA", cut=20, ratio=0.1, level=100.0, vol=1000.0):
    c = np.where(np.arange(len(D)) < cut, level, level * ratio)
    return pd.DataFrame({"symbol": sym, "date": D, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": vol})


def ev(sym="AAA", gap=None, factor=0.1, tier="confirmed", **kw):
    return pd.DataFrame([{"symbol": sym, "gap_date": gap if gap is not None else D[20], "factor": factor, "tier": tier,
                          "source": "ca_reconcile_v1", **kw}])


class TestSplitBonusAdjust:
    def test_bars_strictly_before_the_gap_bar_are_scaled_and_volume_divided(self):
        a = ap.split_bonus_adjust(raw(), ev())
        assert a["close"].tolist() == pytest.approx([10.0] * 40)                  # continuous after adjustment
        assert a["adj_factor"].iloc[:20].eq(0.1).all() and a["adj_factor"].iloc[20:].eq(1.0).all()
        assert a["volume"].iloc[0] == pytest.approx(10000.0) and a["volume"].iloc[25] == pytest.approx(1000.0)
        assert a["close_raw"].iloc[0] == 100.0                                      # the raw close is kept for audit

    def test_listed_factor_is_applied_not_the_observed_gap(self):
        a = ap.split_bonus_adjust(raw(ratio=0.1046), ev(factor=0.1))              # observed gap 4.6% off the listed 0.1
        assert a["adj_factor"].iloc[0] == 0.1 and a["close"].iloc[0] == pytest.approx(10.0)

    def test_no_events_is_the_identity_and_other_symbols_are_untouched(self):
        r = pd.concat([raw("AAA"), raw("BBB")])
        a = ap.split_bonus_adjust(r, ev("AAA")).set_index(["symbol", "date"])
        assert (a.loc["BBB", "adj_factor"] == 1.0).all()
        assert ap.split_bonus_adjust(raw(), pd.DataFrame(columns=["symbol", "gap_date", "factor"]))["adj_factor"].eq(1.0).all()

    def test_events_compound(self):
        r = raw(cut=30, ratio=0.05)
        e = pd.concat([ev(gap=D[10], factor=0.5), ev(gap=D[30], factor=0.1)])
        a = ap.split_bonus_adjust(r, e)
        assert a["adj_factor"].iloc[0] == pytest.approx(0.05) and a["adj_factor"].iloc[15] == pytest.approx(0.1) and a["adj_factor"].iloc[35] == 1.0

    def test_duplicates_collapse_and_the_stronger_tier_wins(self):
        e = ap.normalise_events(pd.concat([ev(tier="confirmed"), ev(tier="official_confirmed")]))
        assert len(e) == 1 and e.iloc[0].tier == "official_confirmed"
        with pytest.raises(ValueError):
            ap.normalise_events(ev(factor=-1.0))


class TestMaterialization:
    def test_same_inputs_give_the_same_version_hash_and_bytes_and_changed_inputs_do_not(self, tmp_path):
        a = ap.split_bonus_adjust(raw(), ev())
        inp = {"events": ap.events_hash(ev()), "raw": ap.raw_fingerprint(raw())}
        m1 = ap.write_materialization(a, ap.SPLIT_SCHEMA, "split_bonus", "sbadj_v1", inp, root=tmp_path / "a")
        m2 = ap.write_materialization(a, ap.SPLIT_SCHEMA, "split_bonus", "sbadj_v1", inp, root=tmp_path / "b")
        assert m1.version == m2.version and m1.output_hash == m2.output_hash
        for f1, f2 in zip(sorted(m1.path.glob("data-*.parquet")), sorted(m2.path.glob("data-*.parquet"))):
            assert f1.read_bytes() == f2.read_bytes()                                # byte-identical
        again = ap.write_materialization(a, ap.SPLIT_SCHEMA, "split_bonus", "sbadj_v1", inp, root=tmp_path / "a")
        assert again.output_hash == m1.output_hash                                   # an existing version is never rewritten
        other = {"events": ap.events_hash(ev(factor=0.2)), "raw": inp["raw"]}
        m3 = ap.write_materialization(ap.split_bonus_adjust(raw(), ev(factor=0.2)), ap.SPLIT_SCHEMA, "split_bonus", "sbadj_v1", other,
                                      root=tmp_path / "a")
        assert m3.version != m1.version and m3.output_hash != m1.output_hash

    def test_roundtrip_read(self, tmp_path):
        a = ap.split_bonus_adjust(raw(), ev())
        m = ap.write_materialization(a, ap.SPLIT_SCHEMA, "split_bonus", "sbadj_v1", {"x": 1}, root=tmp_path)
        back = ap.read_materialization(m.path, symbols=["AAA"], start="2024-01-10")
        assert len(back) == len(a[a["date"] >= "2024-01-10"]) and back["close"].round(8).tolist() == a[a["date"] >= "2024-01-10"]["close"].round(8).tolist()


class TestValidation:
    def test_a_correct_event_validates_and_a_wrong_one_leaves_a_cliff(self):
        r = raw()
        good = ap.validate_split_adjustment(r, ap.split_bonus_adjust(r, ev()), ev())
        assert good["summary"]["status"] == "validated" and good["summary"]["cliffs_raw"] == 1 and good["summary"]["cliffs_adjusted"] == 0
        bad_ev = ev(factor=0.5)                                                      # wrong factor: a 5x cliff remains
        bad = ap.validate_split_adjustment(r, ap.split_bonus_adjust(r, bad_ev), bad_ev)
        assert bad["summary"]["status"] == "failed" and bad["summary"]["events_fail_hard"] == 1

    def test_an_event_without_a_gap_bar_fails_validation(self):
        r = raw()
        e = ev(gap=D[-1] + pd.Timedelta(days=30))
        v = ap.validate_split_adjustment(r, ap.split_bonus_adjust(r, e), e)
        assert v["summary"]["events_no_gap_bar"] == 1 and v["summary"]["status"] == "failed"


class TestTotalReturn:
    def adjusted(self, with_split=False):
        r = raw(cut=40, ratio=1.0)                                                  # flat price 100 for 40 days
        return ap.split_bonus_adjust(r, ev(gap=D[30], factor=0.5) if with_split else pd.DataFrame(columns=["symbol", "gap_date", "factor"]))

    def test_dividend_return_is_amount_over_previous_close_and_is_additive(self):
        tr, info = ap.total_return_series(self.adjusted(), pd.DataFrame([{"symbol": "AAA", "ex_date": D[10], "amount": 5.0}]))
        r = tr[tr["date"] == D[10]].iloc[0]
        assert r.dividend_return_1d == pytest.approx(0.05) and r.price_return_1d == pytest.approx(0.0)
        assert r.total_return_1d == pytest.approx(r.price_return_1d + r.dividend_return_1d) and info["dividends_used"] == 1
        assert tr["total_return_index"].iloc[-1] == pytest.approx(105.0) and tr["price_return_index"].iloc[-1] == pytest.approx(100.0)

    def test_total_return_is_invariant_to_a_later_split(self):
        """Stock A: flat 50, pays Rs 2.5 on D[10]. Stock B: the same economics, but it traded at 100 until a 1:2 split at D[30]
        and paid Rs 5 on D[10] (raw, pre-split basis). After split adjustment B must have exactly A's price and total return."""
        a_raw = raw("AAA", cut=40, ratio=1.0, level=50.0)
        b_raw = raw("BBB", cut=30, ratio=0.5, level=100.0)
        adj = ap.split_bonus_adjust(pd.concat([a_raw, b_raw]), ev("BBB", gap=D[30], factor=0.5))
        div = pd.DataFrame([{"symbol": "AAA", "ex_date": D[10], "amount": 2.5}, {"symbol": "BBB", "ex_date": D[10], "amount": 5.0}])
        tr, _ = ap.total_return_series(adj, div)
        a, b = (tr[tr["symbol"] == s_].reset_index(drop=True) for s_ in ("AAA", "BBB"))
        for c in ("price_return_1d", "dividend_return_1d", "total_return_1d", "total_return_index"):
            assert np.allclose(a[c].fillna(0), b[c].fillna(0), atol=1e-12), c
        assert b["total_return_index"].iloc[-1] == pytest.approx(105.0)

    def test_non_trading_ex_date_attaches_to_the_next_bar_and_bad_dividends_are_skipped_and_counted(self):
        r = raw(cut=40, ratio=1.0)
        r = r[r["date"] != D[10]]
        adj = ap.split_bonus_adjust(r, pd.DataFrame(columns=["symbol", "gap_date", "factor"]))
        div = pd.DataFrame([{"symbol": "AAA", "ex_date": D[10], "amount": 5.0},                    # no bar that day -> next bar
                            {"symbol": "AAA", "ex_date": D[20], "amount": 60.0},                   # 60% yield: skipped
                            {"symbol": "AAA", "ex_date": D[39] + pd.Timedelta(days=30), "amount": 1.0},   # after the last bar
                            {"symbol": "ZZZ", "ex_date": D[5], "amount": 1.0}])                     # unknown symbol
        tr, info = ap.total_return_series(adj, div)
        assert tr[tr["date"] == D[11]].iloc[0].dividend_return_1d == pytest.approx(0.05)
        assert info["dividends_used"] == 1 and info["dividends_skipped"]["yield_too_high"] == 1 and info["dividends_skipped"]["no_bar"] == 2

    def test_same_day_dividends_are_summed(self):
        div = pd.DataFrame([{"symbol": "AAA", "ex_date": D[10], "amount": 3.0}, {"symbol": "AAA", "ex_date": D[10], "amount": 2.0}])
        tr, _ = ap.total_return_series(self.adjusted(), div)
        assert tr[tr["date"] == D[10]].iloc[0].dividend_return_1d == pytest.approx(0.05)
