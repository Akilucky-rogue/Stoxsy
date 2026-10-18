"""Reconcile the NSE corporate-actions history (table nse_corporate_actions) against prices and the security map.

EVIDENCE ONLY: writes `corp_action_reconciliation` and reports; never touches `ohlcv`. Report order follows the owner's priority:
  1. rename + split/bonus cases (incl. the 25 renames that coincide with a price event)
  2. dividends   3. rights   4. demergers / other
and cross-checks the items held for manual review (UEL, DRCSYSTEMS, the 3-5% rename proposals) against the API record
(official ex-date, record date, face value, ISIN).

    python scripts/reconcile_ca_history.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\corp_actions")


def load_security_map():
    from qsde.db.connection import read_sql
    from qsde.ingestion.security_master import SecurityMap

    iv = read_sql("SELECT security_id, symbol, valid_from, valid_to FROM security_master")
    iv["valid_from"] = pd.to_datetime(iv["valid_from"])
    iv["valid_to"] = pd.to_datetime(iv["valid_to"].map(lambda d: d if d.year < 2200 else pd.Timestamp("2200-01-01").date()))
    return SecurityMap(iv)


def main() -> int:
    from qsde.db.connection import execute_sql, read_sql
    from qsde.ingestion import nse_eod
    from qsde.ingestion.ca_reconcile import VERSION, reconcile_actions

    ca = read_sql("SELECT id, symbol, series, isin, company, subject, category, factor, face_value, ex_date, record_date "
                  "FROM nse_corporate_actions ORDER BY ex_date, symbol")
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])
    print(f"{len(ca):,} NSE corporate-action rows, {ca['ex_date'].min().date()} .. {ca['ex_date'].max().date()}")
    smap = load_security_map()
    raw = read_sql("SELECT exchange, symbol, series, date, open, close FROM eod_raw")
    raw["date"] = pd.to_datetime(raw["date"])
    prim = nse_eod.pick_primary_series(raw)
    cal = np.sort(prim["date"].unique())

    res = reconcile_actions(ca, prim, smap, cal)
    execute_sql("DELETE FROM corp_action_reconciliation WHERE reconciler_version = %(v)s", {"v": VERSION})
    nn = lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NaT else v          # noqa: E731
    from qsde.db.connection import get_sync_conn
    from psycopg2.extras import execute_values

    rows = [(int(r.action_id), VERSION, r.category, r.symbol, nn(r.symbol_of_day), nn(r.security_id), r.ex_date, r.classification,
             nn(r.reason), nn(r.listed_factor), nn(r.observed_ratio), nn(r.factor_error), nn(r.gap_date),
             None if nn(r.shift_sessions) is None else int(r.shift_sessions),
             None if nn(r.untraded_sessions) is None else int(r.untraded_sessions), nn(r.amount), nn(r.yield_pct), nn(r.terms))
            for r in res.itertuples()]
    with get_sync_conn() as conn:
        execute_values(conn.cursor(), """INSERT INTO corp_action_reconciliation (action_id, reconciler_version, category, symbol,
            symbol_of_day, security_id, ex_date, classification, reason, listed_factor, observed_ratio, factor_error, gap_date,
            shift_sessions, untraded_sessions, amount, yield_pct, terms) VALUES %s ON CONFLICT DO NOTHING""", rows, page_size=2000)

    L = [f"# NSE corporate-action reconciliation ({VERSION}) - evidence only, no price changed", "",
         f"{len(res):,} actions, {ca['ex_date'].min().date()} .. {ca['ex_date'].max().date()}; prices through {prim['date'].max().date()}", ""]
    tab = res.pivot_table(index="category", columns="classification", values="action_id", aggfunc="count", fill_value=0)
    L += ["| category | confirmed | needs_review | rejected |", "|---|---|---|---|"]
    for cat in ["SPLIT_BONUS", "CONSOLIDATION", "DIVIDEND", "RIGHTS", "DEMERGER", "OTHER"]:
        if cat in tab.index:
            r = tab.loc[cat]
            L.append(f"| {cat} | {int(r.get('confirmed', 0))} | {int(r.get('needs_review', 0))} | {int(r.get('rejected', 0))} |")

    # 1. rename + split/bonus: the 25 renames that coincide with a price event
    ho = pd.read_csv(OUT / "handoff_with_price_event.csv", parse_dates=["effective_date"]) if (OUT / "handoff_with_price_event.csv").exists() else pd.DataFrame()
    props = []
    for r in ho.itertuples():
        near = ca[ca["symbol"].isin([r.old_symbol, r.new_symbol]) & ca["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"]) &
                  ((ca["ex_date"] - r.effective_date).abs() <= pd.Timedelta(days=10))]
        jump = float(r.reason.split("(")[1].split("%")[0]) / 100.0
        for a in near.itertuples():
            props.append({"old_symbol": r.old_symbol, "new_symbol": r.new_symbol, "rename_date": r.effective_date.date(),
                          "observed_jump": jump, "nse_ex_date": a.ex_date.date(), "nse_subject": a.subject, "nse_factor": a.factor,
                          "factor_error": abs((1 + jump) / a.factor - 1) if a.factor else None, "isin": a.isin, "record_date": a.record_date})
    pr = pd.DataFrame(props)
    pr.to_csv(OUT / "rename_split_candidates_from_api.csv", index=False)
    L += ["", "## 1. Renames that coincide with a price event (the 25 unstitched cases)", "",
          f"API actions found near {pr['old_symbol'].nunique() if len(pr) else 0} of {len(ho)} of them "
          f"(the API history may not reach back that far); {int((pr['factor_error'] < 0.03).sum()) if len(pr) else 0} match the observed jump "
          f"within 3%. Detail: rename_split_candidates_from_api.csv"]
    for r in (pr.sort_values("factor_error").head(15).itertuples() if len(pr) else []):
        L.append(f"- {r.old_symbol} -> {r.new_symbol} on {r.rename_date}: jump {r.observed_jump:+.1%}; NSE {r.nse_ex_date} "
                 f"'{r.nse_subject}' factor {r.nse_factor}; error {r.factor_error:.1%}")

    # held items vs the official record
    held = [("UEL", "2025-10-13"), ("DRCSYSTEMS", "2022-03-21"), ("NDRAUTO", "2023-08-21"), ("VERTOZ", "2025-07-11")]
    L += ["", "## Held items vs the NSE API record", ""]
    for sym, gap in held:
        near = ca[(ca["symbol"] == sym) & ((ca["ex_date"] - pd.Timestamp(gap)).abs() <= pd.Timedelta(days=60))]
        L.append(f"- {sym} (gap bar {gap}): " + (" | ".join(f"{a.ex_date.date()} {a.subject} record {a.record_date} FV {a.face_value} {a.isin}"
                                                          for a in near.itertuples()) or "no API record in the window"))
    for cat, title in [("DIVIDEND", "2. Dividends"), ("RIGHTS", "3. Rights"), ("DEMERGER", "4. Demergers")]:
        sub = res[res["category"] == cat]
        why = sub["reason"].str.replace(r"[0-9.]+%?", "N", regex=True).str[:70].value_counts().head(6)
        L += ["", f"## {title}: {len(sub):,}", "", *(f"- {n}x {k}" for k, n in why.items())]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "ca_history_reconciliation.md").write_text("\n".join(L), encoding="utf-8")
    res.to_csv(OUT / "ca_history_reconciliation.csv", index=False)
    print("\n".join(L[:14]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
