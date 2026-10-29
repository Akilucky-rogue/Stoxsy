"""BSE validation + the NSE <-> BSE cross-listing map (rebuilt deterministically from the exchange tables).

    python scripts/bse_validation_report.py            # -> data/reports/bse/bse_validation.md + csv; rebuilds bse_listing_master, listing_xref, xref_conflicts

Linking policy
  1. ISIN first. NSE ISINs come from three sources, all kept: NSE's EQUITY_L master (current), the NSE bhavcopy ISINs of 2016-2020, and the NSE
     corporate-action records. A BSE scrip whose ISIN equals an ISIN of an NSE symbol is a CANDIDATE link.
  2. Price check. Both exchanges print the same security at (almost) the same price on the same day, so over the last <= 250 shared sessions the closes must
     agree. median |gap| > 1%  or  any day > 10%  -> `unlinked_pending_review` (neither side may be stitched or used in a combined series). < 30 shared
     sessions -> `linked_unchecked`. Otherwise `linked_verified`.
  3. Weaker tier (no shared ISIN known): BSE ticker == NSE symbol and closes within 2% on >= 90% of >= 30 shared sessions. Labelled `ticker_price`.
Conflicts (one symbol with several ISINs, one ISIN on several symbols or scrips, master vs history disagreement) are RECORDED in xref_conflicts, never resolved by overwriting.
Review order: NSE liquidity (60-day traded value), membership of the NSE top-500 universe, conflicts, multiple BSE scrips.
BSE prices are raw (no corporate-action adjustment): `raw_prices_only`.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = data_path("reports", "bse")
BUILT_BY = "bse_validation_report.py v2"
DEBT_GROUPS = {"F", "G", "GS", "D", "DB", "DR", "DT", "E", "TB", "W"}
MEDIAN_GAP_LIMIT, MAX_GAP_LIMIT, MIN_OVERLAP, WINDOW = 0.01, 0.10, 30, 250


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
    import psycopg2.extras

    from qsde.db.connection import get_sync_conn, read_sql

    OUT.mkdir(parents=True, exist_ok=True)
    cov = read_sql("""SELECT extract(year from date)::int AS year, count(DISTINCT date) AS sessions, count(*) AS rows, count(isin) AS rows_isin,
                             count(DISTINCT scrip_code) AS scrips, sum(turnover_value)/1e9 AS turnover_bn FROM bse_eod_raw GROUP BY 1 ORDER BY 1""")
    if cov.empty:
        print("no BSE data loaded yet")
        return 1
    bse_dates = set(pd.to_datetime(read_sql("SELECT DISTINCT date FROM bse_eod_raw")["date"]))
    nse_dates = set(pd.to_datetime(read_sql("SELECT DISTINCT date FROM eod_raw WHERE exchange = 'NSE'")["date"]))
    lo, hi = min(bse_dates), max(bse_dates)
    nse_only = sorted(d for d in nse_dates if lo <= d <= hi and d not in bse_dates)
    bse_only_dates = sorted(d for d in bse_dates if d not in nse_dates)

    # BSE listing master -----------------------------------------------------------
    lm = read_sql("SELECT DISTINCT ON (scrip_code) scrip_code, ticker, name, grp, isin, date AS last_date FROM bse_eod_raw ORDER BY scrip_code, date DESC")
    lm = lm.merge(read_sql("SELECT scrip_code, min(date) AS first_date, count(*) AS n_days FROM bse_eod_raw GROUP BY 1"), on="scrip_code")
    lm = lm.merge(read_sql("SELECT scrip_code, max(isin) AS any_isin FROM bse_eod_raw WHERE isin IS NOT NULL GROUP BY 1"), on="scrip_code", how="left")
    lm["isin"] = lm["isin"].fillna(lm["any_isin"])
    cls = [classify(r) for r in lm.itertuples()]
    lm["instrument_type"], lm["type_evidence"] = [c[0] for c in cls], [c[1] for c in cls]

    # NSE ISIN knowledge (three sources, kept separately so disagreements are visible) -----
    master = read_sql("SELECT symbol, isin FROM nse_equity_master")
    hist = read_sql("SELECT DISTINCT symbol, isin FROM eod_raw WHERE isin IS NOT NULL AND exchange = 'NSE'")
    ca = read_sql("SELECT DISTINCT symbol, isin FROM nse_corporate_actions WHERE isin IS NOT NULL")
    master["src"], hist["src"], ca["src"] = "master", "bhavcopy_2016_2020", "corporate_actions"
    nse_isin = pd.concat([master, hist, ca], ignore_index=True)
    nse_syms = set(read_sql("SELECT DISTINCT symbol FROM eod_raw WHERE exchange = 'NSE'")["symbol"])
    nse_isin = nse_isin[nse_isin["symbol"].isin(nse_syms)]
    EQUITY_CODES = ("01", "02", "03", "04", "05", "06", "10", "20", "21", "22", "23")
    not_equity = nse_isin[~nse_isin["isin"].str[8:10].isin(EQUITY_CODES) | nse_isin["isin"].str.startswith("INF")]
    nse_isin = nse_isin[~nse_isin.index.isin(not_equity.index)]            # an NSE corporate-action record can carry a BOND's ISIN: it must not link a share
    pair_isin = nse_isin.groupby(["isin", "symbol"])["src"].apply(lambda s: sorted(set(s))).reset_index()

    conflicts = []
    sym_many = nse_isin.groupby("symbol")["isin"].nunique()
    for sym in sym_many[sym_many > 1].index:
        d = nse_isin[nse_isin["symbol"] == sym].groupby("isin")["src"].apply(lambda s: sorted(set(s))).to_dict()
        conflicts.append(("nse_symbol_many_isins", None, sym, None, d))
    isin_many = nse_isin.groupby("isin")["symbol"].nunique()
    for isin in isin_many[isin_many > 1].index:
        conflicts.append(("nse_isin_many_symbols", isin, None, None, {"symbols": sorted(nse_isin[nse_isin["isin"] == isin]["symbol"].unique())}))
    mh = master.merge(hist, on="symbol", suffixes=("_m", "_h"))
    for r in mh[mh["isin_m"] != mh["isin_h"]].itertuples():
        conflicts.append(("master_vs_history_isin", r.isin_m, r.symbol, None, {"master": r.isin_m, "bhavcopy_2016_2020": r.isin_h}))
    for isin, g in lm[lm["isin"].notna()].groupby("isin"):
        if g["scrip_code"].nunique() > 1:
            conflicts.append(("bse_isin_many_scrips", isin, None, None, {"scrips": [int(x) for x in g["scrip_code"]], "tickers": [t for t in g["ticker"] if isinstance(t, str)]}))

    for sym, g in not_equity.groupby("symbol"):
        conflicts.append(("nse_isin_not_an_equity_share", None, sym, None, {"isins": sorted(g["isin"].unique())[:8]}))

    # ISIN-first candidate links ---------------------------------------------------------
    bse_by_isin = lm[lm["isin"].notna() & (lm["instrument_type"] == "equity")].groupby("isin")["scrip_code"].apply(list).to_dict()
    cand = []
    for r in pair_isin.itertuples():
        for code in bse_by_isin.get(r.isin, []):
            cand.append((r.isin, r.symbol, int(code), "isin", "+".join(r.src)))
    cand = pd.DataFrame(cand, columns=["isin", "nse_symbol", "bse_scrip_code", "link_basis", "nse_isin_sources"]).drop_duplicates(["nse_symbol", "bse_scrip_code"])

    # ticker + price tier for BSE equities with no ISIN candidate -----------------------------
    linked_bse = set(cand["bse_scrip_code"])
    weak_c = lm[(~lm["scrip_code"].isin(linked_bse)) & lm["ticker"].isin(nse_syms) & (lm["instrument_type"] == "equity")]
    weak_pairs = pd.DataFrame({"isin": None, "nse_symbol": weak_c["ticker"].to_numpy(), "bse_scrip_code": weak_c["scrip_code"].astype(int).to_numpy(),
                               "link_basis": "ticker_price", "nse_isin_sources": ""})
    allp = pd.concat([cand, weak_pairs], ignore_index=True)

    # price agreement over the last <= 250 shared sessions, for every pair ------------------------
    bpx = read_sql("SELECT scrip_code, date, close FROM bse_eod_raw WHERE scrip_code = ANY(:s)", params={"s": [int(x) for x in allp["bse_scrip_code"].unique()]})
    npx = read_sql("SELECT symbol, date, close FROM eod_raw WHERE exchange = 'NSE' AND series = 'EQ' AND symbol = ANY(:s)", params={"s": list(allp["nse_symbol"].unique())})
    m = allp[["nse_symbol", "bse_scrip_code"]].merge(bpx, left_on="bse_scrip_code", right_on="scrip_code").merge(
        npx, left_on=["nse_symbol", "date"], right_on=["symbol", "date"], suffixes=("_b", "_n"))
    m["gap"] = (m["close_b"] / m["close_n"] - 1).abs()
    m = m.sort_values("date").groupby(["nse_symbol", "bse_scrip_code"]).tail(WINDOW)
    stat = m.groupby(["nse_symbol", "bse_scrip_code"]).agg(overlap_days=("gap", "size"), median_gap=("gap", "median"), max_gap=("gap", "max"),
                                                           share_within_2pct=("gap", lambda g: float((g <= 0.02).mean()))).reset_index()
    xref = allp.merge(stat, on=["nse_symbol", "bse_scrip_code"], how="left")
    xref["overlap_days"] = xref["overlap_days"].fillna(0).astype(int)

    def status(r):
        if r.link_basis == "ticker_price":
            return ("linked_verified", None) if (r.overlap_days >= MIN_OVERLAP and r.share_within_2pct >= 0.90) else (None, "ticker+price tier failed")
        if r.overlap_days < MIN_OVERLAP:
            return "linked_unchecked", f"only {r.overlap_days} shared sessions: prices cannot confirm the ISIN link"
        why = []
        if r.median_gap > MEDIAN_GAP_LIMIT:
            why.append(f"median close gap {r.median_gap:.2%} > {MEDIAN_GAP_LIMIT:.0%}")
        if r.max_gap > MAX_GAP_LIMIT:
            why.append(f"a day with a {r.max_gap:.1%} gap (> {MAX_GAP_LIMIT:.0%})")
        return ("unlinked_pending_review", "; ".join(why)) if why else ("linked_verified", None)

    res = [status(r) for r in xref.itertuples()]
    xref["link_status"], xref["review_reason"] = [s for s, _ in res], [w for _, w in res]
    xref = xref[xref["link_status"].notna()].copy()
    # a failed pair whose NSE symbol is already verified through ANOTHER BSE scrip is a legacy / secondary scrip: unlinked, but nothing to review
    ok_syms = xref[xref["link_status"] == "linked_verified"].groupby("nse_symbol")["bse_scrip_code"].apply(list).to_dict()
    sec = (xref["link_status"] == "unlinked_pending_review") & xref["nse_symbol"].isin(ok_syms)
    xref.loc[sec, "review_reason"] = xref.loc[sec].apply(lambda r: f"secondary/legacy scrip: {r.nse_symbol} is already linked_verified via BSE scrip {ok_syms[r.nse_symbol][0]} ({r.review_reason})", axis=1)
    xref.loc[sec, "link_status"] = "unlinked_secondary_scrip"

    # review order ------------------------------------------------------------------------------
    d0 = (hi - pd.Timedelta(days=90)).date()
    liq = read_sql("SELECT symbol, avg(close * volume) AS tv FROM eod_raw WHERE exchange = 'NSE' AND series = 'EQ' AND date >= CAST(:d AS DATE) GROUP BY 1", params={"d": d0})
    liqmap = dict(zip(liq["symbol"], liq["tv"]))
    top500 = set(read_sql("SELECT DISTINCT symbol FROM universe_pit WHERE method = 'adv60_top500_v1' AND date >= CAST(:d AS DATE)", params={"d": d0})["symbol"])
    multi_bse = xref.groupby("nse_symbol")["bse_scrip_code"].nunique()
    conf_syms = {c[2] for c in conflicts if c[2]} | {s for c in conflicts if c[0] == "nse_isin_many_symbols" for s in c[4]["symbols"]}
    bad = xref[xref["link_status"] == "unlinked_pending_review"].copy()
    bad["score"] = (bad["nse_symbol"].map(lambda s: np.log10(liqmap.get(s, 1) + 1)) + 3 * bad["nse_symbol"].isin(top500) +
                    1.5 * bad["nse_symbol"].isin(conf_syms) + 1.0 * (bad["nse_symbol"].map(multi_bse) > 1) + 1.0 * (bad["max_gap"] > MAX_GAP_LIMIT))
    bad["review_rank"] = bad["score"].rank(ascending=False, method="first").astype(int)
    xref = xref.merge(bad[["nse_symbol", "bse_scrip_code", "review_rank"]], on=["nse_symbol", "bse_scrip_code"], how="left")

    # BSE-only accounting ---------------------------------------------------------------------------
    usable = set(xref[xref["link_status"].isin(["linked_verified", "linked_unchecked"])]["bse_scrip_code"])
    eq = lm[lm["instrument_type"] == "equity"]
    last_day = lm["last_date"].max()
    active = eq[eq["last_date"] == last_day]
    tv = read_sql("SELECT scrip_code, sum(turnover_value) AS tv FROM bse_eod_raw WHERE date >= CAST(:d AS DATE) GROUP BY 1", params={"d": (hi - pd.Timedelta(days=60)).date()})
    tvm = dict(zip(tv["scrip_code"], tv["tv"]))
    total_tv = max(sum(tvm.get(int(c), 0) for c in active["scrip_code"]), 1)
    held = set(xref[xref["link_status"].isin(["unlinked_pending_review", "unlinked_secondary_scrip"])]["bse_scrip_code"])
    cats = {"linked (verified or unchecked)": active[active["scrip_code"].isin(usable)],
            "unlinked pending review": active[active["scrip_code"].isin(held) & ~active["scrip_code"].isin(usable)],
            "BSE-only (no NSE ISIN match)": active[~active["scrip_code"].isin(usable | held)]}
    nse_master_syms = set(master["symbol"])
    master_without_bse = sorted(s for s in nse_master_syms if s not in set(xref["nse_symbol"]))

    # persist -----------------------------------------------------------------------------------------
    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM bse_listing_master")
        cur.execute("DELETE FROM listing_xref")
        cur.execute("DELETE FROM xref_conflicts WHERE built_by = %s", (BUILT_BY,))
        psycopg2.extras.execute_values(cur, "INSERT INTO bse_listing_master (scrip_code, ticker, name, grp, isin, first_date, last_date, n_days, instrument_type, type_evidence) VALUES %s",
                                       [(int(r.scrip_code), r.ticker, r.name, r.grp, r.isin, r.first_date, r.last_date, int(r.n_days), r.instrument_type, r.type_evidence) for r in lm.itertuples()])
        nn = lambda v: None if v is None or (isinstance(v, float) and v != v) else v          # noqa: E731
        psycopg2.extras.execute_values(cur, """INSERT INTO listing_xref (isin, nse_symbol, bse_scrip_code, link_basis, link_status, review_reason, overlap_days, median_gap, max_gap,
                                               review_rank, evidence, built_by) VALUES %s ON CONFLICT (nse_symbol, bse_scrip_code) DO NOTHING""",
                                       [(r.isin, r.nse_symbol, int(r.bse_scrip_code), r.link_basis, r.link_status, nn(r.review_reason), int(r.overlap_days), nn(r.median_gap), nn(r.max_gap),
                                         None if pd.isna(r.review_rank) else int(r.review_rank), psycopg2.extras.Json({"nse_isin_sources": r.nse_isin_sources, "share_within_2pct": nn(r.share_within_2pct)}), BUILT_BY)
                                        for r in xref.itertuples()])
        if conflicts:
            psycopg2.extras.execute_values(cur, "INSERT INTO xref_conflicts (kind, isin, nse_symbol, bse_scrip_code, detail, built_by) VALUES %s",
                                           [(k, i, s, b, psycopg2.extras.Json(d), BUILT_BY) for k, i, s, b, d in conflicts])
    xref.drop(columns=["share_within_2pct"]).sort_values(["link_status", "review_rank"]).to_csv(OUT / "listing_xref.csv", index=False)
    rev = xref[xref["link_status"] == "unlinked_pending_review"].sort_values("review_rank")
    rev.merge(lm[["scrip_code", "ticker", "name"]], left_on="bse_scrip_code", right_on="scrip_code", how="left").to_csv(OUT / "review_queue_unlinked_pairs.csv", index=False)
    pd.DataFrame([{"kind": k, "isin": i, "nse_symbol": s, "bse_scrip_code": b, "detail": json.dumps(d, default=str)} for k, i, s, b, d in conflicts]).to_csv(OUT / "xref_conflicts.csv", index=False)
    cats["BSE-only (no NSE ISIN match)"].sort_values("scrip_code").to_csv(OUT / "bse_only_active_equities.csv", index=False)

    # report ----------------------------------------------------------------------------------------------
    t = lm["instrument_type"].value_counts().to_dict()
    cnt = xref["link_status"].value_counts().to_dict()
    L = ["# BSE validation and NSE <-> BSE cross-listing map", "",
         "> BSE prices are **raw_prices_only** (no corporate-action adjustment). BSE-only names are NOT in the universe, the defensive signal or the decision cards; "
         "pairs under review are unlinked: neither side may be stitched or combined.", "",
         f"- BSE loaded: {len(bse_dates):,} sessions, {lo.date()} .. {hi.date()}; {int(cov['rows'].sum()):,} rows; {len(lm):,} scrip codes (equity {t.get('equity', 0):,}, debt {t.get('debt', 0):,}, fund/ETF {t.get('fund_etf', 0):,})",
         f"- NSE sessions inside that span missing at BSE: **{len(nse_only)}**" + (f" ({', '.join(str(d.date()) for d in nse_only[:6])})" if nse_only else "") + f"; BSE sessions with no NSE session: {len(bse_only_dates)}",
         f"- NSE ISIN sources: master {len(master):,} symbols, 2016-2020 bhavcopy {hist['symbol'].nunique():,}, corporate actions {ca['symbol'].nunique():,}; union covers {nse_isin['symbol'].nunique():,} of {len(nse_syms):,} NSE symbols ever loaded", "",
         "## Cross-listing map", "",
         f"- candidate pairs: ISIN {int((xref['link_basis'] == 'isin').sum()):,}, ticker+price {int((xref['link_basis'] == 'ticker_price').sum()):,}",
         f"- **linked_verified {cnt.get('linked_verified', 0):,}**; linked_unchecked (< {MIN_OVERLAP} shared sessions) {cnt.get('linked_unchecked', 0):,}; unlinked_secondary_scrip {cnt.get('unlinked_secondary_scrip', 0):,} (legacy scrips of already-linked names: no review needed); **unlinked_pending_review {cnt.get('unlinked_pending_review', 0):,}** "
         f"(median close gap > {MEDIAN_GAP_LIMIT:.0%} or any day > {MAX_GAP_LIMIT:.0%} over the last <= {WINDOW} shared sessions)",
         f"- NSE master symbols with no BSE link at all: {len(master_without_bse):,}", "",
         f"## Active BSE equities on {last_day} ({len(active):,}) and their traded value (last 60 days)", "", "| status | scrips | share of traded value |", "|---|---|---|"]
    for k, v in cats.items():
        L.append(f"| {k} | {len(v):,} | {sum(tvm.get(int(c), 0) for c in v['scrip_code']) / total_tv:.2%} |")
    L += ["", "The updated BSE-only count is the third row: BSE equities with no NSE ISIN match even after the NSE equity master.", "",
          "## Review queue: unlinked pairs, highest impact first (liquidity, NSE top-500, conflicts, multiple BSE scrips, > 10% days)", "",
          "| rank | NSE | BSE scrip | ISIN | days | median gap | max gap | reason |", "|---|---|---|---|---|---|---|---|"]
    for r in rev.head(25).itertuples():
        L.append(f"| {int(r.review_rank)} | {r.nse_symbol} | {r.bse_scrip_code} | {r.isin} | {r.overlap_days} | {r.median_gap:.2%} | {r.max_gap:.1%} | {r.review_reason} |")
    kinds = pd.Series([c[0] for c in conflicts]).value_counts().to_dict()
    L += ["", "## Conflicts recorded (never overwritten)", "", f"- {kinds if kinds else 'none'}", "",
          "Files: `listing_xref.csv`, `review_queue_unlinked_pairs.csv`, `xref_conflicts.csv`, `bse_only_active_equities.csv`."]
    (OUT / "bse_validation.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:30]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
