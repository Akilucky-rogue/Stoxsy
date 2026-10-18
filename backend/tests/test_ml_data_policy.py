"""Official vs research corporate-action price policy (qsde.ml.data)."""
import pandas as pd
import pytest

from qsde.ml import data as mld


def _px():
    d = pd.date_range("2024-01-01", periods=6, freq="B")
    return pd.DataFrame({"symbol": "X", "date": d, "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.0, "volume": 1000})


def test_extra_event_scales_only_sessions_before_ex_date():
    px = _px()
    ev = pd.DataFrame({"symbol": ["X"], "ex_date": [px["date"].iloc[3]], "price_factor": [0.5]})
    out = mld.apply_extra_events(px, ev)
    assert out["close"].tolist() == [50.0, 50.0, 50.0, 100.0, 100.0, 100.0]
    assert out["volume"].iloc[0] == 2000 and out["volume"].iloc[5] == 1000


def test_no_events_is_identity_and_input_untouched():
    px = _px()
    assert mld.apply_extra_events(px, pd.DataFrame(columns=["symbol", "ex_date", "price_factor"])).equals(px)
    mld.apply_extra_events(px, pd.DataFrame({"symbol": ["X"], "ex_date": [px["date"].iloc[2]], "price_factor": [0.1]}))
    assert px["close"].eq(100.0).all()


def test_unknown_policy_rejected():
    with pytest.raises(ValueError):
        mld.load_prices(["X"], policy="whatever")


def test_promotion_policy_is_official():
    assert mld.PROMOTION_POLICY == mld.OFFICIAL


def test_nse_candidates_not_applied_to_prices_become_label_exclusions(monkeypatch):
    """Without this a -50% / -90% split cliff in the raw series would be trained on as a real return."""
    import pandas as pd

    from qsde.ml import data as mld
    calls = []

    def fake_read_sql(sql, params=None, **k):
        calls.append(sql)
        if "nse_corporate_actions" in sql:
            return pd.DataFrame({"symbol": ["SPLITCO"], "ex_date": [pd.Timestamp("2026-06-03").date()]})
        return pd.DataFrame({"symbol": ["UNEXP"], "ex_date": [pd.Timestamp("2025-01-02").date()]})

    import qsde.db.connection as c
    monkeypatch.setattr(c, "read_sql", fake_read_sql)
    off = mld.label_exclusion_events(["SPLITCO", "UNEXP"], mld.OFFICIAL)
    assert set(off["symbol"]) == {"SPLITCO", "UNEXP"}
    assert "NOT EXISTS" in [q for q in calls if "nse_corporate_actions" in q][0]      # only those NOT already applied to prices
