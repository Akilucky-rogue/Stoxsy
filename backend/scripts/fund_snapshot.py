"""Rebuild the per-scheme snapshot (returns with statuses, risk, data-quality badge, peer context) for every active / recently launched scheme.

    python scripts/fund_snapshot.py
Run after every NAV load. Idempotent: the table is a derived copy and is replaced wholesale in one transaction.
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd
import psycopg2.extras

CHUNK = 400


def main() -> int:
    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.funds.snapshot import add_peer_context, scheme_snapshot

    t0 = time.time()
    sm = read_sql("SELECT * FROM mf_scheme_master WHERE lifecycle IN ('active', 'recent_launch') ORDER BY scheme_code")
    last = pd.Timestamp(read_sql("SELECT max(date) AS d FROM mf_nav")["d"].iloc[0])
    start = (last - pd.DateOffset(years=6) - pd.Timedelta(days=10)).date()
    rows = []
    for i in range(0, len(sm), CHUNK):
        part = sm.iloc[i:i + CHUNK]
        nav = read_sql("SELECT scheme_code, date, nav FROM mf_nav WHERE scheme_code = ANY(:c) AND date >= CAST(:s AS DATE) ORDER BY scheme_code, date",
                       params={"c": [int(c) for c in part["scheme_code"]], "s": str(start)})
        nav["date"] = pd.to_datetime(nav["date"])
        by = {c: g.set_index("date")["nav"] for c, g in nav.groupby("scheme_code")}
        for m in part.to_dict("records"):
            s = by.get(int(m["scheme_code"]))
            if s is None or len(s) < 2:
                continue
            snap = scheme_snapshot(s, m)
            if snap:
                rows.append(snap)
        print(f"  {min(i + CHUNK, len(sm))}/{len(sm)} schemes, {time.time() - t0:.0f}s", flush=True)
    add_peer_context(rows)
    J = psycopg2.extras.Json
    vals = [(r["scheme_code"], r["as_of"], r["basis"], J(r["returns"]), J(r["risk"]), J(r["quality"]), J(r["peers"]) if r["peers"] else None,
             r["ret_1y"], r["ret_3y"], r["ret_5y"], r["vol_3y"], r["maxdd_3y"], r["quality_level"]) for r in rows]
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("DELETE FROM mf_fund_snapshot")
        psycopg2.extras.execute_values(cur, "INSERT INTO mf_fund_snapshot (scheme_code, as_of, basis, returns, risk, quality, peers, ret_1y, ret_3y, ret_5y, vol_3y, maxdd_3y, quality_level) VALUES %s", vals, page_size=500)
    q = pd.Series([r["quality_level"] for r in rows]).value_counts().to_dict()
    p = sum(1 for r in rows if r["peers"])
    print(f"snapshot: {len(rows):,} schemes in {time.time() - t0:.0f}s; quality {q}; with peer context {p:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
