"""Immutable, versioned Parquet snapshots of the factor matrix.

Layout
------
    <ROOT>/<factor_version>/<snapshot_id>/data-YYYYMM.parquet   (monthly files)
    <ROOT>/<factor_version>/<snapshot_id>/MANIFEST.json         (self-describing)

Columns: symbol, date, known_at, then every factor as float64 (sorted by name).
`known_at` is the instant the row became knowable (session close + 18h IST, the
same rule the old `factor_pit.valid_from` used); readers can pass
`known_before=` to get a strictly point-in-time view.

NaN is preserved. Warm-up NaNs from the fixed 282-session fracdiff kernel are
information, not zeros: trees handle them natively and deep-learning code
imputes explicitly (with a missing indicator). Nothing here fills values.

Determinism: rows sorted by (symbol, date), fixed column order, fixed zstd
settings, no pandas metadata, so identical inputs give byte-identical files and
the same `dataset_hash`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from qsde.factors.versions import FFD_D, FFD_THRES, FFD_VERSION

log = logging.getLogger(__name__)

ROOT = Path(os.getenv("STOXSY_FEATURE_DIR", r"D:\Stoxsy\data\features"))
META_COLS = ("symbol", "date", "known_at")
KNOWABLE_HOURS_IST = 18                 # keep equal to pit_writer.KNOWABILITY_HOURS_AFTER_SESSION
_IST_OFFSET = pd.Timedelta(hours=5, minutes=30)
ZSTD_LEVEL = 3
ROW_GROUP = 131_072

CORPORATE_ACTION_POLICY = {
    "adjusted": "split/bonus/consolidation with a clean ratio (price_gap_fingerprint_v1)",
    "not_adjusted": ["mild bonus 0.80-0.97", "rights", "cash dividends", "demergers", "symbol changes"],
    "unexplained_events": "label windows and feature dates touching an UNEXPLAINED event must be excluded",
    "serving": "flag a symbol whose >=18% gap in the last 3 sessions is not yet confirmed",
    "enforced_by": "label/view layer (not the snapshot)",
}


# ── data classes ─────────────────────────────────────────────────────

@dataclass
class Snapshot:
    snapshot_id: str
    factor_version: str
    universe_version: str
    kind: str
    built_at: datetime
    dataset_hash: str
    n_rows: int
    n_symbols: int
    date_min: Optional[date]
    date_max: Optional[date]
    path: Path
    columns: list = field(default_factory=list)
    manifest: dict = field(default_factory=dict)

    @property
    def files(self) -> list[Path]:
        return sorted(self.path.glob("data-*.parquet"))


# ── helpers ──────────────────────────────────────────────────────────

def known_at_utc(dates: pd.Series | pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Session date -> instant it becomes knowable, in naive UTC."""
    d = pd.DatetimeIndex(pd.to_datetime(dates)).normalize()
    return d + pd.Timedelta(hours=KNOWABLE_HOURS_IST) - _IST_OFFSET


def _sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def git_sha(repo: Optional[Path] = None) -> str:
    repo = repo or Path(__file__).resolve().parents[3]
    try:
        sha = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(repo), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
        return (sha or "unknown") + ("+dirty" if dirty else "")
    except Exception:  # noqa: BLE001
        return "unknown"


def input_fingerprint() -> dict:
    """Cheap checksums of the tables a snapshot is derived from."""
    try:
        from qsde.db.connection import read_sql
        q = read_sql("""
            SELECT
              (SELECT count(*) FROM eod_raw)                                            AS eod_n,
              (SELECT coalesce(round(sum(close)::numeric, 4), 0)::text FROM eod_raw)    AS eod_sum_close,
              (SELECT coalesce(max(date)::text, '') FROM eod_raw)                       AS eod_max_date,
              (SELECT count(*) FROM corp_action_events WHERE applied)                   AS ca_applied,
              (SELECT coalesce(round(sum(price_factor)::numeric, 6), 0)::text
                 FROM corp_action_events WHERE applied)                                 AS ca_sum_factor,
              (SELECT count(*) FROM universe_pit)                                       AS uni_n,
              (SELECT coalesce(sum(rank), 0) FROM universe_pit)                         AS uni_sum_rank
        """).iloc[0].to_dict()
        return {k: (v.item() if hasattr(v, "item") else v) for k, v in q.items()}
    except Exception as e:  # noqa: BLE001
        return {"unavailable": str(e)[:120]}


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# ── writer ───────────────────────────────────────────────────────────

