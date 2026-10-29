"""One-page evidence packs for NSE <-> BSE pairs that failed the price check. Recommends a status; the owner decides.

    python scripts/xref_evidence_packs.py TRENT:600251 SOUTHBANK:632218 ...
Pack contents: ISIN history from the three NSE sources, every BSE scrip that carries the ISIN, the shared-session price overlay and the divergence days,
liquidity on each side, NSE corporate actions and symbol changes near the divergence, BSE data gaps, and a rule-based recommended status:
  linked_verified      the gap is small and unsystematic once thin-trading days are looked at (never produced automatically: needs a person)
  unlinked             the BSE scrip is a different instrument (partly-paid, DVR, rights entitlement, other share class) or the ISIN does not belong to it
  needs_manual_review  anything else, with the reason
"""
from __future__ import annotations

import re
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = data_path("reports", "bse", "evidence_packs")
DIFF_CLASS = re.compile(r"partly|part paid|\bpp\b|\bdvr\b|differential|rights entitlement|\bre\b|\bpaid.?up\b|warrant|\bnew\b", re.I)


def pack(sym: str, code: int, read_sql) -> tuple[str, str, str]:
    isins = read_sql("""SELECT isin, 'master' src FROM nse_equity_master WHERE symbol = :s UNION SELECT DISTINCT isin, 'bhavcopy_2016_2020' FROM eod_raw WHERE symbol = :s AND isin IS NOT NULL
                        UNION SELECT DISTINCT isin, 'corporate_actions' FROM nse_corporate_actions WHERE symbol = :s AND isin IS NOT NULL""", params={"s": sym})
    bse = read_sql("SELECT scrip_code, ticker, name, grp, isin, first_date, last_date, n_days FROM bse_listing_master WHERE scrip_code = :c", params={"c": code})
    same_isin = read_sql("""SELECT l.scrip_code, l.ticker, l.name, l.grp, l.first_date, l.last_date, l.n_days FROM bse_listing_master l WHERE l.isin = (SELECT isin FROM bse_listing_master WHERE scrip_code = :c)
                            ORDER BY scrip_code""", params={"c": code})
    ov = read_sql("""SELECT b.date, n.close AS nse_close, b.close AS bse_close, n.volume AS nse_vol, b.volume AS bse_vol, b.trades AS bse_trades
                     FROM bse_eod_raw b JOIN eod_raw n ON n.exchange = 'NSE' AND n.series = 'EQ' AND n.symbol = :s AND n.date = b.date WHERE b.scrip_code = :c ORDER BY b.date""", params={"s": sym, "c": code})
    ov["gap"] = ov["bse_close"] / ov["nse_close"] - 1
    win = ov.tail(250)
    big = win[win["gap"].abs() > 0.02]
    ca = read_sql("SELECT ex_date, subject, category FROM nse_corporate_actions WHERE symbol = :s AND ex_date >= CAST(:a AS DATE) ORDER BY ex_date", params={"s": sym, "a": str(win["date"].min())}) if len(win) else pd.DataFrame()
    chg = read_sql("SELECT old_symbol, new_symbol, effective_date FROM symbol_changes WHERE (old_symbol = :s OR new_symbol = :s)", params={"s": sym})
    bse_dates = read_sql("SELECT date FROM bse_eod_raw WHERE scrip_code = :c AND date >= CAST(:a AS DATE) ORDER BY date", params={"c": code, "a": str(win["date"].min())})
    nse_days = read_sql("SELECT date FROM eod_raw WHERE exchange = 'NSE' AND symbol = :s AND series = 'EQ' AND date >= CAST(:a AS DATE)", params={"s": sym, "a": str(win["date"].min())})
    missing_bse = len(set(pd.to_datetime(nse_days["date"])) - set(pd.to_datetime(bse_dates["date"])))

    name = str(bse["name"].iloc[0]) if len(bse) else ""
    med, mx = float(win["gap"].abs().median()), float(win["gap"].abs().max())
    signed = float(win["gap"].median())
    share_same_sign = float((np.sign(win["gap"]) == np.sign(signed)).mean())
    thin = float(win["bse_trades"].median()) if len(win) else float("nan")
    reasons, status = [], "needs_manual_review"
    ok = read_sql("SELECT bse_scrip_code, median_gap, overlap_days FROM listing_xref WHERE nse_symbol = :s AND link_status = 'linked_verified' AND bse_scrip_code <> :c", params={"s": sym, "c": code})
    if len(ok):
        o = ok.iloc[0]
        status = "unlinked"
        reasons.append(f"{sym} is ALREADY linked_verified through BSE scrip {int(o['bse_scrip_code'])} (median close gap {float(o['median_gap']):.2%} over {int(o['overlap_days'])} shared sessions); "
                       f"scrip {code} is a {'short-lived legacy line' if len(bse) and bse['last_date'].iloc[0] < pd.Timestamp('2025-01-01').date() else 'secondary line'} "
                       f"({bse['first_date'].iloc[0] if len(bse) else ''} .. {bse['last_date'].iloc[0] if len(bse) else ''}, {bse['n_days'].iloc[0] if len(bse) else 0} days): keep it unlinked; the name itself is not at risk")
    if DIFF_CLASS.search(name):
        status = "unlinked"; reasons.append(f"BSE name '{name}' indicates a different instrument (partly-paid / DVR / entitlement / other class)")
    if len(same_isin) > 1:
        reasons.append(f"{len(same_isin)} BSE scrip codes carry this ISIN ({', '.join(str(c) for c in same_isin['scrip_code'])}): BSE may list a legacy/duplicate scrip; the live one should carry the link")
    if len(win) and signed and share_same_sign > 0.8 and med > 0.01:
        reasons.append(f"the gap is SYSTEMATIC: BSE closes are {signed:+.2%} vs NSE on {share_same_sign:.0%} of days (a share-class, lot-size, or price-definition difference, not noise)")
    if thin == thin and thin < 30:
        reasons.append(f"BSE trades are thin (median {thin:.0f} trades/day): closes can be stale prints")
    if len(big) and not len(ca):
        reasons.append("large gap days with no NSE corporate action in the window")
    if missing_bse:
        reasons.append(f"BSE has no row on {missing_bse} sessions where NSE traded")
    if status != "unlinked" and not reasons:
        reasons.append("no explanation found for the gap in the data available here")

    L = [f"# Evidence pack: NSE {sym} <-> BSE scrip {code}", "",
         f"**Recommended status: `{status}`** (the owner decides; nothing is applied)", "", "## Why", "", *[f"- {r}" for r in reasons], "",
         "## Identity", "",
         f"- NSE ISINs by source: " + ("; ".join(f"{r.isin} ({r.src})" for r in isins.itertuples()) if len(isins) else "none"),
         f"- BSE scrip: {code}, ticker {bse['ticker'].iloc[0] if len(bse) else None}, name '{name}', group {bse['grp'].iloc[0] if len(bse) else None}, ISIN {bse['isin'].iloc[0] if len(bse) else None}, "
         f"{bse['first_date'].iloc[0] if len(bse) else ''} .. {bse['last_date'].iloc[0] if len(bse) else ''} ({bse['n_days'].iloc[0] if len(bse) else 0} days)",
         "- all BSE scrips with this ISIN: " + ", ".join(f"{r.scrip_code} ({r.ticker}, {r.grp}, {r.first_date}..{r.last_date})" for r in same_isin.itertuples()), "",
         "## Price agreement (last <= 250 shared sessions)", "",
         f"- shared sessions {len(win)}; median |gap| {med:.2%}; max |gap| {mx:.1%}; median signed gap {signed:+.2%}; days with |gap| > 2%: {len(big)}",
         f"- liquidity: NSE median volume {win['nse_vol'].median():,.0f}/day, BSE {win['bse_vol'].median():,.0f}/day (BSE trades median {thin:.0f})",
         f"- NSE corporate actions in the window: " + ("; ".join(f"{r.ex_date} {r.subject}" for r in ca.itertuples()) if len(ca) else "none"),
         f"- NSE symbol changes: " + ("; ".join(f"{r.old_symbol}->{r.new_symbol} {r.effective_date}" for r in chg.itertuples()) if len(chg) else "none"), "",
         "### Largest divergence days", "", "| date | NSE close | BSE close | gap | BSE trades |", "|---|---|---|---|---|"]
    for r in win.reindex(win["gap"].abs().sort_values(ascending=False).index).head(8).itertuples():
        L.append(f"| {r.date} | {r.nse_close:.2f} | {r.bse_close:.2f} | {r.gap:+.2%} | {r.bse_trades} |")
    L += ["", "### Most recent 10 shared sessions", "", "| date | NSE close | BSE close | gap |", "|---|---|---|---|"]
    for r in win.tail(10).itertuples():
        L.append(f"| {r.date} | {r.nse_close:.2f} | {r.bse_close:.2f} | {r.gap:+.2%} |")
    return status, "\n".join(L), "; ".join(reasons)


def main() -> int:
    from qsde.db.connection import execute_sql, read_sql

    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for arg in sys.argv[1:]:
        sym, code = arg.split(":")
        status, md, why = pack(sym, int(code), read_sql)
        (OUT / f"{sym}_{code}.md").write_text(md, encoding="utf-8")
        rows.append((sym, code, status, why))
        print(f"{sym} <-> {code}: {status}\n   {why}")
    pd.DataFrame(rows, columns=["nse_symbol", "bse_scrip_code", "recommended_status", "reasons"]).assign(decision="").to_csv(OUT / "recommendations.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
