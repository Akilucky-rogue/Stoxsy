"""AMFI mutual-fund layer validation: scheme master, NAV coverage and sanity, ISIN cross-checks against NSE's official ETF list and BSE's fund/ETF scrips.

    python scripts/amfi_validation_report.py        # -> data/reports/funds/amfi_validation.md (+ csv)
This layer is separate from the equity layer; nothing here feeds the universe, the defensive signal or decision cards.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\funds")


def main() -> int:
    from qsde.db.connection import read_sql
    from qsde.funds.returns import scheme_returns

    OUT.mkdir(parents=True, exist_ok=True)
    sm = read_sql("SELECT * FROM mf_scheme_master")
    if sm.empty:
        print("no AMFI data loaded")
        return 1
    cov = read_sql("""SELECT extract(year from date)::int AS year, count(*) AS rows, count(DISTINCT scheme_code) AS schemes, count(DISTINCT date) AS days
                      FROM mf_nav GROUP BY 1 ORDER BY 1""")
    loads = read_sql("SELECT source, count(*) AS files, sum(n_rows) AS rows, min(window_from) AS d0, max(window_to) AS d1 FROM mf_nav_loads GROUP BY 1")
    last_nav = pd.to_datetime(sm["last_nav_date"]).max()
    active = sm[pd.to_datetime(sm["last_nav_date"]) >= last_nav - pd.Timedelta(days=7)]

    # NAV sanity: one-day moves > 50% (a bad print, a unit consolidation, or a closed fund liquidating) ------------------
    big = read_sql("""WITH x AS (SELECT scheme_code, date, nav, lag(nav) OVER (PARTITION BY scheme_code ORDER BY date) AS prev FROM mf_nav)
                      SELECT scheme_code, date, prev, nav, nav / prev - 1 AS change FROM x WHERE prev > 0 AND abs(nav / prev - 1) > 0.5""")
    big = big.merge(sm[["scheme_code", "name", "asset_class"]], on="scheme_code", how="left")
    one_nav = read_sql("SELECT scheme_code FROM mf_nav GROUP BY 1 HAVING count(*) = 1")

    # ISIN checks ---------------------------------------------------------------------------------------------------------
    dup_isin = sm[sm["isin_growth_or_payout"].notna()].groupby("isin_growth_or_payout")["scheme_code"].nunique()
    dup_isin = dup_isin[dup_isin > 1]
    etf_off = pd.DataFrame(json.loads(Path(r"D:\Stoxsy\stoxsy\infra\data\non_equity_official_etf_v1.json").read_text())["instruments"])
    amfi_etf = sm[sm["is_etf"]]
    amfi_isins = set(sm["isin_growth_or_payout"].dropna()) | set(sm["isin_reinvest"].dropna())
    nse_etf_in_amfi = etf_off["isin"].isin(amfi_isins).mean()
    bse_funds = read_sql("SELECT scrip_code, isin FROM bse_listing_master WHERE instrument_type = 'fund_etf' AND isin IS NOT NULL")
    bse_in_amfi = bse_funds["isin"].isin(amfi_isins).mean() if len(bse_funds) else float("nan")

    # category table + illustrative returns (methodology demo, not a ranking) -------------------------------------------------------
    cat = sm.groupby(["asset_class", "broad_category"], dropna=False).size().reset_index(name="schemes").sort_values("schemes", ascending=False)
    sample = sm[(sm["asset_class"] == "equity") & (sm["is_direct"] == True) & (sm["is_growth"] == True)].head(1)      # noqa: E712
    demo = ""
    if len(sample):
        code = int(sample["scheme_code"].iloc[0])
        nv = read_sql("SELECT date, nav FROM mf_nav WHERE scheme_code = :c ORDER BY date", params={"c": code})
        r = scheme_returns(nv.set_index(pd.to_datetime(nv["date"]))["nav"])
        demo = f"{sample['name'].iloc[0]} (code {code}): " + ", ".join(f"{h} {v['cumulative']:+.1%}" + (f" ({v['annualised']:+.1%}/yr)" if v["annualised"] is not None else "") for h, v in r.items())

    L = ["# AMFI mutual-fund layer validation", "",
         "> A separate asset-class layer: nothing here enters the equity universe, the defensive stock signal or the decision cards. NAV is raw as published; "
         "past NAV performance is not advice or a forecast.", "",
         f"- schemes in the master: **{len(sm):,}** across {sm['amc'].nunique()} fund houses; active (NAV within 7 days of {last_nav.date()}): {len(active):,}",
         f"- loads: " + "; ".join(f"{r.source}: {r.files} file(s), {int(r.rows):,} rows" + (f" ({r.d0} .. {r.d1})" if pd.notna(r.d0) else "") for r in loads.itertuples()),
         f"- ISIN present on {sm['isin_growth_or_payout'].notna().mean():.1%} of schemes; ISINs shared by more than one scheme code: {len(dup_isin)}",
         f"- schemes with a single NAV only: {len(one_nav):,}", "",
         "## NAV coverage by year", "", "| year | NAV rows | schemes | distinct dates |", "|---|---|---|---|",
         *[f"| {r.year} | {int(r.rows):,} | {int(r.schemes):,} | {int(r.days)} |" for r in cov.itertuples()], "",
         "## Classification (rule-based, evidence stored per scheme)", "",
         "| asset class | schemes | ETF | index | liquid | FoF | direct | growth |", "|---|---|---|---|---|---|---|---|"]
    for a, g in sm.groupby("asset_class"):
        L.append(f"| {a} | {len(g):,} | {int(g['is_etf'].sum()):,} | {int(g['is_index'].sum()):,} | {int(g['is_liquid'].sum()):,} | {int(g['is_fof'].sum()):,} | "
                 f"{int((g['is_direct'] == True).sum()):,} | {int((g['is_growth'] == True).sum()):,} |")        # noqa: E712
    L += ["", "Largest AMFI categories (verbatim headings):", "", "| asset class | category | schemes |", "|---|---|---|",
          *[f"| {r.asset_class} | {r.broad_category} | {r.schemes:,} |" for r in cat.head(15).itertuples()], "",
          "## Cross-checks", "",
          f"- NSE official ETF list: {nse_etf_in_amfi:.1%} of its ISINs appear as an AMFI scheme ISIN ({int(etf_off['isin'].isin(amfi_isins).sum())} of {len(etf_off)}); AMFI schemes flagged ETF: {len(amfi_etf):,}",
          f"- BSE fund/ETF scrips with an ISIN: {len(bse_funds):,}; share found in AMFI: {bse_in_amfi:.1%}", "",
          "## NAV sanity", "", f"- one-day NAV moves above 50%: **{len(big):,}** (bad print, unit consolidation or a fund winding down; listed in `nav_outliers.csv`)",
          *([f"- largest: {r.name} ({r.scheme_code}) on {r.date}: {r.change:+.0%}" for r in big.reindex(big['change'].abs().sort_values(ascending=False).index).head(5).itertuples()] if len(big) else []), "",
          "## Returns methodology", "",
          "Cumulative = NAV(asof) / NAV(on or before asof - horizon) - 1 for 1D, 1W, 1M, 3M, 6M, 1Y, 3Y, 5Y. An annualised figure (CAGR) is shown only from 1Y and is never mixed into the cumulative column. "
          "A horizon is omitted if the scheme lacks history that far back or the start NAV is more than 7 days stale. For GROWTH options the figure is a total return after expenses; "
          "for IDCW / payout options it is a price return only (payouts are not in the file).", "", f"Illustration of the format: {demo}" if demo else "",
          "", "## Not available from AMFI's NAV files", "",
          "Expense ratios (TER disclosures), portfolio holdings (monthly disclosures, needed for overlap), AUM and benchmarks come from other sources and are not loaded: "
          "overlap and expense analysis are not possible yet. Each needs its own approved source."]
    big.sort_values("change", key=abs, ascending=False).head(2000).to_csv(OUT / "nav_outliers.csv", index=False)
    (OUT / "amfi_validation.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:22]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
