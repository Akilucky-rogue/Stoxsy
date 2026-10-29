"""Decision packet for the top-N bucket A events (status `official_match_price_mismatch`). Recommends; applies nothing.

Ranking (highest first): universe membership, liquidity (best PIT rank), stitched-security impact, size of the residual move after the
listed factor, data-quality flags. Recommended decision (deterministic, shown with its reason; the owner makes the call):
  confirm              no flag, |residual| <= 2.5 sigma, gap-bar volume >= 0.5x median, and the NEXT 3 bars show no jump > 3 sigma after the factor
  reject               a demerger or other action within 10 days, or |residual| > 4 sigma
  needs_more_evidence  everything else

    python scripts/ca_bucket_a_packet.py --top 10
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")


def recommend(r, nxt_jump_sigma: float) -> tuple[str, str]:
    flags = str(r.flags) if pd.notna(r.flags) else ""
    z = abs(r.residual_in_sigma)
    if "demerger" in flags or "other action" in flags:
        return "reject", "a demerger or another corporate action sits within 10 days: the gap cannot be attributed to this action alone"
    if z > 4:
        return "reject", f"residual {z:.1f} sigma after the listed factor is too large to be an ordinary move"
    if flags:
        return "needs_more_evidence", "flags: " + flags
    if z <= 2.5 and r.gap_volume_x_median >= 0.5 and nxt_jump_sigma <= 3:
        return "confirm", f"residual {z:.1f} sigma, volume {r.gap_volume_x_median:.1f}x median, no jump in the next 3 bars ({nxt_jump_sigma:.1f} sigma max)"
    why = []
    if z > 2.5:
        why.append(f"residual {z:.1f} sigma > 2.5")
    if r.gap_volume_x_median < 0.5:
        why.append(f"thin gap bar ({r.gap_volume_x_median:.2f}x median)")
    if nxt_jump_sigma > 3:
        why.append(f"a {nxt_jump_sigma:.1f} sigma move in the next 3 bars")
    return "needs_more_evidence", "; ".join(why)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    args = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    w = pd.read_csv(OUT / "review_bucket_a_official_match_price_mismatch.csv")
    w["gap_date"] = pd.to_datetime(w["gap_date"])
    syms = sorted(w["symbol"].unique())
    sm = read_sql("SELECT security_id, symbol FROM security_master WHERE symbol = ANY(:s)", params={"s": syms})
    multi = read_sql("SELECT security_id, count(DISTINCT symbol) n FROM security_master GROUP BY security_id HAVING count(DISTINCT symbol) > 1")
    stitched = set(sm.merge(multi, on="security_id")["symbol"])
    w["stitched"] = w["symbol"].isin(stitched)
    w["priority"] = (3 * w["in_universe_within_60d"].astype(int) + 2 * (w["best_universe_rank"].fillna(9999) <= 100).astype(int)
                     + 1 * (w["best_universe_rank"].fillna(9999) <= 250).astype(int) + 1 * w["stitched"].astype(int)
                     + w["residual_in_sigma"].abs().clip(upper=4) / 4 - 2 * w["flags"].notna().astype(int))
    top = w.sort_values("priority", ascending=False).head(args.top)
    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE' AND symbol = ANY(:s)",
                   params={"s": sorted(top["symbol"].unique())})
    raw["date"] = pd.to_datetime(raw["date"])
    bars = {s: g.sort_values("date").reset_index(drop=True) for s, g in nse_eod.pick_primary_series(raw).groupby("symbol")}
    L = [f"# Bucket A decision packet - top {len(top)} (status `official_match_price_mismatch`)", "",
         "Recommendations only. Nothing is applied; the tolerance is unchanged. The leftover move after applying the LISTED factor is the thing to judge: "
         "an ordinary move (within ~2-3 daily sigma, ordinary volume, no jump afterwards) supports the listed factor; a large or one-off residual does not. "
         "The 90%-positive residual pattern across all 69 is a hypothesis (ex-date price reaction), not evidence that any factor is right.", "",
         "| # | symbol | gap bar | NSE | listed x | observed x | residual | sigma | rank | stitched | recommendation |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    detail, rec_rows = [], []
    for n, r in enumerate(top.itertuples(), 1):
        g = bars[r.symbol]
        i = int(g.index[g["date"] >= r.gap_date][0])
        sig = r.daily_sigma_60d
        nxt = g.iloc[i + 1: i + 4]["close"].pct_change().abs().dropna()
        first = g.iloc[i + 1]["close"] / g.iloc[i]["close"] - 1.0 if i + 1 < len(g) else 0.0
        jump = max([abs(first)] + nxt.tolist()) / sig if sig else 0.0
        dec, why = recommend(r, jump)
        rec_rows.append({"symbol": r.symbol, "gap_date": r.gap_date.date(), "recommended": dec, "reason": why, "decision": ""})
        L.append(f"| {n} | {r.symbol} | {r.gap_date.date()} | {str(r.nse_subject)[:30]} | {r.listed_factor:.3f} | {r.observed_ratio:.3f} | "
                 f"{r.residual_overnight_after_listed_factor:+.1%} | {r.residual_in_sigma:+.1f} | {'' if pd.isna(r.best_universe_rank) else int(r.best_universe_rank)} | "
                 f"{'yes' if r.stitched else 'no'} | **{dec}** |")
        win = g.iloc[max(0, i - 3): i + 4].copy()
        win["adj_close"] = np.where(win["date"] < r.gap_date, win["close"] * r.listed_factor, win["close"])
        win["adj_ret"] = win["adj_close"].pct_change()
        detail += ["", f"### {n}. {r.symbol} - {r.nse_subject} (ISIN {r.isin})", "",
                   f"- NSE record: ex-date {pd.Timestamp(r.nse_ex_date).date()}, subject `{r.nse_subject}`; proposed factor **{r.listed_factor:.4f}** "
                   f"(source: {r.proposed_factor_source}); applied in the current official `ohlcv`: {r.applied_in_ohlcv:.4f}",
                   f"- residual overnight move after the listed factor {r.residual_overnight_after_listed_factor:+.2%} ({r.residual_in_sigma:+.1f} sigma of {sig:.2%}); "
                   f"gap-bar volume {r.gap_volume_x_median:.1f}x median; next-5-session return {r.next5d_return:+.1%}; best PIT rank {r.best_universe_rank}",
                   f"- recommendation: **{dec}** - {why}", "", "| date | open | high | low | close | volume | close x listed factor (pre-event) | return after factor |",
                   "|---|---|---|---|---|---|---|---|"]
        for x in win.itertuples():
            mark = " **<- gap bar**" if x.date == g.iloc[i]["date"] else ""
            detail.append(f"| {x.date.date()}{mark} | {x.open:.2f} | {x.high:.2f} | {x.low:.2f} | {x.close:.2f} | {int(x.volume):,} | {x.adj_close:.2f} | "
                          f"{'' if pd.isna(x.adj_ret) else f'{x.adj_ret:+.1%}'} |")
    (OUT / "packet_bucket_a_top10.md").write_text("\n".join(L + detail), encoding="utf-8")
    pd.DataFrame(rec_rows).to_csv(OUT / "packet_bucket_a_top10.csv", index=False)
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
