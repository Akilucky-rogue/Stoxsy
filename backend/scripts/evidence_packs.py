"""One-page evidence pack per proposed corporate-action confirmation (READ-ONLY; applies nothing).

For each (symbol, listed ex-date) it assembles: the NSE listing record as the feed holds it, the raw price series 10
sessions either side, the overnight gap vs the listed factor, three-session persistence, symbol/ISIN continuity, the
proposed adjusted-price calculation and the return continuity it would produce.

    python scripts/evidence_packs.py                       # the five confirm_and_apply proposals
    python scripts/evidence_packs.py --items DOLPHIN:2024-01-30 UEL:2025-10-08
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\corp_actions\evidence")
DEFAULT = ["DOLPHIN:2024-01-30", "DRCSYSTEMS:2022-03-16", "NDRAUTO:2023-07-24", "UEL:2025-10-08", "VERTOZ:2025-06-24"]


def pack(symbol: str, listed: pd.Timestamp, read_sql, nse_eod, load_source) -> tuple[str, dict]:
    src = load_source()
    s = src[(src["symbol"] == symbol) & ((src["ex_date"] - listed).abs() <= pd.Timedelta(days=15))]
    raw = read_sql("SELECT symbol, series, date, isin, open, high, low, close, prev_close, volume, source_file FROM eod_raw "
                   "WHERE symbol = :s AND date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE)",
                   params={"s": symbol, "a": str((listed - pd.Timedelta(days=45)).date()),
                           "b": str((listed + pd.Timedelta(days=45)).date())})
    raw["date"] = pd.to_datetime(raw["date"])
    px = nse_eod.pick_primary_series(raw).sort_values("date").reset_index(drop=True)
    factor = float(s["factor"].dropna().iloc[0]) if len(s) and s["factor"].notna().any() else float("nan")
    verdict = {"symbol": symbol, "listed": str(listed.date()), "factor": factor, "checks": {}}

    px["gap"] = px["open"] / px["close"].shift(1)
    cal = read_sql("SELECT DISTINCT date FROM eod_raw WHERE date BETWEEN CAST(:a AS DATE) AND CAST(:b AS DATE) ORDER BY date",
                   params={"a": str((listed - pd.Timedelta(days=45)).date()), "b": str((listed + pd.Timedelta(days=45)).date())})
    cal_d = pd.to_datetime(cal["date"]).to_numpy()
    px["sessions_skipped"] = [np.nan] + [int(((cal_d > px["date"].iloc[i - 1].to_datetime64()) &
                                              (cal_d < px["date"].iloc[i].to_datetime64())).sum()) for i in range(1, len(px))]
    i0 = int(np.searchsorted(px["date"].to_numpy(), np.datetime64(listed)))
    win = px.iloc[max(1, i0 - 6):i0 + 7]
    k = (win["gap"] / factor - 1.0).abs().idxmin() if np.isfinite(factor) and len(win) else None
    L = ["# Evidence pack: %s  (listed ex-date %s)" % (symbol, listed.date()), ""]
    L += ["## 1. NSE listing record (as held in the StockTrack feed)", ""]
    if len(s):
        for r in s.itertuples():
            L.append(f"- action_type `{r.src_type}` | purpose: {r.purpose} | parsed: {r.detail} | factor {r.factor} | "
                     f"ex-date {r.ex_date.date()}")
    else:
        L.append("- (no matching source row)")
    L.append("- record date: **not in feed**; face value: only inside the purpose text; ISIN: see section 5")
    L.append("")
    if k is None:
        return "\n".join(L + ["No price window."]), verdict
    gap_date = px.loc[k, "date"]
    prev_close = float(px.loc[k - 1, "close"])
    L += ["## 2. Raw prices, 10 sessions either side of the observed gap", "",
          "| date | series | open | close | prev_close(NSE) | open/prev close(ours) | skipped sessions before |", "|---|---|---|---|---|---|---|"]
    for r in px.iloc[max(0, k - 10):k + 11].itertuples():
        mark = "  <-- gap" if r.date == gap_date else ""
        L.append(f"| {r.date.date()}{mark} | {r.series} | {r.open:.2f} | {r.close:.2f} | {r.prev_close:.2f} | "
                 f"{'' if pd.isna(r.gap) else format(r.gap, '.3f')} | {'' if pd.isna(r.sessions_skipped) else int(r.sessions_skipped)} |")
    gap = float(px.loc[k, "gap"])
    err = abs(gap / factor - 1.0)
    post = px.loc[k:k + 3, "close"]
    persist = float(post.iloc[:3].median() / prev_close) if len(post) >= 3 else np.nan
    shift = (gap_date - listed).days
    skipped = int(px.loc[k, "sessions_skipped"]) if pd.notna(px.loc[k, "sessions_skipped"]) else 0
    L += ["", "## 3. Gap, factor, persistence", "",
          f"- observed overnight gap (open / last traded close): **{gap:.4f}** on {gap_date.date()} "
          f"({shift:+d} calendar days from the listed date); listed factor **{factor:.4f}**; error {err:.1%}",
          f"- sessions the stock did not trade between the previous bar and the gap bar: **{skipped}** "
          f"(production reconciler requires 0, which is why this was SOURCE_UNCONFIRMED)",
          f"- 3-session persistence: median close of the first 3 post-gap sessions / last pre-gap close = **{persist:.4f}** "
          f"(vs factor {factor:.4f}; error {abs(persist / factor - 1):.1%})"]
    # continuity of symbol / ISIN
    isins = sorted(px["isin"].dropna().unique())
    others = read_sql("SELECT symbol, open, prev_close FROM eod_raw WHERE date = CAST(:d AS DATE) AND symbol <> :s AND series = 'EQ' "
                      "AND prev_close > 0", params={"d": str(gap_date.date()), "s": symbol})
    others["ro"] = others["open"] / others["prev_close"]
    twin = others[(others["ro"] / factor - 1).abs() <= 0.03]
    L += ["", "## 4. Symbol / ISIN continuity", "",
          f"- bars exist for `{symbol}` on both sides of the date: **{'yes' if (px['date'] < gap_date).any() and (px['date'] >= gap_date).any() else 'NO'}**",
          f"- ISINs in window: {isins or 'n/a (archive has no ISIN after mid-2020)'}",
          f"- other symbols gapping by the same factor (+-3%) on that date: {', '.join(twin['symbol'].head(5)) or 'none'} "
          f"(informational: another stock with the same ratio on one day is usually chance; a rename is excluded by the both-sides check)"]
    # proposed adjustment
    adj_prev = prev_close * factor
    open_adj_ret = float(px.loc[k, "open"]) / adj_prev - 1.0
    pre_med = float(px.loc[max(0, k - 10):k - 1, "close"].pct_change().abs().median())
    L += ["", "## 5. Proposed adjustment and return continuity", "",
          f"- every bar strictly before {gap_date.date()} multiplied by {factor:.6f} (volume divided by it)",
          f"- adjusted previous close = {prev_close:.4f} x {factor:.4f} = {adj_prev:.4f}; open on the gap bar = {float(px.loc[k, 'open']):.4f}",
          f"- overnight return after adjustment: **{open_adj_ret:+.2%}** (typical absolute daily move in the 10 prior sessions: {pre_med:.2%})"]
    ok_gap = err <= (0.10 if (factor <= 0.8 or factor >= 1.25) else 0.06)
    ok_persist = np.isfinite(persist) and abs(persist / factor - 1) <= 0.10
    ok_cont = abs(open_adj_ret) <= max(4 * pre_med, 0.05) or abs(open_adj_ret) <= 0.10
    ok_sym = bool((px['date'] < gap_date).any() and (px['date'] >= gap_date).any())   # a rename has bars on one side only
    verdict.update({"gap_date": str(gap_date.date()), "prev_bar_date": str(px.loc[k - 1, "date"].date()), "observed_gap": gap,
                    "persistence": persist, "sessions_skipped": skipped, "purpose": (s["purpose"].iloc[0] if len(s) else None)})
    verdict["checks"] = {"gap_matches_listing": bool(ok_gap), "persists_3_sessions": bool(ok_persist),
                         "adjusted_return_continuous": bool(ok_cont), "same_symbol_trades_on_both_sides": bool(ok_sym)}
    rec = "READY FOR YOUR APPROVAL" if all(verdict["checks"].values()) else "HOLD - at least one check failed"
    L += ["", "## 6. Checklist", ""] + [f"- [{'x' if v else ' '}] {k_}" for k_, v in verdict["checks"].items()]
    L += ["", f"**Recommendation: {rec}.** Applying it would change the official `ohlcv` for `{symbol}` before "
              f"{gap_date.date()} and requires a rebuild; nothing has been applied."]
    return "\n".join(L), verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--items", nargs="*", default=DEFAULT)
    args = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod
    from qsde.ingestion.corp_actions_source import load_source

    OUT.mkdir(parents=True, exist_ok=True)
    summary = ["# Evidence packs - summary", ""]
    proposals = {}
    for it in args.items:
        sym, d = it.split(":")
        text, v = pack(sym, pd.Timestamp(d), read_sql, nse_eod, load_source)
        (OUT / f"{sym}_{d}.md").write_text(text, encoding="utf-8")
        summary.append(f"- {sym} {d}: {v['checks'] or 'no evidence'}")
        proposals[f"{sym}:{d}"] = v
        print(sym, d, v["checks"])
    (OUT / "SUMMARY.md").write_text("\n".join(summary), encoding="utf-8")
    import json
    (OUT / "proposals.json").write_text(json.dumps(proposals, indent=2, default=str), encoding="utf-8")
    print("packs ->", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
