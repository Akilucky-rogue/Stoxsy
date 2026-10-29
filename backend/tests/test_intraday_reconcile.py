"""Streamed vs Kite minute bars: matching, missed highs, gaps, partial edge bars, official-bar sanity, and the database round trip (a synthetic symbol, removed afterwards)."""
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from qsde.live import reconcile as R

D = date(2026, 10, 23)


def bars(start="09:15", n=30, base=1000.0, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(f"2026-10-23 {start}", periods=n, freq="min", tz=R.IST).tz_convert("UTC")
    c = base + np.cumsum(rng.normal(0, 0.4, n))
    o = np.r_[base, c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.05, 0.4, n)
    l = np.minimum(o, c) - rng.uniform(0.05, 0.4, n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": rng.integers(5000, 9000, n)}, index=idx)


def test_identical_bars_reconcile_ok():
    o = bars()
    r = R.compare("X", o.copy(), o)
    assert r.status == "ok" and r.stats["coverage"] == 1.0 and r.stats["close_match_rate"] == 1.0 and r.stats["high_miss_bps_max"] == pytest.approx(0)


def test_missed_highs_are_measured_but_do_not_fail_the_bar():
    o = bars()
    s = o.copy()
    s["high"] = (s["high"] - 0.2).clip(lower=s[["open", "close"]].max(axis=1))
    r = R.compare("X", s, o)
    assert r.stats["high_miss_bps_p95"] > 1 and r.stats["close_match_rate"] == 1.0 and r.status == "ok"


def test_close_drift_and_volume_and_gaps_are_flagged():
    o = bars()
    d = o.copy(); d["close"] = d["close"] * 1.001                       # 10 bps off
    assert R.compare("X", d, o).status == "price_drift"
    v = o.copy(); v["volume"] = (v["volume"] * 0.5).astype(int)
    assert R.compare("X", v, o).status == "volume_off"
    g = o.drop(o.index[10:16])                                          # 6 of 30 official minutes never streamed
    r = R.compare("X", g, o)
    assert r.status == "gappy" and r.stats["missing_minutes"] == 6 and r.stats["coverage"] == pytest.approx(24 / 30)


def test_a_stream_that_started_mid_session_does_not_fail_on_its_partial_first_bar():
    o = bars()
    s = o.iloc[5:].copy()
    s.iloc[0, s.columns.get_loc("volume")] = 100                         # first streamed minute only partly seen
    s.iloc[0, s.columns.get_loc("open")] += 3
    r = R.compare("X", s, o)
    assert r.status == "ok" and r.stats["edge_bars_excluded"] == 2          # started mid-session (09:20) and stopped at 09:44: both ends are partial
    full = R.compare("X", o.copy(), o)
    assert full.stats["edge_bars_excluded"] == 1                           # started exactly at 09:15 (a real session start), stopped at 09:44


def test_a_session_of_one_or_two_bars_is_too_short_not_gappy():
    o = bars()
    assert R.compare("X", o.iloc[:1].copy(), o).status == "too_short"
    assert R.compare("X", o.iloc[:3].copy(), o).status == "too_short"        # 3 streamed bars, first and last are edges: only 1 comparable


def test_no_official_and_no_stream_and_official_problems():
    o = bars()
    assert R.compare("X", o, o.iloc[0:0]).status == "no_official"
    assert R.compare("X", o.iloc[0:0], o).status == "no_stream"
    bad = o.copy(); bad.iloc[3, bad.columns.get_loc("high")] = bad.iloc[3]["low"] - 1
    assert R.official_problems(bad) == 1
    assert R.compare("X", o.copy(), bad).stats["official_bar_problems"] == 1


def test_database_round_trip_keeps_streamed_bars_and_prefers_official_in_the_view():
    from qsde.db.connection import execute_sql, get_sync_conn, read_sql

    tok = 999999917
    try:
        execute_sql("INSERT INTO kite_instruments (instrument_token, exchange_token, tradingsymbol, exchange, instrument_type, segment) VALUES (%s,%s,'ZZRECON','NSE','EQ','NSE') "
                    "ON CONFLICT (instrument_token) DO NOTHING", (tok, 1))
        o = bars(base=500.0)
        s = o.copy(); s["high"] = s["high"] - 0.3; s["high"] = s[["high", "open", "close"]].max(axis=1)
        with get_sync_conn() as c:
            cur = c.cursor()
            for ts, r in s.iterrows():
                cur.execute("INSERT INTO ohlcv_intraday (symbol, ts, open, high, low, close, volume, n_ticks) VALUES ('ZZRECON',%s,%s,%s,%s,%s,%s,5) ON CONFLICT DO NOTHING",
                            (ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close), int(r.volume)))
        res = R.run(D, ["ZZRECON"], fetch=lambda token, d: o if token == tok else o.iloc[0:0], pause=0, progress=lambda *_: None)
        assert res[0].status == "ok" and res[0].n_both == 30
        assert read_sql("SELECT count(*) AS n FROM ohlcv_intraday WHERE symbol = 'ZZRECON'")["n"].iloc[0] == 30                    # streamed bars untouched
        assert read_sql("SELECT count(*) AS n FROM ohlcv_intraday_official WHERE symbol = 'ZZRECON'")["n"].iloc[0] == 30
        best = read_sql("SELECT high, source FROM ohlcv_intraday_best WHERE symbol = 'ZZRECON' ORDER BY ts")
        assert set(best["source"]) == {"kite_historical"} and np.allclose(best["high"].to_numpy(), o["high"].to_numpy())
        rec = read_sql("SELECT status, n_both FROM intraday_reconciliation WHERE symbol = 'ZZRECON'")
        assert rec["status"].iloc[0] == "ok" and rec["n_both"].iloc[0] == 30
        R.run(D, ["ZZRECON"], fetch=lambda token, d: o, pause=0, progress=lambda *_: None)                                          # idempotent
        assert read_sql("SELECT count(*) AS n FROM ohlcv_intraday_official WHERE symbol = 'ZZRECON'")["n"].iloc[0] == 30
    finally:
        for t in ("ohlcv_intraday", "ohlcv_intraday_official", "intraday_reconciliation"):
            execute_sql(f"DELETE FROM {t} WHERE symbol = 'ZZRECON'")
        execute_sql("DELETE FROM kite_instruments WHERE instrument_token = %s", (tok,))
