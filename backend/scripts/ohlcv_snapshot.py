"""Retain the current official `ohlcv` series as a versioned, hashed snapshot BEFORE anything changes it.

    python scripts/ohlcv_snapshot.py --reason "before Bucket A rebuild (Option B)"

Writes data/ohlcv_versions/ohlcv_<UTC timestamp>_<events fingerprint>.parquet plus a manifest JSON (row and symbol counts, date range, SHA-256 of the file, the corporate-action
events fingerprint, the Bucket A decisions fingerprint) and records the version in the `ohlcv_versions` table. Read-only on `ohlcv`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from qsde.config.paths import data_path

OUT = data_path("ohlcv_versions")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(reason: str) -> dict:
    import psycopg2.extras

    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.ingestion import bucket_a_decisions as BA, ca_series

    df = read_sql("SELECT symbol, date, open, high, low, close, volume, source FROM ohlcv ORDER BY symbol, date")
    fp = ca_series.applied_events_fingerprint()
    OUT.mkdir(parents=True, exist_ok=True)
    vid = f"ohlcv_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{fp}"
    path = OUT / f"{vid}.parquet"
    df.to_parquet(path, index=False, compression="zstd")
    sha = sha256_file(path)
    man = {"version_id": vid, "reason": reason, "created_utc": datetime.now(timezone.utc).isoformat(), "n_rows": int(len(df)), "n_symbols": int(df["symbol"].nunique()),
           "date_min": str(df["date"].min()), "date_max": str(df["date"].max()), "sha256": sha, "events_fingerprint": fp, "bucket_a_decisions_fingerprint": BA.fingerprint(),
           "series_label": ca_series.OHLCV_LABEL, "file": path.name, "bytes": path.stat().st_size}
    (OUT / f"{vid}.manifest.json").write_text(json.dumps(man, indent=2), encoding="utf-8")
    with get_sync_conn() as c:
        c.cursor().execute("INSERT INTO ohlcv_versions (version_id, reason, events_fingerprint, n_rows, n_symbols, date_min, date_max, sha256, path, policy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                           "ON CONFLICT (version_id) DO NOTHING", (vid, reason, fp, man["n_rows"], man["n_symbols"], df["date"].min(), df["date"].max(), sha, str(path),
                                                                   psycopg2.extras.Json({"label": ca_series.OHLCV_LABEL, "bucket_a": BA.fingerprint()})))
    return man


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reason", required=True)
    a = ap.parse_args()
    m = snapshot(a.reason)
    print(f"{m['version_id']}: {m['n_rows']:,} rows, {m['n_symbols']:,} symbols, {m['date_min']} .. {m['date_max']}, {m['bytes'] / 1e6:,.0f} MB, sha256 {m['sha256'][:16]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
