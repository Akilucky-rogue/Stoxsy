"""Phase 1 regression tests: training-pipeline repairs.

Each class pins one defect found in the pre-consolidation audit. Hermetic
(synthetic data, monkeypatched DB) except `TestFactorPitSqlOnLiveDb`, which
skips when no Postgres is reachable.

Defects covered
  1. dataset.py factor_pit SQL had `AND` after `ORDER BY` (invalid SQL).
  2. retrain.py fractionally-differenced features that were already
     differenced at ingest (double application).
  3. engine.py differenced only the persisted tail slice, so the kernel
     (auto-widened for the short slice) differed from the full-history
     training kernel -> train/serve skew.
  4. dataset.py gave the triple-barrier labeler close-only OHLCV (H4).
  5. Feature parity: a factor value at date T must not depend on data after T,
     and train / serve readers must reshape factor rows identically.
"""
from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BACKEND = Path(__file__).resolve().parents[1]


# ── helpers ──────────────────────────────────────────────────────────

def _pg_parse(sql: str) -> None:
    """Raise if `sql` is not valid PostgreSQL. :name binds -> $1 first."""
    import pglast
    pglast.parse_sql(re.sub(r"(?<!:):([A-Za-z_]\w*)", "$1", sql))


def _walk(n: int, seed: int, start: str = "2019-01-01") -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(2e-4, 0.015, n)))
    spread = np.abs(rng.normal(0.006, 0.002, n))
    idx = pd.bdate_range(start, periods=n)
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.002, n)),
        "high": close * (1 + spread),
        "low": close * (1 - spread),
        "close": close,
        "volume": rng.integers(1_000_000, 5_000_000, n).astype(float),
    }, index=idx.rename("date"))


# ── 1. SQL ───────────────────────────────────────────────────────────

class TestFactorPitSql:
    def test_training_query_is_valid_postgres(self):
        from qsde.models.feature_view import FACTOR_PIT_TRAIN_SQL
        _pg_parse(FACTOR_PIT_TRAIN_SQL)

    def test_serve_query_is_valid_postgres(self):
        from qsde.models.feature_view import FACTOR_PIT_SERVE_SQL
        _pg_parse(FACTOR_PIT_SERVE_SQL)

    def test_train_and_serve_share_the_same_version_constant(self):
        from qsde.factors.versions import FFD_VERSION
        from qsde.models import dataset, signal_generator
        assert dataset.build_training_dataset.__defaults__ is not None
        import inspect
        sig = inspect.signature(dataset.build_training_dataset)
        assert sig.parameters["factor_version"].default == FFD_VERSION
        assert "FFD_VERSION" in Path(signal_generator.__file__).read_text()


def _db_up() -> bool:
    try:
        from qsde.db.connection import read_sql
        read_sql("SELECT 1 AS x")
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _db_up(), reason="no Postgres reachable")
class TestFactorPitSqlOnLiveDb:
    def test_training_query_plans_on_real_postgres(self):
        from qsde.db.connection import read_sql
        from qsde.models.feature_view import FACTOR_PIT_TRAIN_SQL
        sql = "EXPLAIN " + FACTOR_PIT_TRAIN_SQL
        df = read_sql(sql, params={"s": "2024-01-01", "e": "2024-12-31", "fv": "ffd_v2"})
        assert len(df) > 0


# ── 2. double fracdiff ───────────────────────────────────────────────

