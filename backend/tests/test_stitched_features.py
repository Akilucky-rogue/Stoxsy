"""Stitching a renamed stock's history under a security id (factor version ffd_v2s), without touching ffd_v2."""
import numpy as np
import pandas as pd
import pytest

from qsde.factors import engine
from qsde.factors.versions import FFD_STITCHED_VERSION, FFD_VERSION
from qsde.ingestion.security_master import SecurityMap

D = pd.bdate_range("2022-01-03", periods=1100)


def _series(n0, n1, level=100.0, seed=0):
    rng = np.random.default_rng(seed + n0)
    idx = D[n0:n1]
    c = level * np.exp(np.cumsum(rng.normal(0, 0.01, len(idx))))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=idx)


def _smap():
    iv = pd.DataFrame([("S:OLD", "OLD", D[0], D[699]), ("S:OLD", "NEW", D[700], pd.Timestamp("9999-12-31"))],
                      columns=["security_id", "symbol", "valid_from", "valid_to"])
    return SecurityMap(iv)


@pytest.fixture
def loader(monkeypatch):
    old = _series(0, 700)
    new = _series(700, 1100, level=float(old["close"].iloc[-1]), seed=1)
    bars = {"OLD": old, "NEW": new}
    monkeypatch.setattr(engine, "load_ohlcv", lambda sym, start="2006-01-01", end=None: bars[sym])
    return bars


def test_stitched_series_is_continuous_and_covers_both_symbols(loader):
    s = engine.stitched_ohlcv("S:OLD", _smap())
    assert len(s) == 1100 and s.index.is_monotonic_increasing and "_sym" not in s.columns
    assert abs(s["close"].iloc[700] / s["close"].iloc[699] - 1) < 0.05


def test_events_after_the_rename_rescale_the_earlier_segment(loader):
    ev = pd.DataFrame([{"symbol": "NEW", "ex_date": D[900], "price_factor": 0.5, "applied": True}])
    s = engine.stitched_ohlcv("S:OLD", _smap(), ev)
    raw_old = loader["OLD"]["close"]
    assert np.allclose(s["close"].iloc[:700].to_numpy(), raw_old.to_numpy() * 0.5)          # old segment follows the later split
    # NEW's own bars are already adjusted for its own events (ohlcv is), so they are NOT rescaled again
    assert np.allclose(s["close"].iloc[700:].to_numpy(), loader["NEW"]["close"].to_numpy())


def test_relabel_gives_each_row_the_symbol_that_traded(loader):
    s = engine.stitched_ohlcv("S:OLD", _smap())
    f = s.assign(symbol="S:OLD", x=1.0)
    out = engine.relabel_to_symbol_of_day(f, _smap(), {"S:OLD"})
    assert (out["symbol"].iloc[:700] == "OLD").all() and (out["symbol"].iloc[700:] == "NEW").all()


def test_panel_uses_one_fracdiff_series_across_the_rename_and_a_separate_version(loader, monkeypatch):
    empty = lambda *a, **k: pd.DataFrame(index=D)   # noqa: E731
    for n in ("compute_all_fundamental", "compute_all_flow", "compute_all_macro", "compute_all_sentiment"):
        monkeypatch.setattr(engine, n, empty)
    combined, to_write, version = engine.compute_ffd_panel(["OLD", "NEW"], security_map=_smap())
    assert version == FFD_STITCHED_VERSION != FFD_VERSION
    syms = to_write["symbol"] if "symbol" in to_write else None
    assert set(syms.unique()) == {"OLD", "NEW"}
    # the stitched series carries its history across the rename: a 12-month feature is defined from the first NEW row, whereas
    # computed on NEW alone (400 bars) the first 252 rows would still be warm-up NaN
    new_rows = to_write[to_write["symbol"] == "NEW"].sort_index()
    assert "tech_mom_12m" in new_rows.columns and new_rows["tech_mom_12m"].iloc[:5].notna().all()
    _, plain, v2 = engine.compute_ffd_panel(["NEW"])
    assert v2 == FFD_VERSION
    assert plain.empty or plain["tech_mom_12m"].iloc[:200].isna().all()
