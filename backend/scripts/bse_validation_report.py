"""BSE validation milestone: coverage, ISIN match rate, NSE/BSE price divergence, duplicate listings, BSE-only symbols - and the cross-listing map.

    python scripts/bse_validation_report.py            # writes data/reports/bse/bse_validation.md + .csv and rebuilds bse_listing_master / listing_xref

Linking policy (strongest first):
  isin          the BSE row's ISIN equals an ISIN known for the NSE symbol (NSE ISINs come from the NSE bhavcopy of 2016-2020 and from the NSE corporate-action
                records); this is the authority.
  ticker_price  no shared ISIN is known, but BSE's ticker equals the NSE symbol AND the two closes agree within 2% on at least 90% of >= 30 overlapping
                sessions. Labelled as the weaker tier; a later NSE equity master would upgrade it.
Nothing is overwritten in the exchange tables; `listing_xref` and `bse_listing_master` are rebuilt from them deterministically.
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

OUT = Path(r"D:\Stoxsy\data\reports\bse")
BUILT_BY = "bse_validation_report.py v1"
DEBT_GROUPS = {"F", "G", "GS", "D", "DB", "DR", "DT", "E", "TB", "W"}


def classify(row) -> tuple[str, str]:
    isin = row.isin if isinstance(row.isin, str) else ""
    grp = row.grp.strip() if isinstance(row.grp, str) else ""
    if isin.startswith("INF"):
        return "fund_etf", "ISIN prefix INF (fund / ETF unit)"
    if grp in DEBT_GROUPS:
        return "debt", f"BSE group {grp} (debt / fixed income / govt securities)"
    if isin and isin[8:10] not in ("01", "02", "03", "04", "05", "06", "10", "20", "21", "22", "23"):
        return "debt", f"ISIN security code {isin[8:10]} is not an equity-share code"
    return "equity", "equity group / share ISIN" if isin else "equity group; ISIN not published for this date"


def main() -> int:
    from qsde.db.connection import execute_sql, get_sync_conn, read_sql

    OUT.mkdir(parents=True, exist_ok=True)
    cov = read_sql("""SELECT extract(year from date)::int AS year, count(DISTINCT date) AS sessions, count(*) AS rows, count(isin) AS rows_isin,
                             count(DISTINCT scrip_code) AS scrips, sum(turnover_value)/1e9 AS turnover_bn
                      FROM bse_eod_raw GROUP BY 1 ORDER BY 1""")
    if cov.empty:
        print("no BSE data loaded yet")
        return 1
    bse_dates = set(pd.to_datetime(read_sql("SELECT DISTINCT date FROM bse_eod_raw")["date"]))
    nse_dates = set(pd.to_datetime(read_sql("SELECT DISTINCT date FROM eod_raw WHERE exchange = 'NSE'")["date"]))
    lo, hi = min(bse_dates), max(bse_dates)
    nse_only = sorted(d for d in nse_dates if lo <= d <= hi and d not in bse_dates)
    bse_only_dates = sorted(d for d in bse_dates if d not in nse_dates)

    # listing master ---------------------------------------------------
    lm = read_sql("""SELECT DISTINCT ON (scrip_code) scrip_code, ticker, name, grp, isin, date AS last_date FROM bse_eod_raw
                     ORDER BY scrip_code, date DESC""")
    fl = read_sql("SELECT scrip_code, min(date) AS first_date, count(*) AS n_days FROM bse_eod_raw GROUP BY 1")
    lm = lm.merge(fl, on="scrip_code")
    # an ISIN seen on any date (older files may carry it when the latest row does not)
    any_isin = read_sql("SELECT scrip_code, max(isin) AS any_isin FROM bse_eod_raw WHERE isin IS NOT NULL GROUP BY 1")
    lm = lm.merge(any_isin, on="scrip_code", how="left")
    lm["isin"] = lm["isin"].fillna(lm["any_isin"])
    cls = [classify(r) for r in lm.itertuples()]
    lm["instrument_type"], lm["type_evidence"] = [c[0] for c in cls], [c[1] for c in cls]

    # NSE ISIN knowledge ------------------------------------------------
    nse_isin = read_sql("""SELECT symbol, isin FROM (SELECT symbol, isin FROM eod_raw WHERE isin IS NOT NULL AND exchange = 'NSE'
                           UNION SELECT symbol, isin FROM nse_corporate_actions WHERE isin IS NOT NULL) t GROUP BY symbol, isin""")
    nse_syms = set(read_sql("SELECT DISTINCT symbol FROM eod_raw WHERE exchange = 'NSE'")["symbol"])
    by_isin = nse_isin.groupby("isin")["symbol"].apply(list).to_dict()

    links, evidence = [], {}
    for r in lm.itertuples():
        for sym in by_isin.get(r.isin, []):
            if sym in nse_syms:
                links.append((r.isin, sym, int(r.scrip_code), "isin"))
    linked_bse = {l[2] for l in links}

    # weaker tier: same ticker + agreeing prices -------------------------
    cand = lm[(~lm["scrip_code"].isin(linked_bse)) & lm["ticker"].notna() & lm["ticker"].isin(nse_syms) & (lm["instrument_type"] == "equity")]
    weak = []
    if len(cand):
        bpx = read_sql("SELECT scrip_code, date, close FROM bse_eod_raw WHERE scrip_code = ANY(:s)", params={"s": cand["scrip_code"].astype(int).tolist()})
        npx = read_sql("SELECT symbol, date, close FROM eod_raw WHERE exchange = 'NSE' AND series = 'EQ' AND symbol = ANY(:s)", params={"s": cand["ticker"].tolist()})
        t2s = dict(zip(cand["scrip_code"], cand["ticker"]))
        bpx["symbol"] = bpx["scrip_code"].map(t2s)
        m = bpx.merge(npx, on=["symbol", "date"], suffixes=("_b", "_n"))
        m["gap"] = (m["close_b"] / m["close_n"] - 1).abs()
        for (code, sym), g in m.groupby(["scrip_code", "symbol"]):
            if len(g) >= 30 and (g["gap"] <= 0.02).mean() >= 0.90:
                weak.append((None, sym, int(code), "ticker_price"))
                evidence[(sym, int(code))] = {"overlap_days": int(len(g)), "share_within_2pct": float((g["gap"] <= 0.02).mean())}
    links += weak
    xref = pd.DataFrame(links, columns=["isin", "nse_symbol", "bse_scrip_code", "link_basis"]).drop_duplicates(["nse_symbol", "bse_scrip_code"])

    # price divergence on ISIN-linked pairs (recent window) ---------------
    div = pd.DataFrame()
    isin_pairs = xref[xref["link_basis"] == "isin"]
    if len(isin_pairs):
        d0 = (hi - pd.Timedelta(days=120)).date()
        b = read_sql("SELECT scrip_code, date, close FROM bse_eod_raw WHERE date >= CAST(:d AS DATE)", params={"d": d0})
        n = read_sql("SELECT symbol, date, close FROM eod_raw WHERE exchange = 'NSE' AND series = 'EQ' AND date >= CAST(:d AS DATE)", params={"d": d0})
        mm = isin_pairs.merge(b, left_on="bse_scrip_code", right_on="scrip_code").merge(n, left_on=["nse_symbol", "date"], right_on=["symbol", "date"], suffixes=("_b", "_n"))
        mm["gap"] = (mm["close_b"] / mm["close_n"] - 1).abs()
        div = mm.groupby(["nse_symbol", "bse_scrip_code"]).agg(days=("gap", "size"), median_gap=("gap", "median"), max_gap=("gap", "max")).reset_index()

    # duplicates and conflicts ---------------------------------------------
    multi_scrip = lm[lm["isin"].notna()].groupby("isin")["scrip_code"].nunique()
    multi_scrip = multi_scrip[multi_scrip > 1]
    multi_isin = read_sql("SELECT scrip_code, count(DISTINCT isin) AS n FROM bse_eod_raw WHERE isin IS NOT NULL GROUP BY 1 HAVING count(DISTINCT isin) > 1")
    multi_nse = xref.groupby("bse_scrip_code")["nse_symbol"].nunique()
    multi_nse = multi_nse[multi_nse > 1]

    # BSE-only ---------------------------------------------------------------
    eq = lm[lm["instrument_type"] == "equity"]
    last_day = lm["last_date"].max()
    active = eq[eq["last_date"] == last_day]
    linked_active = active[active["scrip_code"].isin(set(xref["bse_scrip_code"]))]
    bse_only_active = active[~active["scrip_code"].isin(set(xref["bse_scrip_code"]))]
    tv = read_sql("SELECT scrip_code, sum(turnover_value) AS tv FROM bse_eod_raw WHERE date >= CAST(:d AS DATE) GROUP BY 1", params={"d": (hi - pd.Timedelta(days=60)).date()})
    tv_map = dict(zip(tv["scrip_code"], tv["tv"]))
    share_only = sum(tv_map.get(int(c), 0) for c in bse_only_active["scrip_code"]) / max(sum(tv_map.get(int(c), 0) for c in active["scrip_code"]), 1)

    # persist -------------------------------------------------------------------
    lm_out = lm.drop(columns=["any_isin"])
    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM bse_listing_master")
        cur.execute("DELETE FROM listing_xref WHERE built_by = %s", (BUILT_BY,))
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, "INSERT INTO bse_listing_master (scrip_code, ticker, name, grp, isin, first_date, last_date, n_days, instrument_type, type_evidence) VALUES %s",
                                       [(int(r.scrip_code), r.ticker, r.name, r.grp, r.isin, r.first_date, r.last_date, int(r.n_days), r.instrument_type, r.type_evidence) for r in lm_out.itertuples()])
        psycopg2.extras.execute_values(cur, "INSERT INTO listing_xref (isin, nse_symbol, bse_scrip_code, link_basis, evidence, built_by) VALUES %s ON CONFLICT (nse_symbol, bse_scrip_code) DO NOTHING",
                                       [(r.isin, r.nse_symbol, int(r.bse_scrip_code), r.link_basis, psycopg2.extras.Json(evidence.get((r.nse_symbol, int(r.bse_scrip_code)))), BUILT_BY) for r in xref.itertuples()])
    xref.to_csv(OUT / "listing_xref.csv", index=False)
    div.to_csv(OUT / "price_divergence.csv", index=False)
    bse_only_active.sort_values("scrip_code").to_csv(OUT / "bse_only_active_equities.csv", index=False)

    # report ---------------------------------------------------------------------
    nse_eq_syms = len(nse_syms)
    L = ["# BSE bhavcopy validation (first milestone)", "",
         f"- loaded: {len(bse_dates):,} sessions, {lo.date()} .. {hi.date()}; {int(cov['rows'].sum()):,} rows; {len(lm):,} distinct scrip codes",
         f"- sessions in NSE's archive inside that span but missing at BSE: **{len(nse_only)}**" + (f" ({', '.join(str(d.date()) for d in nse_only[:12])}{' ...' if len(nse_only) > 12 else ''})" if nse_only else ""),
         f"- sessions at BSE with no NSE session: **{len(bse_only_dates)}**" + (f" ({', '.join(str(d.date()) for d in bse_only_dates[:12])})" if bse_only_dates else ""), "",
         "## Coverage by year", "", "| year | sessions | rows | rows with ISIN | scrips | turnover (Rs bn) |", "|---|---|---|---|---|---|"]
    for r in cov.itertuples():
        L.append(f"| {r.year} | {r.sessions} | {int(r.rows):,} | {r.rows_isin / r.rows:.1%} | {r.scrips:,} | {r.turnover_bn:,.0f} |")
    t = lm["instrument_type"].value_counts().to_dict()
    L += ["", "## Instrument types (rule-based; evidence is stored per scrip)", "", f"- {t}", "",
          "## Cross-listing map", "",
          f"- ISIN-linked pairs: **{(xref['link_basis'] == 'isin').sum():,}**; ticker+price-linked (weaker tier): **{(xref['link_basis'] == 'ticker_price').sum():,}**",
          f"- NSE symbols ever loaded: {nse_eq_syms:,}; NSE symbols with a BSE link: {xref['nse_symbol'].nunique():,}",
          f"- active BSE equities on {last_day}: {len(active):,}; linked to NSE: {len(linked_active):,} ({len(linked_active) / max(len(active), 1):.1%}); **BSE-only: {len(bse_only_active):,}** "
          f"({share_only:.2%} of the last 60 days' traded value of active BSE equities)", ""]
    if len(div):
        L += ["## NSE vs BSE closing-price divergence (ISIN-linked pairs, last ~120 days)", "",
              f"- pairs {len(div):,}; median of per-pair median gap {div['median_gap'].median():.3%}; pairs with a median gap > 1%: {(div['median_gap'] > 0.01).sum()}; pairs with a max gap > 10%: {(div['max_gap'] > 0.10).sum()}", ""]
        worst = div.sort_values("max_gap", ascending=False).head(10)
        L += ["| NSE symbol | BSE scrip | days | median gap | max gap |", "|---|---|---|---|---|", *[f"| {r.nse_symbol} | {r.bse_scrip_code} | {r.days} | {r.median_gap:.2%} | {r.max_gap:.1%} |" for r in worst.itertuples()], ""]
    L += ["## Duplicates and conflicts", "", f"- ISINs carried by more than one BSE scrip code: {len(multi_scrip)}" + (f" (e.g. {', '.join(multi_scrip.index[:5])})" if len(multi_scrip) else ""),
          f"- BSE scrip codes that carry more than one ISIN over time (corporate actions / re-issues): {len(multi_isin)}",
          f"- BSE scrip codes linked to more than one NSE symbol: {len(multi_nse)}", "",
          "Files: `listing_xref.csv`, `price_divergence.csv`, `bse_only_active_equities.csv`."]
    (OUT / "bse_validation.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:22]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