def _load_retrain():
    spec = importlib.util.spec_from_file_location("retrain_script", BACKEND / "scripts" / "retrain.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestNoDoubleFracdiff:
    def test_dataset_refuses_to_difference_already_differenced_rows(self):
        from qsde.factors.versions import FFD_VERSION
        from qsde.models.dataset import build_training_dataset
        with pytest.raises(ValueError, match="already"):
            build_training_dataset(horizon="swing", apply_fracdiff=True,
                                   factor_version=FFD_VERSION)

    def test_retrain_default_does_not_request_fracdiff(self):
        mod = _load_retrain()
        args = argparse.Namespace(start_date="2018-01-01", label_method="triple_barrier",
                                  no_fracdiff=False)
        kw = mod.dataset_kwargs(args, "swing")
        assert kw["apply_fracdiff"] is False
        assert kw["horizon"] == "swing"


# ── 3. tail-only fracdiff / kernel skew ──────────────────────────────

def _reference_ffd(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    n, width = len(x), len(w)
    out = np.full(n, np.nan)
    for i in range(width - 1, n):
        win = x[i - width + 1: i + 1]
        if not np.isnan(win).any():
            out[i] = float(np.dot(w, win))
    return out


class TestFracdiffKernel:
    def test_vectorised_matches_reference_loop(self):
        from qsde.models.fracdiff import _get_weights_ffd, frac_diff_ffd
        rng = np.random.default_rng(0)
        x = np.cumsum(rng.normal(size=700))
        x[300] = np.nan
        s = pd.Series(x, index=pd.bdate_range("2020-01-01", periods=700))
        w = _get_weights_ffd(0.4, 1e-4)
        got = frac_diff_ffd(s, d=0.4, thres=1e-4, auto_widen=False).to_numpy()
        np.testing.assert_allclose(got, _reference_ffd(x, w), rtol=1e-9, atol=1e-9, equal_nan=True)

    def test_fixed_kernel_value_independent_of_series_length(self):
        from qsde.models.fracdiff import frac_diff_ffd
        rng = np.random.default_rng(1)
        s = pd.Series(np.cumsum(rng.normal(size=900)), index=pd.bdate_range("2019-01-01", periods=900))
        full = frac_diff_ffd(s, d=0.4, auto_widen=False)
        cut = s.iloc[:500]
        part = frac_diff_ffd(cut, d=0.4, auto_widen=False)
        common = part.dropna().index
        assert len(common) > 100
        np.testing.assert_allclose(part.loc[common], full.loc[common], rtol=1e-9)

    def test_short_series_is_nan_not_a_different_kernel(self):
        from qsde.models.fracdiff import frac_diff_ffd
        s = pd.Series(np.arange(60, dtype=float), index=pd.bdate_range("2020-01-01", periods=60))
        out = frac_diff_ffd(s, d=0.4, auto_widen=False)
        assert out.isna().all()


class TestTailWriteMatchesBackfill:
    """The daily EOD job persists only a tail; the backfill persists
    everything. Rows for the same dates must be identical."""

    @staticmethod
    def _run(monkeypatch, frames, **kw):
        from qsde.factors import engine, pit_writer
        captured: dict = {}
        monkeypatch.setattr(engine, "compute_factors_for_symbol",
                            lambda sym, start="2006-01-01": frames[sym].copy())
        monkeypatch.setattr(pit_writer, "write_factors_to_pit",
                            lambda df, **k: captured.update(df=df.copy(), k=k) or len(df))
        engine.compute_factors_batch(list(frames), **kw)
        return captured["df"], captured["k"]

    @staticmethod
    def _frames():
        out = {}
        for sym, seed in (("AAA", 1), ("BBB", 2)):
            px = _walk(650, seed)
            f = pd.DataFrame({
                "symbol": sym,
                "tech_obv_slope": np.cumsum(np.random.default_rng(seed).normal(size=650)),
                "tech_rsi_14": np.random.default_rng(seed + 9).uniform(20, 80, 650),
            }, index=px.index)
            out[sym] = f
        return out

    def test_tail_rows_equal_backfill_rows(self, monkeypatch):
        frames = self._frames()
        last = max(f.index.max() for f in frames.values())
        full, _ = self._run(monkeypatch, frames)
        tail, kw = self._run(monkeypatch, frames, write_tail_days=10, today=last)
        assert kw["factor_version"] == "ffd_v2"

        def key(df):
            d = df.reset_index().rename(columns={df.index.name or "index": "date"})
            return d.set_index(["symbol", "date"]).sort_index()

        f, t = key(full), key(tail)
        assert len(t) > 0
        f = f.loc[t.index]
        pd.testing.assert_frame_equal(t, f, check_exact=False, rtol=1e-9, atol=1e-9)

    def test_value_at_date_does_not_depend_on_later_history(self, monkeypatch):
        frames = self._frames()
        cut = frames["AAA"].index[-40]
        short = {k: v.loc[:cut] for k, v in frames.items()}
        a, _ = self._run(monkeypatch, short)
        b, _ = self._run(monkeypatch, frames)

        def at(df):
            d = df.reset_index().rename(columns={df.index.name or "index": "date"})
            return d[d["date"] == cut].set_index("symbol").sort_index()[["tech_obv_slope", "tech_rsi_14"]]

        pd.testing.assert_frame_equal(at(a), at(b), check_exact=False, rtol=1e-9, atol=1e-9)


# ── 4. high/low reach the labeler ────────────────────────────────────

class TestLabelerGetsHighLow:
    def test_prices_query_selects_high_and_low_and_labeler_receives_them(self, monkeypatch):
        from qsde.models import dataset, triple_barrier

        px = _walk(420, 7)
        prices = px.reset_index().assign(symbol="AAA")[["symbol", "date", "open", "high", "low", "close"]]
        # The real DB returns datetime.date objects for both tables.
        prices["date"] = prices["date"].dt.date
        asked: list[str] = []

        def fake_read_sql(sql, params=None, **k):
            asked.append(sql)
            if "FROM ohlcv" in sql:
                return prices.copy()
            if "FROM factor_pit" in sql:
                d = px.index[60:380]
                rows = [(("AAA"), x.date(), "tech_obv_slope", float(i)) for i, x in enumerate(d)]
                return pd.DataFrame(rows, columns=["symbol", "as_of_date", "factor_name", "factor_value"])
            return pd.DataFrame()

        seen: dict = {}
        real = triple_barrier.apply_triple_barrier_labels

        def spy(dataset, ohlcv, horizon="swing", **kw):
            seen["cols"] = set(ohlcv.columns)
            seen["hl_differs"] = bool((ohlcv["high"] != ohlcv["close"]).any()
                                      and (ohlcv["low"] != ohlcv["close"]).any())
            return real(dataset=dataset, ohlcv=ohlcv, horizon=horizon, **kw)

        monkeypatch.setattr(dataset, "read_sql", fake_read_sql)
        monkeypatch.setattr(triple_barrier, "apply_triple_barrier_labels", spy)
        out = dataset.build_training_dataset(horizon="swing", start_date="2019-01-01",
                                             end_date="2020-12-31")
        price_sql = next(s for s in asked if "FROM ohlcv" in s)
        assert re.search(r"\bhigh\b", price_sql) and re.search(r"\blow\b", price_sql)
        assert {"high", "low", "close"} <= seen["cols"]
        assert seen["hl_differs"], "labeler received close-only OHLCV (H4 partial fix)"
        assert len(out) > 0 and set(out["target"].unique()) <= {-1.0, 0.0, 1.0}


# ── 5. feature parity ────────────────────────────────────────────────

class TestFeatureParity:
    def test_real_factor_values_do_not_use_future_bars(self, monkeypatch):
        """Technical/SMC/pattern factors at date T computed from history ending
        at T must equal the same factors computed from a longer history."""
        from qsde.factors import engine

        px = _walk(760, 11)
        empty = lambda *a, **k: pd.DataFrame(index=px.index)  # noqa: E731
        for name in ("compute_all_fundamental", "compute_all_flow", "compute_all_macro",
                     "compute_all_sentiment"):
            monkeypatch.setattr(engine, name, empty)
        monkeypatch.setattr(engine, "load_ohlcv",
                            lambda sym, start="2006-01-01", end=None:
                            px.loc[:end] if end else px)

        cut = px.index[-60]
        short = px.loc[:cut]
        monkeypatch.setattr(engine, "load_ohlcv", lambda sym, start="2006-01-01", end=None: short)
        a = engine.compute_factors_for_symbol("AAA")
        monkeypatch.setattr(engine, "load_ohlcv", lambda sym, start="2006-01-01", end=None: px)
        b = engine.compute_factors_for_symbol("AAA")

        cols = [c for c in a.columns if c != "symbol" and c in b.columns]
        assert len(cols) > 30
        aa = a.loc[cut, cols].astype(float)
        bb = b.loc[cut, cols].astype(float)
        both = aa.notna() & bb.notna()
        bad = [c for c in cols if both[c] and not np.isclose(aa[c], bb[c], rtol=1e-6, atol=1e-9)]
        assert not bad, f"factors depend on bars after T (look-ahead): {bad}"

    def test_train_and_serve_reshape_factor_rows_identically(self):
        from qsde.models.feature_view import pivot_factors
        d = pd.Timestamp("2024-03-01").date()
        long = pd.DataFrame({
            "symbol": ["AAA"] * 3 + ["BBB"] * 3,
            "as_of_date": [d] * 6,
            "factor_name": ["f1", "f2", "f3"] * 2,
            "factor_value": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
        })
        train = pivot_factors(long, index=["symbol", "as_of_date"]).drop(columns="as_of_date")
        serve = pivot_factors(long.drop(columns="as_of_date"), index=["symbol"])
        pd.testing.assert_frame_equal(
            train.sort_values("symbol").reset_index(drop=True),
            serve.sort_values("symbol").reset_index(drop=True)[train.columns],
        )