class SnapshotWriter:
    """Accumulate wide factor frames, then write one immutable snapshot."""

    def __init__(self, factor_version: str, universe_version: str, kind: str = "full",
                 root: Path = ROOT, params: Optional[dict] = None,
                 fingerprint: Optional[dict] = None, sha: Optional[str] = None,
                 built_at: Optional[datetime] = None):
        if kind not in ("full", "increment"):
            raise ValueError("kind must be 'full' or 'increment'")
        self.factor_version, self.universe_version, self.kind = factor_version, universe_version, kind
        self.root = Path(root)
        self.params = params or {}
        self.fingerprint = fingerprint
        self.sha = sha
        self.built_at = built_at or datetime.now(timezone.utc)
        self._frames: list[pd.DataFrame] = []

    def add(self, df: pd.DataFrame) -> None:
        """df: engine wide frame - DatetimeIndex (or `date` column) + `symbol` + factor columns."""
        d = df.copy()
        if "date" not in d.columns:
            d = d.reset_index().rename(columns={d.index.name or "index": "date"})
        if "as_of_date" in d.columns and "date" in d.columns:
            d = d.drop(columns=["as_of_date"])
        d["date"] = pd.to_datetime(d["date"]).dt.normalize()
        drop = {"yf_symbol", "source"}
        feats = [c for c in d.columns if c not in set(META_COLS) | drop]
        non_num = [c for c in feats if not np.issubdtype(d[c].dtype, np.number)]
        if non_num:
            raise ValueError(f"non-numeric feature columns: {non_num[:5]}")
        self._frames.append(d[["symbol", "date", *feats]])

    def close(self, register: bool = True) -> Snapshot:
        if not self._frames:
            raise ValueError("no data added")
        all_cols = sorted({c for f in self._frames for c in f.columns if c not in META_COLS})
        parts = []
        for f in self._frames:
            g = f.reindex(columns=["symbol", "date", *all_cols])
            parts.append(g)
        data = pd.concat(parts, ignore_index=True)
        data["symbol"] = data["symbol"].astype(str)
        data = data.drop_duplicates(["symbol", "date"], keep="last")
        data = data.sort_values(["symbol", "date"], kind="mergesort").reset_index(drop=True)
        data[all_cols] = data[all_cols].astype("float64")

        tmp = self.root / self.factor_version / f".tmp-{uuid.uuid4().hex[:10]}"
        tmp.mkdir(parents=True, exist_ok=True)
        files_meta = []
        try:
            months = data["date"].dt.strftime("%Y%m")
            for ym in sorted(months.unique()):
                chunk = data[months == ym]
                table = self._to_table(chunk, all_cols)
                name = f"data-{ym}.parquet"
                pq.write_table(table, tmp / name, compression="zstd", compression_level=ZSTD_LEVEL,
                               row_group_size=ROW_GROUP, use_dictionary=["symbol"],
                               write_statistics=True, data_page_version="2.0")
                files_meta.append({"name": name, "rows": int(len(chunk)),
                                   "sha256": _sha256_file(tmp / name)})
            params = {"fracdiff_d": FFD_D, "fracdiff_thres": FFD_THRES, "fracdiff_auto_widen": False,
                      "knowable_hours_ist": KNOWABLE_HOURS_IST, "zstd_level": ZSTD_LEVEL, **self.params}
            fp = self.fingerprint if self.fingerprint is not None else input_fingerprint()
            sha = self.sha or git_sha()
            hash_in = {"files": [(m["name"], m["sha256"]) for m in files_meta], "params": params,
                       "fingerprint": fp, "columns": all_cols, "policy": CORPORATE_ACTION_POLICY,
                       "factor_version": self.factor_version, "universe_version": self.universe_version}
            dataset_hash = hashlib.sha256(_canon(hash_in).encode()).hexdigest()
            snapshot_id = f"{self.built_at:%Y%m%dT%H%M%SZ}-{dataset_hash[:10]}"
            manifest = {
                "snapshot_id": snapshot_id, "factor_version": self.factor_version,
                "universe_version": self.universe_version, "kind": self.kind,
                "built_at": self.built_at.isoformat(), "git_sha": sha, "dataset_hash": dataset_hash,
                "rows": int(len(data)), "symbols": int(data["symbol"].nunique()),
                "date_min": str(data["date"].min().date()), "date_max": str(data["date"].max().date()),
                "columns": all_cols, "n_features": len(all_cols), "nan_policy": "preserved (never filled)",
                "params": params, "input_fingerprint": fp, "corporate_action_policy": CORPORATE_ACTION_POLICY,
                "files": files_meta,
            }
            (tmp / "MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str))
            final = self.root / self.factor_version / snapshot_id
            if final.exists():
                raise FileExistsError(f"snapshot already exists: {final}")
            tmp.rename(final)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        snap = _snapshot_from_manifest(manifest, final)
        if register:
            _register(snap)
        log.info("snapshot %s: %d rows, %d symbols, %d features, %d files",
                 snap.snapshot_id, snap.n_rows, snap.n_symbols, len(all_cols), len(files_meta))
        return snap

    @staticmethod
    def _to_table(df: pd.DataFrame, feats: list[str]) -> pa.Table:
        known = pa.array(known_at_utc(df["date"]).to_numpy(dtype="datetime64[us]"),
                         type=pa.timestamp("us", tz="UTC"))
        arrays = [pa.array(df["symbol"].to_numpy(dtype=object), type=pa.string()),
                  pa.array(df["date"].dt.date.to_numpy(dtype=object), type=pa.date32()),
                  known]
        names = list(META_COLS)
        for c in feats:
            arrays.append(pa.array(df[c].to_numpy(dtype="float64"), type=pa.float64(), from_pandas=True))
            names.append(c)
        return pa.Table.from_arrays(arrays, names=names)       # no pandas metadata -> deterministic


def persist_increment(df: pd.DataFrame, factor_version: str, universe_version: str = "daily_active_v1",
                      root: Path = ROOT) -> Optional[Snapshot]:
    """Write one incremental snapshot (the daily EOD path)."""
    if df is None or len(df) == 0:
        return None
    w = SnapshotWriter(factor_version, universe_version, kind="increment", root=root)
    w.add(df)
    return w.close()


# ── registry (DB) with disk fallback ─────────────────────────────────

def _snapshot_from_manifest(m: dict, path: Path) -> Snapshot:
    return Snapshot(
        snapshot_id=m["snapshot_id"], factor_version=m["factor_version"],
        universe_version=m["universe_version"], kind=m["kind"],
        built_at=datetime.fromisoformat(m["built_at"]), dataset_hash=m["dataset_hash"],
        n_rows=m["rows"], n_symbols=m["symbols"],
        date_min=date.fromisoformat(m["date_min"]) if m.get("date_min") else None,
        date_max=date.fromisoformat(m["date_max"]) if m.get("date_max") else None,
        path=Path(path), columns=list(m.get("columns", [])), manifest=m)


def _register(s: Snapshot) -> bool:
    """Insert into feature_snapshots; a 'full' snapshot supersedes earlier ones."""
    try:
        from qsde.db.connection import get_sync_conn
        with get_sync_conn() as conn:
            cur = conn.cursor()
            if s.kind == "full":
                cur.execute("UPDATE feature_snapshots SET is_current = FALSE "
                            "WHERE factor_version = %s AND universe_version = %s AND is_current",
                            (s.factor_version, s.universe_version))
            cur.execute("""
                INSERT INTO feature_snapshots (snapshot_id, factor_version, universe_version, kind,
                    built_at, dataset_hash, n_rows, n_symbols, date_min, date_max, path, git_sha,
                    manifest, is_current)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,TRUE)
                ON CONFLICT (snapshot_id) DO NOTHING""",
                        (s.snapshot_id, s.factor_version, s.universe_version, s.kind, s.built_at,
                         s.dataset_hash, s.n_rows, s.n_symbols, s.date_min, s.date_max, str(s.path),
                         s.manifest.get("git_sha"), json.dumps(s.manifest, default=str)))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("feature_snapshots registration skipped (%s); the MANIFEST.json is still authoritative", e)
        return False


def mark_not_current(snapshot_id: str) -> None:
    from qsde.db.connection import execute_sql
    execute_sql("UPDATE feature_snapshots SET is_current = FALSE WHERE snapshot_id = %s", (snapshot_id,))


def all_snapshots(factor_version: str = FFD_VERSION, root: Path = ROOT) -> list[Snapshot]:
    """Every snapshot found on disk for a version, oldest first."""
    out = []
    base = Path(root) / factor_version
    if not base.exists():
        return out
    for mf in sorted(base.glob("*/MANIFEST.json")):
        try:
            out.append(_snapshot_from_manifest(json.loads(mf.read_text()), mf.parent))
        except Exception as e:  # noqa: BLE001
            log.warning("unreadable manifest %s: %s", mf, e)
    return sorted(out, key=lambda s: s.built_at)


def current_snapshots(factor_version: str = FFD_VERSION, universe_version: Optional[str] = None,
                      root: Path = ROOT) -> list[Snapshot]:
    """Snapshots the reader should union: is_current in the DB, else (DB down) the
    newest 'full' per universe_version plus later increments."""
    disk = {s.snapshot_id: s for s in all_snapshots(factor_version, root)}
    # The DB registry describes the default store only; a custom root (tests,
    # scratch builds) is resolved purely from its own manifests.
    if Path(root) == ROOT:
        try:
            from qsde.db.connection import read_sql
            q = ("SELECT snapshot_id FROM feature_snapshots WHERE factor_version = :v AND is_current"
                 + (" AND universe_version = :u" if universe_version else ""))
            ids = read_sql(q, params={"v": factor_version, "u": universe_version})["snapshot_id"].tolist()
            got = [disk[i] for i in ids if i in disk]
            if got or ids:
                if ids and not got:
                    log.warning("feature_snapshots lists %d current snapshot(s) for %s but none is on disk",
                                len(ids), factor_version)
                return sorted(got, key=lambda s: s.built_at)
        except Exception as e:  # noqa: BLE001
            log.debug("DB registry unavailable (%s) - resolving current snapshots from disk", e)
    res = []
    for u in sorted({s.universe_version for s in disk.values()}):
        if universe_version and u != universe_version:
            continue
        seq = [s for s in disk.values() if s.universe_version == u]
        seq.sort(key=lambda s: s.built_at)
        last_full = max((i for i, s in enumerate(seq) if s.kind == "full"), default=0)
        res.extend(seq[last_full:])
    return sorted(res, key=lambda s: s.built_at)


# ── reader ───────────────────────────────────────────────────────────

def read_features(factor_version: str = FFD_VERSION, symbols: Optional[Sequence[str]] = None,
                  start: Optional[date | str] = None, end: Optional[date | str] = None,
                  columns: Optional[Iterable[str]] = None, known_before: Optional[datetime | str] = None,
                  universe_version: Optional[str] = None, snapshot_ids: Optional[Sequence[str]] = None,
                  root: Path = ROOT) -> pd.DataFrame:
    """Wide frame: symbol, as_of_date, known_at + requested factors. NaN preserved.

    Unions the CURRENT snapshots (or `snapshot_ids` exactly, for reproducible runs);
    where (symbol,date) overlap the newest built_at wins.
    """
    snaps = ([s for s in all_snapshots(factor_version, root) if s.snapshot_id in set(snapshot_ids)]
             if snapshot_ids else current_snapshots(factor_version, universe_version, root))
    if not snaps:
        return pd.DataFrame(columns=["symbol", "as_of_date", "known_at"])
    want = None if columns is None else list(dict.fromkeys(["symbol", "date", "known_at", *columns]))
    flt = []
    if symbols is not None:
        flt.append(("symbol", "in", list(symbols)))
    if start is not None:
        flt.append(("date", ">=", pd.Timestamp(start).date()))
    if end is not None:
        flt.append(("date", "<=", pd.Timestamp(end).date()))
    if known_before is not None:
        flt.append(("known_at", "<=", pd.Timestamp(known_before, tz="UTC") if pd.Timestamp(known_before).tzinfo is None
                    else pd.Timestamp(known_before).tz_convert("UTC")))
    frames = []
    for s in snaps:
        files = s.files
        if not files:
            continue
        # prune whole months that cannot match the date filter
        if start is not None or end is not None:
            lo = pd.Timestamp(start).strftime("%Y%m") if start is not None else "000000"
            hi = pd.Timestamp(end).strftime("%Y%m") if end is not None else "999999"
            files = [f for f in files if lo <= f.stem.split("-")[1] <= hi]
            if not files:
                continue
        cols = None
        if want is not None:
            present = set(s.columns) | set(META_COLS)
            cols = [c for c in want if c in present]
        t = pq.read_table([str(f) for f in files], columns=cols, filters=flt or None)
        df = t.to_pandas()
        df["_built_at"] = s.built_at
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["symbol", "as_of_date", "known_at"])
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["symbol", "date", "_built_at"], kind="mergesort")
    out = out.drop_duplicates(["symbol", "date"], keep="last").drop(columns="_built_at")
    out = out.rename(columns={"date": "as_of_date"}).reset_index(drop=True)
    return out


