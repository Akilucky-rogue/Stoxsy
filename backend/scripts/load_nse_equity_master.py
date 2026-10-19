"""Load NSE's EQUITY_L.csv (already landed raw and hashed) into nse_equity_master.

    python scripts/load_nse_equity_master.py --landed <path to the landed csv>
The file is the source of truth for the NSE symbol -> ISIN map. The load is a replace-by-symbol upsert; the raw file, its SHA-256 and the retrieval time
(`known_at`, from the landing sidecar) are stored with every row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--landed", required=True)
    args = ap.parse_args()
    import psycopg2.extras

    from qsde.db.connection import get_sync_conn

    p = Path(args.landed)
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    side = json.loads(p.with_name(p.stem + ".meta.json").read_text(encoding="utf-8"))
    known_at = pd.Timestamp(side["fetched_at"])
    df = pd.read_csv(p, skipinitialspace=True, dtype=str)
    df.columns = [c.strip().upper() for c in df.columns]
    df["SYMBOL"] = df["SYMBOL"].str.strip().str.upper()
    df["ISIN NUMBER"] = df["ISIN NUMBER"].str.strip()
    ok = df["ISIN NUMBER"].str.match(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$", na=False)
    bad = int((~ok).sum())
    df = df[ok].drop_duplicates("SYMBOL")
    rows = [(r["SYMBOL"], str(r["NAME OF COMPANY"]).strip(), str(r["SERIES"]).strip(),
             pd.to_datetime(r["DATE OF LISTING"], format="%d-%b-%Y", errors="coerce"), r["ISIN NUMBER"],
             pd.to_numeric(r["FACE VALUE"], errors="coerce"), sha, known_at.to_pydatetime()) for _, r in df.iterrows()]
    rows = [(s, n, se, None if pd.isna(d) else d.date(), i, None if pd.isna(fv) else float(fv), sh, k) for s, n, se, d, i, fv, sh, k in rows]
    with get_sync_conn() as conn:
        psycopg2.extras.execute_values(conn.cursor(), """INSERT INTO nse_equity_master (symbol, name, series, listing_date, isin, face_value, source_sha256, known_at)
            VALUES %s ON CONFLICT (symbol) DO UPDATE SET name = EXCLUDED.name, series = EXCLUDED.series, listing_date = EXCLUDED.listing_date,
            isin = EXCLUDED.isin, face_value = EXCLUDED.face_value, source_sha256 = EXCLUDED.source_sha256, known_at = EXCLUDED.known_at, loaded_at = NOW()""", rows)
    print(f"loaded {len(rows)} NSE equities (rows without a valid ISIN skipped: {bad}); sha256 {sha[:12]}; known_at {known_at}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
