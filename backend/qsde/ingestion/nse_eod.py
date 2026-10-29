"""Load the local NSE daily bhavcopy archive into `eod_raw`.

Source: per-day parsed parquet files produced by the old StockTrack project,
copied to D:\\Stoxsy\\data\\stocktrack_data. Two layouts, same columns for what
we use:

    nse/old_legacy/parsed/YYYY-MM-DD.parquet   2016-01-01 .. 2020-06-30 (has ISIN)
    nse/legacy/parsed/YYYY-MM-DD.parquet       2020-07-01 .. 2026-04-30 (no ISIN)

Everything is stored RAW (unadjusted) in `eod_raw`. Rows that fail hard sanity
checks are NOT loaded; they are returned in a rejects frame with a reason, so
nothing disappears silently. Re-running over the same files is idempotent
(ON CONFLICT DO UPDATE on the primary key).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import numpy as np
import pandas as pd
from qsde.config.paths import data_path

log = logging.getLogger(__name__)

DEFAULT_BASE = Path(os.getenv("STOXSY_BHAVCOPY_DIR",
                              str(data_path("stocktrack_data", "cache", "historical", "nse"))))
# (sub-directory, first date, last date) — a file belongs to the layout whose
# range contains its date.
LAYOUTS: list[tuple[str, date, date]] = [
    ("old_legacy/parsed", date(2016, 1, 1), date(2020, 6, 30)),
    ("legacy/parsed", date(2020, 7, 1), date(2100, 1, 1)),
]
# Plain equity series only. EQ = rolling settlement, BE = trade-for-trade,
# BZ = trade-for-trade, surveillance. Govt securities, SME, debt and ETFs
# (GS, SM, N*, W*, MF ...) are excluded on purpose.
EQUITY_SERIES: tuple[str, ...] = ("EQ", "BE", "BZ")
SERIES_PRIORITY = {"EQ": 0, "BE": 1, "BZ": 2}

COLUMN_MAP = {
    "SYMBOL": "symbol", "SERIES": "series", "DATE1": "date", "ISIN": "isin",
    "OPEN_PRICE": "open", "HIGH_PRICE": "high", "LOW_PRICE": "low",
    "CLOSE_PRICE": "close", "LAST_PRICE": "last", "PREV_CLOSE": "prev_close",
    "TTL_TRD_QNTY": "volume", "TURNOVER_LACS": "turnover_lacs",
    "NO_OF_TRADES": "trades", "DELIV_QTY": "deliv_qty", "DELIV_PER": "deliv_per",
}
RAW_COLUMNS = ["exchange", "symbol", "series", "date", "isin", "open", "high", "low",
               "close", "last", "prev_close", "volume", "turnover_lacs", "trades",
               "deliv_qty", "deliv_per", "source_file"]
_NUMERIC = ["open", "high", "low", "close", "last", "prev_close",
            "volume", "turnover_lacs", "trades", "deliv_qty", "deliv_per"]
_INT = ["volume", "trades", "deliv_qty"]


# ── file discovery ───────────────────────────────────────────────────

def _layout_for(d: date) -> Optional[str]:
    for sub, lo, hi in LAYOUTS:
        if lo <= d <= hi:
            return sub
    return None


def list_files(start: date, end: date, base: Path = DEFAULT_BASE) -> list[tuple[date, Path]]:
    """Existing parquet files for trading days in [start, end], oldest first."""
    out: list[tuple[date, Path]] = []
    for sub, lo, hi in LAYOUTS:
        folder = Path(base) / sub
        if not folder.exists():
            continue
        for f in folder.glob("*.parquet"):
            try:
                d = datetime.strptime(f.stem, "%Y-%m-%d").date()
            except ValueError:
                continue
            if lo <= d <= hi and start <= d <= end:
                out.append((d, f))
    out.sort(key=lambda t: t[0])
    return out


# ── normalisation ────────────────────────────────────────────────────

def normalize_legacy_frame(df: pd.DataFrame, file_date: date, source_file: str) -> pd.DataFrame:
    """Raw parquet frame -> standard columns, equity series only, numerics coerced.

    The date printed INSIDE the data (DATE1) is authoritative, not the file name.
    Found in the archive: on market holidays (e.g. 2026-04-03, 2026-04-14) the
    downloader saved the previous trading day's bhavcopy under the holiday's
    name. Trusting the file name would have invented trading days; trusting
    DATE1 loads those rows under their true date (an idempotent no-op when that
    day's own file is also present). Rows with an unparseable DATE1 fall back
    to the file date. Any disagreement is flagged in `date_mismatch`.
    """
    df = df.rename(columns=COLUMN_MAP)
    keep = [c for c in COLUMN_MAP.values() if c in df.columns]
    df = df[keep].copy()
    for c in ("isin", "last", "deliv_qty", "deliv_per"):
        if c not in df.columns:
            df[c] = np.nan if c != "isin" else None

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["series"] = df["series"].astype(str).str.strip().str.upper()
    df = df[df["series"].isin(EQUITY_SERIES)].copy()

    for c in _NUMERIC:
        # '', ' ', '-' and similar placeholders become NaN
        df[c] = pd.to_numeric(df[c], errors="coerce")

    parsed = pd.to_datetime(df["date"], errors="coerce").dt.date
    df["date_mismatch"] = parsed.isna() | (parsed != file_date)
    df["date"] = [p if pd.notna(p) else file_date for p in parsed]
    df["exchange"] = "NSE"
    df["source_file"] = source_file
    if "isin" in df.columns:
        df["isin"] = df["isin"].where(df["isin"].notna(), None)
        df["isin"] = df["isin"].map(lambda v: str(v).strip() if v is not None else None)
    return df.reset_index(drop=True)


# ── validation ───────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    valid: pd.DataFrame
    rejects: pd.DataFrame
    counts: dict = field(default_factory=dict)


_EPS = 1e-9


def validate_eod(df: pd.DataFrame) -> ValidationResult:
    """Split `df` into loadable rows and rejects (with a `reason`).

    Hard rules (a row failing any is rejected):
      * symbol non-empty, date present, series in EQUITY_SERIES
      * open/high/low/close finite and > 0
      * high >= max(open, close) and low <= min(open, close) and high >= low
      * volume finite and >= 0
      * no duplicate (exchange, symbol, series, date) - later duplicate rows rejected
    Soft: deliv_per outside [0, 100] is set to NaN (and counted), the row is kept.
    """
    d = df.copy()
    reason = pd.Series("", index=d.index, dtype=object)

    def mark(mask: pd.Series, text: str) -> None:
        nonlocal reason
        m = mask & (reason == "")
        reason = reason.mask(m, text)

    mark(d["symbol"].isna() | (d["symbol"].astype(str).str.len() == 0) | (d["symbol"].astype(str) == "nan"),
         "missing_symbol")
    mark(d["date"].isna(), "missing_date")
    mark(~d["series"].isin(EQUITY_SERIES), "series_not_equity")
    for c in ("open", "high", "low", "close"):
        mark(~np.isfinite(d[c].astype(float)) | (d[c] <= 0), f"bad_{c}")
    mark(d["high"] < np.maximum(d["open"], d["close"]) - _EPS, "high_below_open_or_close")
    mark(d["low"] > np.minimum(d["open"], d["close"]) + _EPS, "low_above_open_or_close")
    mark(d["high"] < d["low"] - _EPS, "high_below_low")
    mark(~np.isfinite(d["volume"].astype(float)) | (d["volume"] < 0), "bad_volume")
    dup = d.duplicated(subset=["exchange", "symbol", "series", "date"], keep="first")
    mark(dup, "duplicate_key")

    bad_deliv = d["deliv_per"].notna() & ((d["deliv_per"] < 0) | (d["deliv_per"] > 100))
    d.loc[bad_deliv, "deliv_per"] = np.nan

    rejects = d[reason != ""].assign(reason=reason[reason != ""])
    valid = d[reason == ""].copy()
    counts = {
        "rows_in": int(len(d)),
        "rows_valid": int(len(valid)),
        "rows_rejected": int(len(rejects)),
        "rejects_by_reason": rejects["reason"].value_counts().to_dict() if len(rejects) else {},
        "deliv_per_out_of_range_nulled": int(bad_deliv.sum()),
        "date_mismatch_rows": int(d["date_mismatch"].sum()) if "date_mismatch" in d else 0,
    }
    return ValidationResult(valid=valid, rejects=rejects, counts=counts)


# ── database ─────────────────────────────────────────────────────────

_UPSERT_SQL = """
INSERT INTO eod_raw
    (exchange, symbol, series, date, isin, open, high, low, close, last, prev_close,
     volume, turnover_lacs, trades, deliv_qty, deliv_per, source_file)
VALUES %s
ON CONFLICT (exchange, symbol, series, date) DO UPDATE SET
    isin = EXCLUDED.isin, open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
    close = EXCLUDED.close, last = EXCLUDED.last, prev_close = EXCLUDED.prev_close,
    volume = EXCLUDED.volume, turnover_lacs = EXCLUDED.turnover_lacs, trades = EXCLUDED.trades,
    deliv_qty = EXCLUDED.deliv_qty, deliv_per = EXCLUDED.deliv_per,
    source_file = EXCLUDED.source_file, loaded_at = NOW()
"""


def _records(df: pd.DataFrame) -> list[tuple]:
    """DataFrame -> list of tuples of plain Python values (NaN/NA -> None).

    The shared upsert_dataframe helper would send NaN as the float 'NaN', which
    Postgres stores as a real value, so we convert explicitly here.
    """
    out = df[RAW_COLUMNS].copy()
    for c in _INT:
        out[c] = out[c].round().astype("Int64")
    out = out.astype(object).where(out.notna(), None)
    return [
        tuple(None if v is None else (v.item() if hasattr(v, "item") else v) for v in row)
        for row in out.itertuples(index=False, name=None)
    ]


def upsert_eod_raw(df: pd.DataFrame, page_size: int = 5000) -> int:
    """Idempotent bulk upsert of validated rows. Returns rows sent."""
    from psycopg2.extras import execute_values

    from qsde.db.connection import get_sync_conn

    if df.empty:
        return 0
    recs = _records(df)
    with get_sync_conn() as conn:
        cur = conn.cursor()
        execute_values(cur, _UPSERT_SQL, recs, page_size=page_size)
    return len(recs)


# ── orchestration ────────────────────────────────────────────────────

@dataclass
class LoadReport:
    files: int = 0
    rows_in: int = 0
    rows_valid: int = 0
    rows_rejected: int = 0
    rows_loaded: int = 0
    rejects_by_reason: dict = field(default_factory=dict)
    deliv_per_nulled: int = 0
    date_mismatch_rows: int = 0
    first_date: Optional[date] = None
    last_date: Optional[date] = None
    rejects_sample: list = field(default_factory=list)
    stale_files: list = field(default_factory=list)   # file date != date inside the data


def load_range(start: date, end: date, base: Path = DEFAULT_BASE, dry_run: bool = False,
               batch_days: int = 20, keep_reject_samples: int = 25) -> LoadReport:
    """Normalise, validate and (unless dry_run) upsert every file in [start, end]."""
    files = list_files(start, end, base)
    rep = LoadReport(files=len(files))
    buf: list[pd.DataFrame] = []

    def flush() -> None:
        if not buf or dry_run:
            buf.clear()
            return
        batch = pd.concat(buf, ignore_index=True)
        # A stale holiday file and the real file for that day carry the same
        # keys; one INSERT .. ON CONFLICT cannot touch a row twice.
        batch = batch.drop_duplicates(subset=["exchange", "symbol", "series", "date"], keep="last")
        rep.rows_loaded += upsert_eod_raw(batch)
        buf.clear()

    for i, (d, f) in enumerate(files, 1):
        raw = pd.read_parquet(f)
        res = validate_eod(normalize_legacy_frame(raw, d, f.name))
        rep.rows_in += res.counts["rows_in"]
        rep.rows_valid += res.counts["rows_valid"]
        rep.rows_rejected += res.counts["rows_rejected"]
        rep.deliv_per_nulled += res.counts["deliv_per_out_of_range_nulled"]
        rep.date_mismatch_rows += res.counts["date_mismatch_rows"]
        if res.counts["rows_in"] and res.counts["date_mismatch_rows"] / res.counts["rows_in"] > 0.5:
            inner = res.valid["date"].mode()
            rep.stale_files.append({"file": f.name,
                                    "data_date": str(inner.iloc[0]) if len(inner) else None})
        for k, v in res.counts["rejects_by_reason"].items():
            rep.rejects_by_reason[k] = rep.rejects_by_reason.get(k, 0) + v
        if len(rep.rejects_sample) < keep_reject_samples and len(res.rejects):
            take = res.rejects[["symbol", "series", "date", "open", "high", "low", "close", "reason"]]
            rep.rejects_sample.extend(take.head(keep_reject_samples - len(rep.rejects_sample))
                                      .astype(str).to_dict("records"))
        rep.first_date = rep.first_date or d
        rep.last_date = d
        buf.append(res.valid)
        if i % batch_days == 0:
            flush()
    flush()
    return rep


def pick_primary_series(raw: pd.DataFrame) -> pd.DataFrame:
    """One row per (symbol, date): EQ beats BE beats BZ (a stock can move between
    series, e.g. into trade-for-trade, without becoming a different security)."""
    d = raw.copy()
    d["_p"] = d["series"].map(SERIES_PRIORITY).fillna(9)
    d = d.sort_values(["symbol", "date", "_p"]).drop_duplicates(["symbol", "date"], keep="first")
    return d.drop(columns="_p").reset_index(drop=True)