def read_latest_rows(max_age_days: int = 20, asof: Optional[date | str] = None,
                     symbols: Optional[Sequence[str]] = None, columns: Optional[Iterable[str]] = None,
                     factor_version: str = FFD_VERSION, root: Path = ROOT) -> pd.DataFrame:
    """One row per symbol: the newest row on or before `asof` within `max_age_days`.

    Takes a single date's row per symbol (no per-factor stitching across days:
    that mixed vintages, AUDIT M2). NaN preserved.
    """
    asof_ts = pd.Timestamp(asof) if asof is not None else pd.Timestamp.today().normalize()
    df = read_features(factor_version, symbols=symbols, start=(asof_ts - pd.Timedelta(days=max_age_days)).date(),
                       end=asof_ts.date(), columns=columns, root=root)
    if df.empty:
        return df
    return df.sort_values(["symbol", "as_of_date"]).groupby("symbol", as_index=False).tail(1).reset_index(drop=True)


def latest_factor(name: str, symbols: Optional[Sequence[str]] = None, max_age_days: int = 20,
                  factor_version: str = FFD_VERSION, root: Path = ROOT) -> pd.Series:
    """Latest value of one factor per symbol (index = symbol); symbols with no value are absent."""
    df = read_features(factor_version, symbols=symbols, columns=[name],
                       start=(pd.Timestamp.today().normalize() - pd.Timedelta(days=max_age_days)).date(), root=root)
    if df.empty or name not in df.columns:
        return pd.Series(dtype="float64", name=name)
    df = df.dropna(subset=[name]).sort_values(["symbol", "as_of_date"])
    return df.groupby("symbol")[name].last()


def feature_columns(factor_version: str = FFD_VERSION, root: Path = ROOT) -> list[str]:
    snaps = current_snapshots(factor_version, root=root)
    return sorted({c for s in snaps for c in s.columns})
