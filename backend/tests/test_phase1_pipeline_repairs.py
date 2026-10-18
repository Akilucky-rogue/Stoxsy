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

class TestOneStoreForTrainAndServe:
    """The factor_pit SQL readers were replaced by the Parquet store (ADR 0001).
    Training and serving must use the same reader module and version constant,
    and no live code path may read factor_pit any more."""

    def test_train_and_serve_share_the_same_reader_and_version_constant(self):
        import inspect

        from qsde.factors.versions import FFD_VERSION
        from qsde.models import dataset, signal_generator
        sig = inspect.signature(dataset.build_training_dataset)
        assert sig.parameters["factor_version"].default == FFD_VERSION
        d_src = Path(dataset.__file__).read_text()
        s_src = Path(signal_generator.__file__).read_text()
        assert "from qsde.features.store import read_features" in d_src
        assert "from qsde.features.store import read_latest_rows" in s_src
        assert "FFD_VERSION" in s_src

    def test_no_live_module_reads_factor_pit(self):
        """Two live stores is exactly the skew this repo just fixed."""
        import re
        root = BACKEND / "qsde"
        offenders = []
        allowed = {"pit_writer.py"}       # legacy writer kept only for the raw_v0 archive / bitemporal tests
        for p in list(root.rglob("*.py")) + list((BACKEND / "api").rglob("*.py")):
            if p.name in allowed or p.parent.name == "db" or p.name == "versions.py":
                continue
            txt = p.read_text(encoding="utf-8", errors="ignore")
            # code (not prose): a SQL FROM/JOIN of the table
            if re.search(r"(?i)\b(from|join)\s+factor_pit\b", txt):
                offenders.append(str(p.relative_to(BACKEND)))
        assert not offenders, f"still reading factor_pit: {offenders}"


def _db_up() -> bool:
    try:
        from qsde.db.connection import read_sql
        read_sql("SELECT 1 AS x")
        return True
    except Exception:  # noqa: BLE001
        return False


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
        from qsde.factors import engine
        captured: dict = {}
        monkeypatch.setattr(engine, "compute_factors_for_symbol",
                            lambda sym, start="2006-01-01": frames[sym].copy())
        monkeypatch.setattr(engine, "_persist",
                            lambda df, version: captured.update(df=df.copy(), k={"factor_version": version}))
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

    def test_universe_filter_persists_only_member_symbol_days_but_uses_full_history(self, monkeypatch):
        frames = self._frames()
        full, _ = self._run(monkeypatch, frames)                      # unfiltered reference
        d_all = sorted(frames["AAA"].index)
        members = pd.DataFrame({"date": d_all[-30:-10], "symbol": "AAA"})   # AAA only, 20 days
        filt, kw = self._run(monkeypatch, frames, universe=members)
        assert set(filt["symbol"]) == {"AAA"} and len(filt) == 20
        assert kw["factor_version"] == "ffd_v2"
        ref = full[(full["symbol"] == "AAA") & (full.index.isin(members["date"]))]
        # differenced from the FULL history, not from the 20 persisted rows
        np.testing.assert_allclose(filt["tech_obv_slope"].to_numpy(),
                                   ref["tech_obv_slope"].to_numpy(), rtol=1e-9)

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
            return pd.DataFrame()

        def fake_read_features(**k):
            d = px.index[60:380]
            return pd.DataFrame({"symbol": "AAA", "as_of_date": [x.date() for x in d],
                                 "known_at": pd.NaT, "tech_obv_slope": [float(i) for i in range(len(d))]})

        seen: dict = {}
        real = triple_barrier.apply_triple_barrier_labels

        def spy(dataset, ohlcv, horizon="swing", **kw):
            seen["cols"] = set(ohlcv.columns)
            seen["hl_differs"] = bool((ohlcv["high"] != ohlcv["close"]).any()
                                      and (ohlcv["low"] != ohlcv["close"]).any())
            return real(dataset=dataset, ohlcv=ohlcv, horizon=horizon, **kw)

        monkeypatch.setattr(dataset, "read_sql", fake_read_sql)
        monkeypatch.setattr(dataset, "read_features", fake_read_features)
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

    def test_train_and_serve_read_identical_values_for_the_same_symbol_day(self, tmp_path):
        """The training reader (read_features) and the serving reader (read_latest_rows)
        come from one store; for the same (symbol, date) they must return the same
        numbers, NaN included (no zero-filling on either side)."""
        from datetime import datetime, timezone

        from qsde.features.store import SnapshotWriter, read_features, read_latest_rows

        idx = pd.bdate_range("2026-03-02", periods=12, name="date")
        rng = np.random.default_rng(1)
        df = pd.concat([pd.DataFrame({"symbol": s, "f1": rng.normal(size=12), "f2": rng.normal(size=12)},
                                     index=idx) for s in ("AAA", "BBB")])
        df.loc[(df.index == idx[-1]) & (df["symbol"] == "BBB"), "f2"] = np.nan
        w = SnapshotWriter("ffd_v2", "u", root=tmp_path, fingerprint={}, sha="s",
                           built_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
        w.add(df)
        w.close(register=False)

        train = read_features("ffd_v2", root=tmp_path, start=idx[-1].date(), end=idx[-1].date())
        serve = read_latest_rows(max_age_days=30, asof=idx[-1], factor_version="ffd_v2", root=tmp_path)
        a = train.sort_values("symbol").reset_index(drop=True)
        b = serve.sort_values("symbol").reset_index(drop=True)[a.columns]
        pd.testing.assert_frame_equal(a, b)
        assert b.loc[b["symbol"] == "BBB", "f2"].isna().all()
