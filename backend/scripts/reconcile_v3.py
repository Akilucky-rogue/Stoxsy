"""source_confirmed_v3: build the security map from the OFFICIAL NSE list, evaluate every SOURCE_UNCONFIRMED listing under the
rename-aware / skipped-session rules, store the candidates and write the before/after diff report. READ-ONLY for the official
series: no event is applied here. Candidates become applied only through owner approvals (infra/data/corp_action_approvals.json).

    python scripts/reconcile_v3.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")


def main() -> int:
    from qsde.db.connection import execute_sql, read_sql
    from qsde.ingestion import nse_eod
    from qsde.ingestion.corp_actions_v3 import VERSION, evaluate
    from qsde.ingestion.security_master import build_security_map

    # 1. security map from the official list + archive hand-off check
    off = read_sql("SELECT old_symbol, new_symbol, effective_date FROM symbol_changes "
                   "WHERE verification_status = 'official' AND change_type = 'symbol_change' AND source = 'nse_symbolchange_csv'")
    syms = sorted(set(off["old_symbol"]) | set(off["new_symbol"]))
    bars = read_sql("SELECT symbol, min(date) AS first_bar, max(date) AS last_bar FROM eod_raw WHERE symbol = ANY(:s) GROUP BY symbol",
                    params={"s": syms})
    # adjusted prices at the two ends of every series, for the hand-off price-continuity gate
    fo = read_sql("SELECT DISTINCT ON (symbol) symbol, open AS first_open FROM ohlcv WHERE symbol = ANY(:s) ORDER BY symbol, date ASC",
                  params={"s": syms})
    lc = read_sql("SELECT DISTINCT ON (symbol) symbol, close AS last_close FROM ohlcv WHERE symbol = ANY(:s) ORDER BY symbol, date DESC",
                  params={"s": syms})
    bars = bars.merge(fo, on="symbol", how="left").merge(lc, on="symbol", how="left")
    cal = pd.to_datetime(read_sql("SELECT DISTINCT date FROM eod_raw ORDER BY date")["date"]).to_numpy()
    applied = read_sql("SELECT symbol, ex_date, price_factor, applied FROM corp_action_events WHERE applied")
    smap = build_security_map(off, bars, cal, applied)
    execute_sql("DELETE FROM security_master")
    for r in smap.intervals.itertuples():
        execute_sql("INSERT INTO security_master (security_id, symbol, valid_from, valid_to, source) VALUES (%(i)s,%(s)s,%(f)s,%(t)s,'nse_symbolchange_csv')",
                    {"i": r.security_id, "s": r.symbol, "f": r.valid_from.date(), "t": r.valid_to.date()})
    print(f"security map: {smap.intervals['security_id'].nunique()} stitched securities / {len(smap.intervals)} symbol intervals; "
          f"{len(smap.rejected)} official rows failed the hand-off check; {len(smap.conflicts)} conflicts")

    # renames that coincide with a price event: kept separate, listed with the NSE actions near the date for individual review
    from qsde.ingestion.corp_actions_source import load_source
    hj = smap.rejected[smap.rejected["reason"].str.contains("price discontinuity")].copy()
    src = load_source()
    rows = []
    for r in hj.itertuples():
        near = src[src["symbol"].isin([r.old_symbol, r.new_symbol]) &
                   ((src["ex_date"] - pd.Timestamp(r.effective_date)).abs() <= pd.Timedelta(days=10))]
        rows.append({"old_symbol": r.old_symbol, "new_symbol": r.new_symbol, "effective_date": r.effective_date.date(),
                     "reason": r.reason, "nse_actions_within_10d": " | ".join(
                         f"{x.symbol} {x.ex_date.date()} {x.purpose}"[:90] for x in near.itertuples()) or "none in the feed"})
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT / "handoff_with_price_event.csv", index=False)
    print(f"{len(rows)} renames coincide with a price event -> not stitched; listed in handoff_with_price_event.csv")

    # 2. the listings v2 left unconfirmed (listed date recovered from the stored shift)
    ev = read_sql("SELECT symbol, ex_date, source_purpose, ex_date_shift FROM corp_action_events WHERE kind = 'SOURCE_UNCONFIRMED' "
                  "ORDER BY symbol, ex_date")
    ev["ex_date"] = pd.to_datetime(ev["ex_date"])
    pos = pd.Series(np.arange(len(cal)), index=pd.DatetimeIndex(cal))
    listed = []
    for r in ev.itertuples():
        i = int(pos.reindex([r.ex_date], method="bfill").iloc[0])
        sh = 0 if pd.isna(r.ex_date_shift) else int(r.ex_date_shift)
        listed.append(pd.Timestamp(cal[max(0, min(len(cal) - 1, i - sh))]))
    unc = pd.DataFrame({"listed_symbol": ev["symbol"], "listed_ex_date": listed, "purpose": ev["source_purpose"]})

    # 3. price bars for every symbol that could have traded those names
    need = set(unc["listed_symbol"])
    for s in list(need):
        for iv in smap.intervals[smap.intervals["security_id"] == (smap.security_of(s) or "")].itertuples():
            need.add(iv.symbol)
    raw = read_sql("SELECT exchange, symbol, series, date, open, close FROM eod_raw WHERE symbol = ANY(:s)", params={"s": sorted(need)})
    raw["date"] = pd.to_datetime(raw["date"])
    prim = nse_eod.pick_primary_series(raw)
    res = evaluate(unc, prim, smap, calendar=cal)

    execute_sql("DELETE FROM corp_action_candidates WHERE reconciler_version = %(v)s", {"v": VERSION})
    for r in res.itertuples():
        nn = lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else v        # noqa: E731
        execute_sql("""INSERT INTO corp_action_candidates (reconciler_version, listed_symbol, symbol_of_day, security_id,
                          listed_ex_date, gap_date, purpose, listed_factor, observed_ratio, factor_error, shift_sessions,
                          untraded_sessions, persistence_ratio, classification, reason)
                       VALUES (%(v)s,%(ls)s,%(sd)s,%(sid)s,%(le)s,%(gd)s,%(p)s,%(lf)s,%(or)s,%(fe)s,%(sh)s,%(ut)s,%(pr)s,%(c)s,%(rs)s)
                       ON CONFLICT DO NOTHING""",
                    {"v": VERSION, "ls": r.listed_symbol, "sd": nn(r.symbol_of_day), "sid": nn(r.security_id), "le": r.listed_ex_date,
                     "gd": nn(r.gap_date), "p": r.purpose, "lf": nn(r.listed_factor), "or": nn(r.observed_ratio),
                     "fe": nn(r.factor_error), "sh": nn(r.shift_sessions), "ut": nn(r.untraded_sessions),
                     "pr": nn(r.persistence_ratio), "c": r.classification, "rs": r.reason})

    # 4. before / after diff against the official (v2 + approvals) events
    ok = res[res["classification"] == "v3_candidate"]
    uni = set(read_sql("SELECT DISTINCT symbol FROM universe_pit WHERE adv_cr >= 10")["symbol"])
    cur = read_sql("SELECT kind, applied, count(*) n FROM corp_action_events GROUP BY 1, 2 ORDER BY 1, 2")
    touched = sorted(set(ok["symbol_of_day"]))
    renamed = ok[ok["symbol_of_day"] != ok["listed_symbol"]]
    L = ["# source_confirmed_v3 - before/after diff (no event applied)", "",
         f"- official series today (v2 + owner approvals): {int(cur[cur.applied].n.sum())} applied events",
         "- events CHANGED by v3: 0 | events REMOVED: 0 (v3 never touches the official series)",
         f"- events v3 would ADD if every candidate were approved: **{len(ok)}**, touching {len(touched)} symbols "
         f"({len([s for s in touched if s in uni])} in the >= 10 crore universe): {', '.join(touched)}",
         f"- of those, {len(renamed)} were resolved through an official NSE rename (action keyed under the new symbol, price evidence under the old)",
         f"- listings evaluated: {len(res)} -> candidates {len(ok)}, rejected {int((res.classification == 'v3_rejected').sum())}, "
         f"unresolved {int((res.classification == 'unresolved').sum())}", "",
         f"Security map: {smap.intervals['security_id'].nunique()} stitched securities from the NSE list "
         f"({len(smap.rejected)} official rows failed the archive hand-off check, {len(smap.conflicts)} conflicts: {smap.conflicts[:3]}).", "",
         "Hand-off failures by reason: " + "; ".join(f"{k}: {v}" for k, v in
                                                    smap.rejected["reason"].str.replace(r"\(\d+ sessions.*", "", regex=True).value_counts().items()), "",
         "## Candidates (each needs an evidence pack and an owner approval)", "",
         "| listed symbol | traded as | listed ex | gap bar | factor | observed | err | shift | untraded | persists |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in ok.itertuples():
        L.append(f"| {r.listed_symbol} | {r.symbol_of_day} | {r.listed_ex_date} | {r.gap_date} | {r.listed_factor:.4f} | "
                 f"{r.observed_ratio:.4f} | {r.factor_error:.1%} | {int(r.shift_sessions):+d} | {int(r.untraded_sessions)} | {r.persistence_ratio:.3f} |")
    L += ["", "## Rejected by the hard bounds or persistence", "", "| listed symbol | traded as | listed ex | reason |", "|---|---|---|---|"]
    for r in res[res["classification"] == "v3_rejected"].itertuples():
        L.append(f"| {r.listed_symbol} | {r.symbol_of_day} | {r.listed_ex_date} | {r.reason} |")
    L += ["", "## Unresolved (no parseable factor / no bars)", "",
          ", ".join(f"{r.listed_symbol} {r.listed_ex_date}" for r in res[res["classification"] == "unresolved"].itertuples()) or "none"]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "v3_candidates_and_diff.md").write_text("\n".join(L), encoding="utf-8")
    res.to_csv(OUT / "v3_candidates.csv", index=False)
    smap.rejected.to_csv(OUT / "security_map_rejected.csv", index=False)
    import json
    prop = {"version": "owner_approved_v1", "note": "PROPOSED by reconcile_v3 - NOT LOADED. To approve, move the entries you accept (after "
            "reading their evidence packs) into corp_action_approvals.json and change decision to approve. Hard bounds are re-checked at apply time.",
            "decisions": [{"symbol": r.listed_symbol, "apply_to_symbol": r.symbol_of_day, "listed_ex_date": str(r.listed_ex_date),
                           "gap_date": str(r.gap_date), "factor": round(float(r.listed_factor), 10), "decision": "proposed",
                           "notes": r.reason} for r in ok.itertuples()]}
    prop_path = Path(__file__).resolve().parents[2] / "infra" / "data" / "corp_action_approvals_PROPOSED_v3.json"
    prop_path.write_text(json.dumps(prop, indent=1), encoding="utf-8")
    print("\n".join(L[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
