"""Validation of the NSE F&O archive load: coverage against the NSE cash calendar, per-session sizes, structure, expiry calendar, price sanity, futures-vs-underlying basis, and a
cross-check of the two file formats on their overlap. Gate for any F&O research view or analytic.

    python scripts/fno_validation_report.py [--parity]      # -> data/reports/fno/fno_validation.md
`--parity` fetches the UDiFF file for the overlap sessions (a few small downloads) and compares it with the loaded legacy rows.
Raw F&O prices are NOT adjusted: NSE re-bases a contract on a corporate action, so a series across an action is not continuous.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\fno")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--parity", action="store_true")
    args = ap.parse_args()
    from qsde.db.connection import read_sql

    OUT.mkdir(parents=True, exist_ok=True)
    ses = read_sql("SELECT * FROM fno_eod_sessions ORDER BY trade_date")
    if ses.empty:
        print("no F&O data loaded")
        return 1
    ses["trade_date"] = pd.to_datetime(ses["trade_date"])
    d0, d1 = ses["trade_date"].min(), ses["trade_date"].max()
    cash = pd.to_datetime(read_sql("SELECT DISTINCT date FROM eod_raw WHERE exchange = 'NSE' AND date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE)", params={"a": str(d0.date()), "b": str(d1.date())})["date"])
    missing = sorted(set(cash) - set(ses["trade_date"]))
    extra = sorted(set(ses["trade_date"]) - set(cash))
    findings: list = []
    if missing:
        findings.append(("fail" if len(missing) > 5 else "warn", f"{len(missing)} NSE cash sessions have no F&O file: " + ", ".join(str(d.date()) for d in missing[:6])))
    L = ["## 1. Coverage", "", f"- sessions loaded **{len(ses):,}**, {d0.date()} .. {d1.date()}; NSE cash sessions in the same span: {len(cash):,}; cash sessions without an F&O file: **{len(missing)}**; F&O sessions without a cash session: **{len(extra)}** "
         "(special sessions such as Muhurat days can legitimately differ)", "",
         "| year | sessions | median rows kept | median idle contracts not stored | min rows kept |", "|---|---|---|---|---|"]
    for y, g in ses.groupby(ses["trade_date"].dt.year):
        L.append(f"| {y} | {len(g)} | {int(g['n_rows_kept'].median()):,} | {int(g['n_dropped_idle'].median()):,} | {int(g['n_rows_kept'].min()):,} |")
    small = ses[ses["n_rows_kept"] < 0.4 * ses["n_rows_kept"].rolling(21, min_periods=5, center=True).median()]
    if len(small):
        findings.append(("warn", f"{len(small)} sessions hold under 40% of their neighbours' contracts (partial files?): " + ", ".join(str(d.date()) for d in small["trade_date"].head(5))))
    L += ["", f"- sessions far smaller than their neighbours: **{len(small)}**; rows of an instrument type the table does not hold (e.g. India VIX futures, interest-rate futures): {int(ses['n_other_types'].sum()):,}", ""]

    # 2 structure ------------------------------------------------------------------------------------------------------------------------------------------
    inst = read_sql("SELECT instrument, count(*) AS n, count(DISTINCT symbol) AS symbols, count(DISTINCT trade_date) AS sessions FROM fno_eod_raw GROUP BY 1 ORDER BY 1")
    idxfut = read_sql("SELECT symbol, count(DISTINCT trade_date) AS sessions FROM fno_eod_raw WHERE instrument = 'FUTIDX' AND symbol IN ('NIFTY', 'BANKNIFTY') AND open_interest > 0 GROUP BY 1")
    full = dict(zip(idxfut["symbol"], idxfut["sessions"]))
    for s in ("NIFTY", "BANKNIFTY"):
        if full.get(s, 0) < 0.98 * len(ses):
            findings.append(("warn", f"{s} index futures have open interest on only {full.get(s, 0)} of {len(ses)} sessions"))
    L += ["## 2. Structure", "", "| instrument | rows | symbols | sessions |", "|---|---|---|---|", *[f"| {r.instrument} | {int(r.n):,} | {int(r.symbols)} | {int(r.sessions)} |" for r in inst.itertuples()], "",
          f"- NIFTY / BANKNIFTY index futures with open interest on {full.get('NIFTY', 0)} / {full.get('BANKNIFTY', 0)} of {len(ses)} sessions", ""]

    # 3 integrity ------------------------------------------------------------------------------------------------------------------------------------------
    bad = read_sql("""SELECT count(*) FILTER (WHERE open_interest < 0) AS neg_oi, count(*) FILTER (WHERE open_interest > 0 AND coalesce(settle, 0) <= 0 AND instrument LIKE 'FUT%') AS fut_no_settle,
                             count(*) FILTER (WHERE high < low) AS hi_lt_lo, count(*) FILTER (WHERE instrument LIKE 'FUT%' AND strike <> 0) AS fut_strike, count(*) FILTER (WHERE expiry_date < trade_date) AS expired,
                             count(*) FILTER (WHERE option_type <> 'FUT' AND instrument LIKE 'FUT%') AS fut_opt FROM fno_eod_raw""").iloc[0]
    for k, v in bad.items():
        if int(v) > 0:
            findings.append(("fail" if k in ("neg_oi", "expired") else "warn", f"integrity: {k} = {int(v):,}"))
    L += ["## 3. Integrity", "", *[f"- {k}: **{int(v):,}**" for k, v in bad.items()], ""]

    # 4 expiry calendar ----------------------------------------------------------------------------------------------------------------------------------
    ex = read_sql("SELECT DISTINCT expiry_date FROM fno_eod_raw WHERE symbol = 'NIFTY' AND instrument = 'FUTIDX'")
    ex["expiry_date"] = pd.to_datetime(ex["expiry_date"])
    wk = ex.groupby(ex["expiry_date"].dt.year)["expiry_date"].apply(lambda s: s.dt.day_name().value_counts().to_dict())
    weekend = ex[ex["expiry_date"].dt.dayofweek >= 5]
    if len(weekend):
        findings.append(("warn", f"{len(weekend)} NIFTY futures expiries fall on a weekend"))
    L += ["## 4. Expiry calendar (NIFTY index futures)", "", f"- distinct monthly expiries seen: {len(ex)}; on a weekend: **{len(weekend)}**",
          "- weekday of expiry by year (NSE moved NIFTY monthly expiry from Thursday to Tuesday in 2025): " + "; ".join(f"{y}: " + ", ".join(f"{k} {v}" for k, v in d.items()) for y, d in wk.groupby(level=0).apply(lambda s: s.iloc[0]).items()), ""]

    # 5 futures vs underlying (UDiFF only carries the underlying price) -------------------------------------------------------------------------------
    basis = read_sql("""SELECT f.trade_date, f.symbol, f.settle AS fut, f.underlying_price AS und FROM fno_eod_raw f WHERE f.instrument = 'FUTSTK' AND f.underlying_price > 0 AND f.settle > 0
                        AND f.open_interest > 0 AND f.expiry_date = (SELECT min(g.expiry_date) FROM fno_eod_raw g WHERE g.symbol = f.symbol AND g.trade_date = f.trade_date AND g.instrument = 'FUTSTK')""")
    if len(basis):
        basis["b"] = basis["fut"] / basis["und"] - 1
        wild = basis[basis["b"].abs() > 0.05]
        L += ["## 5. Stock futures vs the underlying (near month, sessions that carry the underlying price)", "", f"- {len(basis):,} observations; median basis {basis['b'].median():+.2%}; 1st-99th percentile {basis['b'].quantile(.01):+.2%} .. {basis['b'].quantile(.99):+.2%}; "
              f"basis beyond +-5%: **{len(wild):,}** (corporate actions, rolls and illiquid near-month contracts)", ""]
        if len(wild) > 0.02 * len(basis):
            findings.append(("warn", f"{len(wild):,} of {len(basis):,} near-month stock-futures observations have a basis beyond 5%"))

    # 6 format parity -------------------------------------------------------------------------------------------------------------------------------------
    if args.parity:
        from qsde.stream.adapters import nse_fo as F
        from qsde.stream.adapters.nse import HttpFetcher

        fx, rows = HttpFetcher(min_interval_s=1.0), []
        for d in [x for x in ses["trade_date"] if pd.Timestamp("2024-06-28") <= x <= pd.Timestamp("2024-07-05")]:
            p = fx.get(F.URL_UDIFF.format(yyyymmdd=f"{d:%Y%m%d}"), F.HEADERS)
            u = F.parse_fo(p, "udiff", "parity")
            lg = read_sql("SELECT instrument, symbol, expiry_date, strike, option_type, settle, open_interest, contracts FROM fno_eod_raw WHERE trade_date = CAST(:d AS DATE)", params={"d": str(d.date())})
            lg["expiry_date"] = pd.to_datetime(lg["expiry_date"]); lg["strike"] = lg["strike"].astype(float)
            u["strike"] = u["strike"].astype(float)
            k = ["instrument", "symbol", "expiry_date", "strike", "option_type"]
            m = lg.merge(u[k + ["settle", "open_interest", "contracts"]], on=k, how="outer", suffixes=("_legacy", "_udiff"), indicator=True)
            both = m[m["_merge"] == "both"]
            rows.append((d.date(), len(m), int((m["_merge"] == "both").sum()), int((m["_merge"] == "left_only").sum()), int((m["_merge"] == "right_only").sum()),
                         float((np.isclose(both["settle_legacy"], both["settle_udiff"], atol=0.011, equal_nan=True)).mean()), float((both["open_interest_legacy"] == both["open_interest_udiff"]).mean())))
        if rows:
            L += ["## 6. Cross-check of the two file formats on their overlap (legacy loaded vs UDiFF fetched)", "", "| session | contracts | in both | legacy only | UDiFF only | settle agrees | OI agrees |", "|---|---|---|---|---|---|---|",
                  *[f"| {a} | {b} | {c} | {d_} | {e} | {f:.1%} | {g:.1%} |" for a, b, c, d_, e, f, g in rows], ""]
            if min(r[5] for r in rows) < 0.99 or min(r[6] for r in rows) < 0.99:
                findings.append(("warn", "the two formats disagree on settlement or open interest for more than 1% of shared contracts"))
    verdict = "FAIL" if any(s == "fail" for s, _ in findings) else ("PASS WITH FINDINGS" if findings else "PASS")
    head = ["# NSE F&O archive validation", "", "> Raw prices as NSE published them (not adjusted for corporate actions). Contracts with no open interest and no volume are not stored (counted per session). No F&O analytic or view "
            "is built on this table until the findings are reviewed.", "", f"**Gate verdict: {verdict}**", "", *([f"- [{s}] {t}" for s, t in findings] or ["- no findings"]), ""]
    (OUT / "fno_validation.md").write_text("\n".join(head + L), encoding="utf-8")
    print("\n".join(head + L)[:5000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
