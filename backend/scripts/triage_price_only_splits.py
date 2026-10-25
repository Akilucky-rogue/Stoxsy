"""One-pass triage of the split/bonus events that the official `ohlcv` adjusts (price-confirmed v2) but the reviewed series does not.

These are events where a price gap was found and (separately) an NSE record exists, but ca_reconcile_v1 classified the NSE record
`needs_review` (gap outside the hard bounds, price did not persist, ...). The triage only ORDERS them for a human decision; it
approves nothing and creates no tier.

    bucket A  reviewable : the applied factor equals the NSE-listed factor (<=1%), the observed factor is within 10%, the gap is
                           within 5 sessions of the NSE ex-date and there are <= 5 untraded sessions. Fails only on tolerance/persistence.
    bucket B  unlikely   : price did not persist, or the observed factor is >25% from the listed one, or the listed factor
                           disagrees with the applied one.
    bucket C  evidence   : everything else (long untraded window, large timing shift, no matching NSE record).

    python scripts/triage_price_only_splits.py --version sbadj_v1-34759406b1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")


def classify(r) -> str:
    reason = str(r.reason or "")
    listed, applied = r.listed_factor, r.applied_factor
    if pd.isna(listed):
        return "C"
    if "did not persist" in reason:
        return "B"
    if abs(listed / applied - 1) > 0.01:
        return "B"
    err = r.factor_error
    if pd.notna(err) and err > 0.25:
        return "B"
    ok_timing = pd.notna(r.shift_sessions) and abs(r.shift_sessions) <= 5
    ok_untraded = pd.isna(r.untraded_sessions) or r.untraded_sessions <= 5
    if pd.notna(err) and err <= 0.10 and ok_timing and ok_untraded:
        return "A"
    return "C"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    args = ap.parse_args()
    from qsde.db.connection import read_sql

    applied = read_sql("""SELECT e.symbol, e.ex_date AS gap_date, e.price_factor AS applied_factor FROM corp_action_events e
                          WHERE e.applied AND e.method = 'source_confirmed_v2'""")
    in_new = read_sql("SELECT symbol, gap_date FROM adjustment_events_applied WHERE version = :v", params={"v": args.version})
    key = lambda d: d["symbol"] + "|" + pd.to_datetime(d["gap_date"]).dt.strftime("%Y-%m-%d")      # noqa: E731
    only = applied[~key(applied).isin(key(in_new))].copy()
    rec = read_sql("""SELECT r.symbol_of_day AS symbol, r.gap_date, r.ex_date AS nse_ex_date, r.listed_factor, r.observed_ratio,
                             r.factor_error, r.shift_sessions, r.untraded_sessions, r.classification, r.reason, c.isin, c.subject
                      FROM corp_action_reconciliation r JOIN nse_corporate_actions c ON c.id = r.action_id
                      WHERE r.reconciler_version = 'ca_reconcile_v1' AND r.category IN ('SPLIT_BONUS', 'CONSOLIDATION')""")
    only["k"], rec["k"] = key(only), key(rec)
    m = only.merge(rec.drop(columns=["symbol", "gap_date"]), on="k", how="left")
    m["bucket"] = [classify(r) for r in m.itertuples()]
    # several NSE rows can point at one (symbol, gap bar): keep the best-matching one (smallest factor error)
    m = m.sort_values(["factor_error"], na_position="last").drop_duplicates("k").drop(columns="k")
    m = m.sort_values(["bucket", "gap_date", "symbol"])
    OUT.mkdir(parents=True, exist_ok=True)
    m.to_csv(OUT / "triage_price_only_splits.csv", index=False)
    cnt = m["bucket"].value_counts().to_dict()
    L = [f"# Triage of {len(m)} price-only split/bonus events (adjusted in `ohlcv`, absent from `{args.version}`)", "",
         "Ordering aid only - nothing is approved and no tier is created. Decisions go in `infra/data/corp_action_approvals.json`.", "",
         f"- bucket A (reviewable, fails only tolerance/persistence): **{cnt.get('A', 0)}**",
         f"- bucket B (unlikely: no persistence / big factor error / listed factor disagrees): **{cnt.get('B', 0)}**",
         f"- bucket C (needs evidence: long untraded window, timing shift, or no matching NSE record): **{cnt.get('C', 0)}**", "",
         "## Bucket A - for your decision", "", "| symbol | gap bar | NSE ex-date | applied x | observed x | factor err | shift | untraded | NSE subject |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in m[m.bucket == "A"].itertuples():
        L.append(f"| {r.symbol} | {r.gap_date} | {r.nse_ex_date} | {r.applied_factor:.3f} | {r.observed_ratio:.3f} | {r.factor_error:.1%} | "
                 f"{int(r.shift_sessions)} | {'' if pd.isna(r.untraded_sessions) else int(r.untraded_sessions)} | {str(r.subject)[:55]} |")
    (OUT / "triage_price_only_splits.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:9]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
