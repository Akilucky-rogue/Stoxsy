"""Reviewed price-adjustment pipeline: split/bonus-adjusted prices and a separate dividend total-return series.

Strictly separate from `ohlcv` (which is untouched) and from raw `eod_raw`. Inputs are explicit and fingerprinted; outputs are
immutable Parquet with a manifest, deterministic byte-for-byte: the same inputs and approval set always give the same output hash.

Rules
  * only `confirmed` (ca_reconcile_v1) and approved (`official_confirmed` / owner_approved) split-bonus events are used, and the factor
    is the NSE-LISTED multiplier, never the observed gap;
  * an event scales every bar of its symbol STRICTLY BEFORE its gap date (the first bar after the action); volume is divided;
  * dividends never touch prices: `price_return`, `dividend_return` and `total_return` are separate, labelled columns, gross of tax;
  * rights and demergers are NOT handled here (event-level review).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

log = logging.getLogger(__name__)

ROOT = Path(r"D:\Stoxsy\data\adjusted")
SPLIT_VERSION_PREFIX = "sbadj_v1"
TR_VERSION_PREFIX = "tr_v1"
ZSTD_LEVEL = 3
MAX_DIV_YIELD = 0.25
EVENT_COLS = ["symbol", "security_id", "gap_date", "nse_ex_date", "factor", "tier", "source", "source_record_id", "isin", "evidence_ref"]


# ── event set ────────────────────────────────────────────────────────

def normalise_events(events: pd.DataFrame) -> pd.DataFrame:
    """Dedupe (same symbol, gap date and factor listed twice), validate, sort deterministically."""
    e = events.copy()
    for c in EVENT_COLS:
        if c not in e.columns:
            e[c] = None
    e["gap_date"] = pd.to_datetime(e["gap_date"]).dt.normalize()
    e["factor"] = e["factor"].astype(float).round(10)
    if ((e["factor"] <= 0) | ~np.isfinite(e["factor"])).any():
        raise ValueError("non-positive or non-finite factor in the event set")
    tier_rank = {"official_confirmed": 0, "owner_approved": 1, "confirmed": 2}
    e["_r"] = e["tier"].map(tier_rank).fillna(9)
    e = e.sort_values(["symbol", "gap_date", "factor", "_r"], kind="mergesort").drop_duplicates(["symbol", "gap_date", "factor"], keep="first")
    return e.drop(columns="_r")[EVENT_COLS].reset_index(drop=True)


def events_hash(events: pd.DataFrame) -> str:
    e = normalise_events(events)
    payload = [(r.symbol, str(r.gap_date.date()), r.factor, r.tier) for r in e.itertuples()]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def raw_fingerprint(raw: pd.DataFrame) -> dict:
    return {"rows": int(len(raw)), "min_date": str(pd.to_datetime(raw["date"]).min().date()),
            "max_date": str(pd.to_datetime(raw["date"]).max().date()),
            "sum_close": round(float(raw["close"].astype(float).sum()), 4), "symbols": int(raw["symbol"].nunique())}


# ── split / bonus adjustment ─────────────────────────────────────────

def split_bonus_adjust(raw: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    """raw: symbol,date,open,high,low,close,volume (one row per symbol-day). Returns the adjusted frame (sorted by symbol, date)
    with `adj_factor` (the cumulative multiplier applied to that bar) and `close_raw` kept for audit."""
    d = raw[["symbol", "date", "open", "high", "low", "close", "volume"]].copy()
    d["date"] = pd.to_datetime(d["date"]).dt.normalize()
    d = d.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    d["close_raw"] = d["close"].astype(float)
    d["adj_factor"] = 1.0
    ev = normalise_events(events)
    for sym, grp in ev.groupby("symbol", sort=False):
        m = (d["symbol"] == sym).to_numpy()
        if not m.any():
            continue
        dates = d.loc[m, "date"].to_numpy("datetime64[ns]")
        f = np.ones(m.sum())
        for r in grp.itertuples():
            f = np.where(dates < np.datetime64(r.gap_date), f * r.factor, f)
        d.loc[m, "adj_factor"] = f
    for c in ("open", "high", "low", "close"):
        d[c] = d[c].astype(float) * d["adj_factor"]
    d["volume"] = d["volume"].astype(float) / d["adj_factor"]
    return d[["symbol", "date", "open", "high", "low", "close", "volume", "adj_factor", "close_raw"]]


def validate_split_adjustment(raw: pd.DataFrame, adjusted: pd.DataFrame, events: pd.DataFrame, official: Optional[pd.DataFrame] = None,
                              cliff: float = 0.18, event_limit: float = 0.20, hard_limit: float = 0.40) -> dict:
    """Event-level and series-level continuity checks. `official` (optional): the current ohlcv, for comparison."""
    ev = normalise_events(events)
    a = adjusted.set_index(["symbol", "date"])["close"]
    ao = adjusted.set_index(["symbol", "date"])["open"]
    r_open = raw.assign(date=pd.to_datetime(raw["date"])).set_index(["symbol", "date"])["open"]
    r_close = raw.assign(date=pd.to_datetime(raw["date"])).set_index(["symbol", "date"])["close"]
    rows = []
    for r in ev.itertuples():
        s = adjusted[adjusted["symbol"] == r.symbol]
        i = s.index[s["date"] == r.gap_date]
        if len(i) == 0 or s.index.get_loc(i[0]) == 0:
            rows.append({"symbol": r.symbol, "gap_date": str(r.gap_date.date()), "status": "no_gap_bar"})
            continue
        k = s.index.get_loc(i[0])
        prev = s.iloc[k - 1]
        cur = s.iloc[k]
        adj_gap = float(cur["open"] / prev["close"] - 1)
        raw_gap = float(cur["open"] / cur["adj_factor"] / (prev["close"] / prev["adj_factor"]) - 1)    # raw open / raw prev close
        rows.append({"symbol": r.symbol, "gap_date": str(r.gap_date.date()), "factor": r.factor, "raw_overnight": raw_gap,
                     "adjusted_overnight": adj_gap, "tier": r.tier,
                     "status": "FAIL_hard" if abs(adj_gap) >= hard_limit else ("flag" if abs(adj_gap) >= event_limit else "ok")})
    ed = pd.DataFrame(rows)

    def cliffs(frame, close="close"):
        f = frame.sort_values(["symbol", "date"])
        ret = f.groupby("symbol")[close].pct_change()
        return int((ret.abs() >= cliff).sum())
    out = {"events": int(len(ev)), "events_ok": int((ed["status"] == "ok").sum()) if len(ed) else 0,
           "events_flagged": int((ed["status"] == "flag").sum()) if len(ed) else 0,
           "events_fail_hard": int((ed["status"] == "FAIL_hard").sum()) if len(ed) else 0,
           "events_no_gap_bar": int((ed["status"] == "no_gap_bar").sum()) if len(ed) else 0,
           "cliffs_raw": cliffs(raw.assign(date=pd.to_datetime(raw["date"]))), "cliffs_adjusted": cliffs(adjusted),
           "bars": int(len(adjusted))}
    if official is not None and len(official):
        out["cliffs_official_ohlcv"] = cliffs(official.assign(date=pd.to_datetime(official["date"])))
    ok_share = out["events_ok"] / max(out["events"], 1)
    out["event_ok_share"] = ok_share
    out["status"] = "validated" if (ok_share >= 0.99 and out["events_fail_hard"] == 0 and out["events_no_gap_bar"] == 0) else "failed"
    return {"summary": out, "events": ed}


# ── deterministic Parquet writer ─────────────────────────────────────

SPLIT_SCHEMA = pa.schema([("symbol", pa.string()), ("date", pa.date32()), ("open", pa.float64()), ("high", pa.float64()),
                          ("low", pa.float64()), ("close", pa.float64()), ("volume", pa.float64()),
                          ("adj_factor", pa.float64()), ("close_raw", pa.float64())])
TR_SCHEMA = pa.schema([("symbol", pa.string()), ("date", pa.date32()), ("close_adj", pa.float64()),
                       ("price_return_1d", pa.float64()), ("dividend_return_1d", pa.float64()), ("total_return_1d", pa.float64()),
                       ("price_return_index", pa.float64()), ("total_return_index", pa.float64()), ("dividend_per_share_raw", pa.float64())])


@dataclass
class Materialization:
    version: str
    kind: str
    path: Path
    output_hash: str
    rows: int
    manifest: dict = field(default_factory=dict)


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def write_materialization(df: pd.DataFrame, schema: pa.Schema, kind: str, prefix: str, inputs: dict, root: Path = ROOT,
                          extra: Optional[dict] = None, built_at: Optional[datetime] = None) -> Materialization:
    """Write per-year Parquet files + MANIFEST.json atomically (temp dir -> rename). Version = prefix + hash of the inputs."""
    in_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()
    version = f"{prefix}-{in_hash[:10]}"
    final = Path(root) / kind / version
    if final.exists():
        man = json.loads((final / "MANIFEST.json").read_text(encoding="utf-8"))
        return Materialization(version, kind, final, man["output_hash"], man["rows"], man)
    tmp = Path(root) / kind / f".tmp-{version}"
    if tmp.exists():
        import shutil
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    d = df.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    years = pd.to_datetime(d["date"]).dt.year
    files = []
    for y in sorted(years.unique()):
        part = d[years == y]
        tbl = pa.Table.from_arrays(
            [pa.array(part["symbol"].to_numpy(dtype=object), type=pa.string()),
             pa.array(pd.to_datetime(part["date"]).dt.date.to_numpy(dtype=object), type=pa.date32())] +
            [pa.array(part[c.name].to_numpy(dtype="float64"), type=pa.float64(), from_pandas=True) for c in list(schema)[2:]],
            schema=schema)
        name = f"data-{y}.parquet"
        pq.write_table(tbl, tmp / name, compression="zstd", compression_level=ZSTD_LEVEL, use_dictionary=["symbol"],
                       write_statistics=True, data_page_version="2.0")
        files.append({"name": name, "rows": int(len(part)), "sha256": _sha(tmp / name)})
    out_hash = hashlib.sha256(json.dumps([(f["name"], f["sha256"]) for f in files]).encode()).hexdigest()
    manifest = {"version": version, "kind": kind, "inputs": inputs, "input_hash": in_hash, "output_hash": out_hash,
                "rows": int(len(d)), "files": files, "columns": [c.name for c in schema],
                "built_at": (built_at or datetime.now(timezone.utc)).isoformat(), **(extra or {})}
    (tmp / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str), encoding="utf-8")
    tmp.rename(final)
    return Materialization(version, kind, final, out_hash, int(len(d)), manifest)


def read_materialization(path: Path, symbols=None, start=None, end=None) -> pd.DataFrame:
    p = Path(path)
    parts = [pq.read_table(f).to_pandas() for f in sorted(p.glob("data-*.parquet"))]
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    if symbols is not None:
        df = df[df["symbol"].isin(set(symbols))]
    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end)]
    return df.reset_index(drop=True)


# ── total return (dividends) ─────────────────────────────────────────

def total_return_series(adjusted: pd.DataFrame, dividends: pd.DataFrame, max_yield: float = MAX_DIV_YIELD) -> tuple[pd.DataFrame, dict]:
    """adjusted: output of split_bonus_adjust. dividends: symbol, ex_date, amount (RAW per-share cash at the time, gross of tax).

    A dividend with ex-date x is attached to the first bar on/after x. price_return is the split-adjusted close-to-close return;
    dividend_return = D x adj_factor(bar) / adjusted previous close; total_return = their sum, exactly (P_t + D)/P_{t-1} - 1.
    Dividends yielding more than `max_yield` of the previous close are skipped and counted (units / special payouts need review)."""
    d = adjusted[["symbol", "date", "close", "adj_factor"]].sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
    d["prev_close"] = d.groupby("symbol")["close"].shift(1)
    d["price_return_1d"] = d["close"] / d["prev_close"] - 1.0
    d["dividend_per_share_raw"] = 0.0
    div = dividends.copy()
    div["ex_date"] = pd.to_datetime(div["ex_date"]).dt.normalize()
    div = div.groupby(["symbol", "ex_date"], as_index=False)["amount"].sum()
    skipped = {"no_bar": 0, "no_prev_close": 0, "yield_too_high": 0, "nonpositive": 0}
    used = 0
    pos = {s: g.index.to_numpy() for s, g in d.groupby("symbol", sort=False)}
    dates = {s: d.loc[ix, "date"].to_numpy("datetime64[ns]") for s, ix in pos.items()}
    for r in div.itertuples():
        if r.amount is None or not (r.amount > 0):
            skipped["nonpositive"] += 1
            continue
        if r.symbol not in pos:
            skipped["no_bar"] += 1
            continue
        k = int(np.searchsorted(dates[r.symbol], np.datetime64(r.ex_date)))
        if k >= len(dates[r.symbol]):
            skipped["no_bar"] += 1
            continue
        row = pos[r.symbol][k]
        pc = d.at[row, "prev_close"]
        if not (pc == pc) or pc <= 0:
            skipped["no_prev_close"] += 1
            continue
        if r.amount * d.at[row, "adj_factor"] / pc > max_yield:
            skipped["yield_too_high"] += 1
            continue
        d.at[row, "dividend_per_share_raw"] += float(r.amount)
        used += 1
    d["dividend_return_1d"] = d["dividend_per_share_raw"] * d["adj_factor"] / d["prev_close"]
    d["total_return_1d"] = d["price_return_1d"].fillna(0.0) + d["dividend_return_1d"].fillna(0.0)
    first = d["price_return_1d"].isna()
    d.loc[first, ["price_return_1d", "dividend_return_1d", "total_return_1d"]] = np.nan
    d["price_return_index"] = 100.0 * (1.0 + d["price_return_1d"].fillna(0.0)).groupby(d["symbol"]).cumprod()
    d["total_return_index"] = 100.0 * (1.0 + d["total_return_1d"].fillna(0.0)).groupby(d["symbol"]).cumprod()
    out = d.rename(columns={"close": "close_adj"})[["symbol", "date", "close_adj", "price_return_1d", "dividend_return_1d",
                                                    "total_return_1d", "price_return_index", "total_return_index",
                                                    "dividend_per_share_raw"]]
    return out, {"dividends_used": used, "dividends_skipped": skipped, "dividends_in": int(len(div))}
