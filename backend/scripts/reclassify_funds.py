"""Recompute fund classification for every scheme from the stored AMFI headings and names (no refetch), and set the lifecycle status.

    python scripts/reclassify_funds.py
Idempotent. lifecycle: single_nav (only one NAV ever), recent_launch (first NAV within 45 days of the latest NAV date), inactive_no_recent_nav (last NAV
more than 30 days before the latest), active otherwise.
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import pandas as pd


def main() -> int:
    import psycopg2.extras

    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.funds.classify import classify

    sm = read_sql("SELECT scheme_code, name, category_raw, plan, option_type, first_nav_date, last_nav_date FROM mf_scheme_master")
    nn = read_sql("SELECT scheme_code, count(*) AS n FROM mf_nav GROUP BY 1")
    sm = sm.merge(nn, on="scheme_code", how="left")
    latest = pd.to_datetime(sm["last_nav_date"]).max()
    rows = []
    for r in sm.itertuples():
        c = classify(r.category_raw, r.name, r.plan, r.option_type)
        first, last = pd.Timestamp(r.first_nav_date), pd.Timestamp(r.last_nav_date)
        life = ("single_nav" if (r.n == 1) else "inactive_no_recent_nav" if (latest - last).days > 30 else "recent_launch" if (latest - first).days <= 45 else "active")
        rows.append((int(r.scheme_code), c["scheme_type"], c["broad_category"], c["sub_category"], c["asset_class"], c["fund_group"], c["unclassified_reason"], c["is_etf"], c["is_index"],
                     c["is_liquid"], c["is_fof"], c["is_direct"], c["is_growth"], c["classification_evidence"], life))
    with get_sync_conn() as conn:
        cur = conn.cursor()
        psycopg2.extras.execute_values(cur, """UPDATE mf_scheme_master m SET scheme_type = v.scheme_type, broad_category = v.broad_category, sub_category = v.sub_category,
            asset_class = v.asset_class, fund_group = v.fund_group, unclassified_reason = v.unclassified_reason, is_etf = v.is_etf, is_index = v.is_index, is_liquid = v.is_liquid,
            is_fof = v.is_fof, is_direct = COALESCE(v.is_direct, m.is_direct), is_growth = COALESCE(v.is_growth, m.is_growth), classification_evidence = v.ev, lifecycle = v.life
            FROM (VALUES %s) AS v(scheme_code, scheme_type, broad_category, sub_category, asset_class, fund_group, unclassified_reason, is_etf, is_index, is_liquid, is_fof, is_direct,
            is_growth, ev, life) WHERE m.scheme_code = v.scheme_code""", rows, page_size=2000)
    out = read_sql("SELECT asset_class, fund_group, count(*) n FROM mf_scheme_master GROUP BY 1, 2 ORDER BY 3 DESC")
    print(out.to_string(index=False))
    print(read_sql("SELECT lifecycle, count(*) n FROM mf_scheme_master GROUP BY 1").to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
