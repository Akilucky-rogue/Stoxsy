"""After-close reconciliation of streamed minute bars against Kite's historical minute bars.

Streamed bars are built from Kite price snapshots (not every trade) and the first/last bar of a session can be partial, so a streamed high can miss a few paise and a
streamed volume can be short. The official Kite historical bar is the reference. `compare` is pure; `run` does the I/O (fetch, store, record). Streamed bars are never
overwritten: the official bars go to `ohlcv_intraday_official`, the result to `intraday_reconciliation`, and the view `ohlcv_intraday_best` prefers the official bar.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

IST = ZoneInfo("Asia/Kolkata")
TOL_BPS = 2.0               # open/close differences within this many basis points count as a match
COVERAGE_OK = 0.98          # share of official bars (inside the streamed window) that we also streamed
CLOSE_P95_BPS = 5.0         # 95th percentile |close difference| allowed
VOLUME_BAND = (0.85, 1.15)  # total streamed / official volume over non-edge bars
FIRST_BAR_IST, LAST_BAR_IST = (9, 15), (15, 29)
OHLC = ["open", "high", "low", "close"]


@dataclass
class Result:
    symbol: str
    status: str
    n_stream: int
    n_official: int
    n_both: int
    stats: dict
    joined: Optional[pd.DataFrame] = None


def _utc_index(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.index = pd.DatetimeIndex(pd.to_datetime(d.index, utc=True)).floor("min")
    return d[~d.index.duplicated(keep="last")].sort_index()


def _is_edge(ts: pd.Timestamp, first: pd.Timestamp, last: pd.Timestamp) -> bool:
    """The first/last streamed bar is partial unless the stream really covered the session boundary (09:15 or 15:29 IST)."""
    t = ts.tz_convert(IST)
    if ts == first and (t.hour, t.minute) != FIRST_BAR_IST:
        return True
    if ts == last and (t.hour, t.minute) != LAST_BAR_IST:
        return True
    return False


def official_problems(o: pd.DataFrame) -> int:
    """Number of official bars that violate OHLC logic (high below open/close, low above, negative volume)."""
    if o.empty:
        return 0
    bad = (o["high"] < o[["open", "close"]].max(axis=1) - 1e-9) | (o["low"] > o[["open", "close"]].min(axis=1) + 1e-9) | (o["volume"] < 0)
    return int(bad.sum())


def compare(symbol: str, streamed: pd.DataFrame, official: pd.DataFrame) -> Result:
    """Both frames indexed by bar-start time with open/high/low/close/volume."""
    if streamed.empty:
        return Result(symbol, "no_stream", 0, len(official), 0, {"note": "no streamed bars for this session"})
    s, o = _utc_index(streamed), _utc_index(official) if len(official) else official
    if o.empty:
        return Result(symbol, "no_official", len(s), 0, 0, {"note": "Kite returned no minute bars"})
    first, last = s.index.min(), s.index.max()
    o_in = o[(o.index >= first) & (o.index <= last)]                 # official bars inside the window we were listening
    j = s.join(o, how="inner", lsuffix="_s", rsuffix="_o")
    edge = np.array([_is_edge(t, first, last) for t in j.index], dtype=bool)
    j["edge"] = edge
    for c in ("open", "close"):
        j[f"{c}_diff_bps"] = (j[f"{c}_s"] - j[f"{c}_o"]) / j[f"{c}_o"] * 1e4
    j["high_miss_bps"] = (j["high_o"] - j["high_s"]) / j["high_o"] * 1e4        # > 0: the streamed high is below the true high
    j["low_miss_bps"] = (j["low_s"] - j["low_o"]) / j["low_o"] * 1e4            # > 0: the streamed low is above the true low
    k = j[~j["edge"]]
    coverage = len(j) / len(o_in) if len(o_in) else float("nan")
    cd = k["close_diff_bps"].abs()
    vol_ratio = float(k["volume_s"].sum() / k["volume_o"].sum()) if len(k) and k["volume_o"].sum() > 0 else float("nan")
    q = lambda x, p: float(np.nanpercentile(x, p)) if len(x) else float("nan")                  # noqa: E731
    stats = {
        "coverage": coverage, "missing_minutes": int(len(o_in) - len(j)), "extra_streamed_minutes": int(len(s) - len(j)), "edge_bars_excluded": int(edge.sum()),
        "open_match_rate": float((k["open_diff_bps"].abs() <= TOL_BPS).mean()) if len(k) else float("nan"),
        "close_match_rate": float((cd <= TOL_BPS).mean()) if len(k) else float("nan"),
        "close_abs_bps_p50": q(cd, 50), "close_abs_bps_p95": q(cd, 95),
        "high_miss_bps_p50": q(k["high_miss_bps"], 50), "high_miss_bps_p95": q(k["high_miss_bps"], 95), "high_miss_bps_max": float(k["high_miss_bps"].max()) if len(k) else float("nan"),
        "low_miss_bps_p50": q(k["low_miss_bps"], 50), "low_miss_bps_p95": q(k["low_miss_bps"], 95),
        "streamed_high_above_official_bars": int((k["high_miss_bps"] < -TOL_BPS).sum()),
        "volume_ratio_total": vol_ratio, "official_bar_problems": official_problems(o_in),
    }
    if len(k) < 3:
        status, stats["note"] = "too_short", "fewer than 3 comparable (non-edge) bars: not enough to judge"
    elif not coverage >= COVERAGE_OK:
        status = "gappy"
    elif stats["close_abs_bps_p95"] > CLOSE_P95_BPS:
        status = "price_drift"
    elif not (VOLUME_BAND[0] <= vol_ratio <= VOLUME_BAND[1]):
        status = "volume_off"
    else:
        status = "ok"
    return Result(symbol, status, len(s), len(o_in), len(j), stats, j)


# ── I/O ──────────────────────────────────────────────────────────────

def session_window(d: date) -> tuple[str, str]:
    return f"{d.isoformat()} 09:15:00", f"{d.isoformat()} 15:30:00"


def kite_fetch(kite) -> Callable[[int, date], pd.DataFrame]:
    """Fetcher over a connected KiteConnect: one minute-bar call per symbol and session."""
    def fetch(token: int, d: date) -> pd.DataFrame:
        a, b = session_window(d)
        rows = kite.historical_data(token, a, b, "minute")
        if not rows:
            return pd.DataFrame(columns=OHLC + ["volume"])
        df = pd.DataFrame(rows)
        df.index = pd.DatetimeIndex(pd.to_datetime(df["date"], utc=True))
        return df[OHLC + ["volume"]]
    return fetch


def load_streamed(conn, symbol: str, d: date) -> pd.DataFrame:
    cur = conn.cursor()
    a = datetime(d.year, d.month, d.day, 9, 0, tzinfo=IST)
    b = datetime(d.year, d.month, d.day, 15, 45, tzinfo=IST)
    cur.execute("SELECT ts, open, high, low, close, volume FROM ohlcv_intraday WHERE symbol = %s AND ts >= %s AND ts <= %s ORDER BY ts", (symbol, a, b))
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=OHLC + ["volume"])
    df = pd.DataFrame(rows)
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts"), utc=True))
    return df[OHLC + ["volume"]].astype(float)


def store_official(conn, symbol: str, o: pd.DataFrame) -> int:
    if o.empty:
        return 0
    cur = conn.cursor()
    rows = [(symbol, ts.to_pydatetime(), float(r.open), float(r.high), float(r.low), float(r.close), int(r.volume)) for ts, r in _utc_index(o).iterrows()]
    cur.executemany("INSERT INTO ohlcv_intraday_official (symbol, ts, open, high, low, close, volume) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (symbol, ts) DO UPDATE SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, "
                    "volume = EXCLUDED.volume, fetched_at = NOW()", rows)
    return len(rows)


def store_result(conn, d: date, r: Result) -> None:
    import psycopg2.extras

    clean = {k: (None if isinstance(v, float) and not np.isfinite(v) else v) for k, v in r.stats.items()}
    conn.cursor().execute(
        "INSERT INTO intraday_reconciliation (session_date, symbol, status, n_stream, n_official, n_both, stats) VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (session_date, symbol) DO UPDATE SET status = EXCLUDED.status, n_stream = EXCLUDED.n_stream, n_official = EXCLUDED.n_official, "
        "n_both = EXCLUDED.n_both, stats = EXCLUDED.stats, reconciled_at = NOW()",
        (d, r.symbol, r.status, r.n_stream, r.n_official, r.n_both, psycopg2.extras.Json(clean)))


def run(d: date, symbols: Optional[list] = None, fetch: Optional[Callable] = None, pause: float = 0.35, conn=None, progress: Callable = print) -> list[Result]:
    """Reconcile one session. `fetch(token, date)` defaults to Kite; tests inject a fake. Streamed bars are only read, never changed."""
    from qsde.db.connection import get_sync_conn

    own = conn is None
    ctx = get_sync_conn() if own else None
    c = ctx.__enter__() if own else conn
    try:
        cur = c.cursor()
        if symbols is None:
            a = datetime(d.year, d.month, d.day, 0, 0, tzinfo=IST)
            cur.execute("SELECT DISTINCT symbol FROM ohlcv_intraday WHERE ts >= %s AND ts < %s + INTERVAL '1 day' ORDER BY symbol", (a, a))
            symbols = [r["symbol"] for r in cur.fetchall()]
        cur.execute("SELECT tradingsymbol, instrument_token FROM kite_instruments WHERE exchange = 'NSE' AND instrument_type = 'EQ' AND tradingsymbol = ANY(%s)", (list(symbols),))
        tok = {r["tradingsymbol"]: int(r["instrument_token"]) for r in cur.fetchall()}
        if fetch is None:
            from qsde.ingestion.kite_client import KiteClient

            fetch = kite_fetch(KiteClient()._kite)
        out = []
        for i, sym in enumerate(symbols):
            s = load_streamed(c, sym, d)
            if sym not in tok:
                r = Result(sym, "no_official", len(s), 0, 0, {"note": "symbol not in the Kite instrument master"})
            else:
                o = fetch(tok[sym], d)
                if len(o):
                    store_official(c, sym, o)
                r = compare(sym, s, o)
                if i + 1 < len(symbols):
                    time.sleep(pause)
            store_result(c, d, r)
            out.append(r)
            progress(f"{sym}: {r.status} (stream {r.n_stream}, official {r.n_official}, both {r.n_both})")
        return out
    finally:
        if own:
            ctx.__exit__(None, None, None)
