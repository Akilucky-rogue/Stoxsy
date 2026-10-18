"""Build the reviewed adjustment materializations from the database. Separate from `ohlcv`; nothing here modifies it.

    python scripts/build_adjustment.py --kind split_bonus
    python scripts/build_adjustment.py --kind total_return      # needs a split_bonus version (the latest validated one by default)
    python scripts/build_adjustment.py --kind all

Each build: fingerprint inputs -> adjust -> validate (event-level + series-level) -> write immutable Parquet + MANIFEST -> rebuild into
a scratch directory and require the SAME output hash -> register in adjustment_runs / adjustment_events_applied. A series only gets
validation_status 'validated' if validation AND the rebuild-identical check both pass. Reports: data/reports/adjustment/.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

REPORTS = Path(r"D:\Stoxsy\data\reports\adjustment")


def load_raw():
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE'")
    raw["date"] = pd.to_datetime(raw["date"])
    return nse_eod.pick_primary_series(raw)[["symbol", "date", "open", "high", "low", "close", "volume"]]


def load_events() -> pd.DataFrame:
    """confirmed (ca_reconcile_v1) + approved (official_confirmed / owner_approved) split-bonus events, listed factor only."""
    from qsde.db.connection import read_sql

    a = read_sql("""SELECT r.symbol_of_day AS symbol, r.security_id, r.gap_date, r.ex_date AS nse_ex_date, r.listed_factor AS factor,
                           'confirmed' AS tier, 'ca_reconcile_v1' AS source, c.id AS source_record_id, c.isin, NULL::text AS evidence_ref
                    FROM corp_action_reconciliation r JOIN nse_corporate_actions c ON c.id = r.action_id
                    WHERE r.reconciler_version = 'ca_reconcile_v1' AND r.category IN ('SPLIT_BONUS', 'CONSOLIDATION')
                      AND r.classification = 'confirmed' AND r.gap_date IS NOT NULL AND r.symbol_of_day IS NOT NULL""")
    b = read_sql("""SELECT symbol, ex_date AS gap_date, price_factor AS factor,
                           CASE WHEN approval_tier = 'official_confirmed' THEN 'official_confirmed' ELSE 'owner_approved' END AS tier,
                           'owner_approval' AS source, isin, evidence_ref
                    FROM corp_action_events WHERE applied AND method = 'owner_approved_v1'""")
    return pd.concat([a, b], ignore_index=True)


def official_events() -> pd.DataFrame:
    from qsde.db.connection import read_sql

    e = read_sql("SELECT symbol, ex_date AS gap_date, price_factor AS factor FROM corp_action_events WHERE applied")
    e["gap_date"] = pd.to_datetime(e["gap_date"])
    return e


def register(m, kind, inputs, validation, status, events=None):
    from qsde.db.connection import execute_sql

    execute_sql("DELETE FROM adjustment_runs WHERE version = %(v)s", {"v": m.version})
    execute_sql("""INSERT INTO adjustment_runs (version, kind, inputs, output_hash, rows, n_events, validation_status, validation, path)
                   VALUES (%(v)s,%(k)s,%(i)s,%(h)s,%(r)s,%(n)s,%(s)s,%(val)s,%(p)s)""",
                {"v": m.version, "k": kind, "i": json.dumps(inputs, default=str), "h": m.output_hash, "r": m.rows,
                 "n": None if events is None else int(len(events)), "s": status, "val": json.dumps(validation, default=str), "p": str(m.path)})
    if events is not None:
        for r in events.itertuples():
            nn = lambda v: None if v is None or (isinstance(v, float) and v != v) else v          # noqa: E731
            execute_sql("""INSERT INTO adjustment_events_applied (version, symbol, security_id, gap_date, nse_ex_date, factor, tier, source,
                              source_record_id, isin, evidence_ref) VALUES (%(v)s,%(s)s,%(sid)s,%(g)s,%(x)s,%(f)s,%(t)s,%(so)s,%(rid)s,%(i)s,%(e)s)
                           ON CONFLICT DO NOTHING""",
                        {"v": m.version, "s": r.symbol, "sid": nn(r.security_id), "g": r.gap_date.date(), "x": nn(r.nse_ex_date),
                         "f": float(r.factor), "t": r.tier, "so": r.source, "rid": None if nn(r.source_record_id) is None else int(r.source_record_id),
                         "i": nn(r.isin), "e": nn(r.evidence_ref)})


def build_split_bonus():
    from qsde.adjust import pipeline as ap
    from qsde.ingestion.corp_actions_approvals import approvals_fingerprint

    raw = load_raw()
    events = ap.normalise_events(load_events())
    inputs = {"kind": "split_bonus", "events_hash": ap.events_hash(events), "n_events": int(len(events)),
              "raw": ap.raw_fingerprint(raw), "reconciler": "ca_reconcile_v1", "approvals_sha256": approvals_fingerprint()["sha256"],
              "factor_rule": "NSE-listed multiplier; bars strictly before the gap bar are scaled; volume divided"}
    adjusted = ap.split_bonus_adjust(raw, events)
    from qsde.db.connection import read_sql
    off = read_sql("SELECT symbol, date, close FROM ohlcv")
    val = ap.validate_split_adjustment(raw, adjusted, events, official=off)
    m = ap.write_materialization(adjusted, ap.SPLIT_SCHEMA, "split_bonus", ap.SPLIT_VERSION_PREFIX, inputs,
                                 extra={"validation_summary": val["summary"]})
    tmp = Path(tempfile.mkdtemp(prefix="adjcheck-"))
    try:                                                   # determinism: rebuild from scratch elsewhere, require the same hash
        m2 = ap.write_materialization(ap.split_bonus_adjust(raw, events), ap.SPLIT_SCHEMA, "split_bonus", ap.SPLIT_VERSION_PREFIX, inputs,
                                      root=tmp)
        identical = m2.output_hash == m.output_hash
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    status = "validated" if (val["summary"]["status"] == "validated" and identical) else "failed"
    val["summary"]["rebuild_identical"] = identical

    # event-set comparison with the current official series
    oe = official_events()
    new = events.assign(key=events["symbol"] + "|" + events["gap_date"].dt.strftime("%Y-%m-%d"))
    old = oe.assign(key=oe["symbol"] + "|" + oe["gap_date"].dt.strftime("%Y-%m-%d"))
    only_new, only_old = new[~new["key"].isin(old["key"])], old[~old["key"].isin(new["key"])]
    both = new.merge(old, on="key", suffixes=("_n", "_o"))
    mism = both[(both["factor_n"] / both["factor_o"] - 1).abs() > 1e-6]
    register(m, "split_bonus", inputs, val["summary"], status, events)

    REPORTS.mkdir(parents=True, exist_ok=True)
    s = val["summary"]
    L = [f"# Split/bonus adjustment `{m.version}` - {status.upper()}", "",
         f"- output hash `{m.output_hash[:16]}`; rebuilt from scratch with identical hash: **{identical}**; bars {s['bars']:,}",
         f"- events used: {s['events']} (confirmed {int((events.tier == 'confirmed').sum())}, official_confirmed "
         f"{int((events.tier == 'official_confirmed').sum())}, owner_approved {int((events.tier == 'owner_approved').sum())})",
         f"- event continuity: ok {s['events_ok']}, flagged (adjusted overnight >= 20%) {s['events_flagged']}, hard failures (>= 40%) "
         f"{s['events_fail_hard']}, no gap bar {s['events_no_gap_bar']}; ok share {s['event_ok_share']:.1%}",
         f"- one-day moves >= 18% (symbol-days): raw {s['cliffs_raw']:,} -> current official ohlcv {s.get('cliffs_official_ohlcv', 'n/a'):,} "
         f"-> this series {s['cliffs_adjusted']:,}", "",
         "## Against the current official `ohlcv` (price-confirmed v2 + approvals)", "",
         f"- events only in this series: {len(only_new)}; only in the official series: {len(only_old)}; same key, different factor: {len(mism)}",
         "- only in the official series (not `confirmed` here, so NOT adjusted in this series): " +
         ", ".join(f"{r.symbol} {r.gap_date.date()} x{r.factor:.3f}" for r in only_old.head(25).itertuples()) + (" ..." if len(only_old) > 25 else ""),
         "- only in this series: " + ", ".join(f"{r.symbol} {r.gap_date.date()} x{r.factor:.3f}" for r in only_new.head(25).itertuples()), ""]
    bad = val["events"][val["events"]["status"] != "ok"]
    if len(bad):
        L += ["## Events needing a look", "", "| symbol | gap bar | factor | raw overnight | adjusted overnight | status |", "|---|---|---|---|---|---|"]
        for r in bad.head(40).itertuples():
            L.append(f"| {r.symbol} | {r.gap_date} | {getattr(r, 'factor', '')} | {getattr(r, 'raw_overnight', '')} | {getattr(r, 'adjusted_overnight', '')} | {r.status} |")
    (REPORTS / f"split_bonus_{m.version}.md").write_text("\n".join(L), encoding="utf-8")
    val["events"].to_csv(REPORTS / f"split_bonus_{m.version}_events.csv", index=False)
    only_old.drop(columns="key").to_csv(REPORTS / f"split_bonus_{m.version}_only_in_official.csv", index=False)
    print("\n".join(L[:14]))
    return m, status


def build_total_return(split_version: str = None):
    from qsde.adjust import pipeline as ap
    from qsde.db.connection import read_sql

    runs = read_sql("SELECT version, validation_status, path FROM adjustment_runs WHERE kind = 'split_bonus' ORDER BY built_at DESC")
    run = runs[runs["version"] == split_version].iloc[0] if split_version else runs[runs["validation_status"] == "validated"].iloc[0]
    adjusted = ap.read_materialization(Path(run["path"]))
    div = read_sql("""SELECT r.symbol_of_day AS symbol, r.ex_date, r.amount, c.isin
                      FROM corp_action_reconciliation r JOIN nse_corporate_actions c ON c.id = r.action_id
                      WHERE r.reconciler_version = 'ca_reconcile_v1' AND r.category = 'DIVIDEND' AND r.classification = 'confirmed'
                        AND r.amount IS NOT NULL AND r.symbol_of_day IS NOT NULL""")
    tr, info = ap.total_return_series(adjusted, div)
    div_hash = __import__("hashlib").sha256(json.dumps(sorted((r.symbol, str(r.ex_date), round(float(r.amount), 6)) for r in div.itertuples())).encode()).hexdigest()
    inputs = {"kind": "total_return", "split_version": run["version"], "split_validation": run["validation_status"],
              "dividends_hash": div_hash, "dividends_confirmed": int(len(div)), "reconciler": "ca_reconcile_v1",
              "basis": {"dividend_basis": "gross cash per share, as announced", "tax_treatment": "gross of tax (no TDS modelled)",
                        "price_return": "split-adjusted close-to-close", "total_return": "price_return + dividend_return, exactly"}}
    m = ap.write_materialization(tr, ap.TR_SCHEMA, "total_return", ap.TR_VERSION_PREFIX, inputs, extra={"dividend_info": info})
    # validation
    ok_rows = tr["total_return_1d"].notna()
    drag = (tr.loc[ok_rows, "total_return_1d"] - tr.loc[ok_rows, "price_return_1d"])
    uni_div = tr[tr["dividend_return_1d"].fillna(0) > 0]
    per_year = (tr.assign(y=tr["date"].dt.year).groupby(["symbol", "y"])["dividend_return_1d"].sum().groupby("y").mean())
    summary = {"rows": int(len(tr)), "dividends_used": info["dividends_used"], "dividends_skipped": info["dividends_skipped"],
               "dividends_confirmed_in": info["dividends_in"], "min_dividend_return_nonneg": bool((drag.dropna() >= -1e-12).all()),
               "max_single_day_dividend_return": float(uni_div["dividend_return_1d"].max()) if len(uni_div) else 0.0,
               "mean_annual_dividend_yield_by_year": {int(k): round(float(v), 4) for k, v in per_year.items()},
               "split_series_validated": run["validation_status"] == "validated"}
    tmp = Path(tempfile.mkdtemp(prefix="trcheck-"))
    try:                                                   # determinism: rebuild from the same inputs elsewhere, require the same hash
        tr2, _ = ap.total_return_series(ap.read_materialization(Path(run["path"])), div)
        m2 = ap.write_materialization(tr2, ap.TR_SCHEMA, "total_return", ap.TR_VERSION_PREFIX, inputs, root=tmp,
                                      extra={"dividend_info": info})
        summary["rebuild_identical"] = m2.output_hash == m.output_hash
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    summary["status"] = "validated" if (summary["min_dividend_return_nonneg"] and summary["split_series_validated"]
                                       and summary["rebuild_identical"]
                                       and summary["max_single_day_dividend_return"] <= 0.25) else "failed"
    register(m, "total_return", inputs, summary, summary["status"])
    REPORTS.mkdir(parents=True, exist_ok=True)
    L = [f"# Total-return series `{m.version}` - {summary['status'].upper()}", "",
         f"- built on split/bonus series `{run['version']}` ({run['validation_status']}); output hash `{m.output_hash[:16]}`; rows {len(tr):,}",
         f"- dividends: {info['dividends_in']:,} confirmed (symbol, ex-date) groups -> {info['dividends_used']:,} applied; skipped {info['dividends_skipped']}",
         "- labelled fields: `price_return_1d` (split-adjusted), `dividend_return_1d` (gross cash / previous adjusted close, no tax), "
         "`total_return_1d` = their sum; indices are separate columns. Dividends are NOT inside `ohlcv` or the split-adjusted prices.",
         f"- mean annual dividend return across stocks, by year: {summary['mean_annual_dividend_yield_by_year']}"]
    (REPORTS / f"total_return_{m.version}.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    return m, summary["status"]


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--kind", choices=["split_bonus", "total_return", "all"], default="all")
    ap_.add_argument("--split-version", default=None)
    args = ap_.parse_args()
    if args.kind in ("split_bonus", "all"):
        build_split_bonus()
    if args.kind in ("total_return", "all"):
        build_total_return(args.split_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
