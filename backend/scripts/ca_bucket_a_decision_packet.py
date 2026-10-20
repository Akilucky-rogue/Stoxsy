"""Bucket A decision packet in compact form: one block per event, everything the owner needs to confirm / reject / ask for more evidence. Applies NOTHING.

    python scripts/ca_bucket_a_decision_packet.py --top 10
Per event: symbol, NSE action type, listed ratio, ex-date, ISIN; observed gap date and factor; the residual move after applying the LISTED factor; halt / thin-trading /
rename / demerger flags; a price table for the 5 sessions before and after the gap bar; the system's recommendation (confirm | reject | needs_more_evidence) with its reason;
and what is in force TODAY for the event (so the owner decides knowing the current state). Ranking and recommendation rules are those of ca_bucket_a_packet.py.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from ca_bucket_a_packet import recommend

OUT = Path(r"D:\Stoxsy\data\reports\corp_actions")
SIGNAL = Path(r"D:\Stoxsy\data\signals\defensive\latest.parquet")
DEMERGER = r"demerg|de-merg|spin|scheme of arrangement|amalgam|merger|capital reduction|reduction of capital"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--remaining", action="store_true", help="every event WITHOUT an owner decision (in priority order), written to packet_bucket_a_decision_remaining.*")
    args = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    w = pd.read_csv(OUT / "review_bucket_a_official_match_price_mismatch.csv")
    w["gap_date"] = pd.to_datetime(w["gap_date"])
    sm = read_sql("SELECT security_id, symbol FROM security_master WHERE symbol = ANY(:s)", params={"s": sorted(w["symbol"].unique())})
    multi = read_sql("SELECT security_id FROM security_master GROUP BY security_id HAVING count(DISTINCT symbol) > 1")
    w["stitched"] = w["symbol"].isin(set(sm.merge(multi, on="security_id")["symbol"]))
    w["priority"] = (3 * w["in_universe_within_60d"].astype(int) + 2 * (w["best_universe_rank"].fillna(9999) <= 100).astype(int) + 1 * (w["best_universe_rank"].fillna(9999) <= 250).astype(int)
                     + 1 * w["stitched"].astype(int) + w["residual_in_sigma"].abs().clip(upper=4) / 4 - 2 * w["flags"].notna().astype(int))
    ranked = w.sort_values("priority", ascending=False)
    if args.remaining:
        from qsde.ingestion import bucket_a_decisions as BA
        done = {(x["symbol"], x["ex_date"]) for x in BA.load()["decisions"]}
        ranked = ranked[[(r.symbol, str(pd.Timestamp(r.nse_ex_date).date())) not in done for r in ranked.itertuples()]]
    top = ranked.head(len(ranked) if args.remaining else args.top).reset_index(drop=True)
    stem = "packet_bucket_a_decision_remaining" if args.remaining else "packet_bucket_a_decision_top10"
    syms = sorted(top["symbol"].unique())
    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE' AND symbol = ANY(:s)", params={"s": syms})
    raw["date"] = pd.to_datetime(raw["date"])
    bars = {s: g.sort_values("date").reset_index(drop=True) for s, g in nse_eod.pick_primary_series(raw).groupby("symbol")}
    acts = read_sql("SELECT symbol, category, subject, ex_date FROM nse_corporate_actions WHERE symbol = ANY(:s)", params={"s": syms})
    acts["ex_date"] = pd.to_datetime(acts["ex_date"])
    chg = read_sql("SELECT old_symbol, new_symbol, effective_date FROM symbol_changes WHERE old_symbol = ANY(:s) OR new_symbol = ANY(:s)", params={"s": syms})
    chg["effective_date"] = pd.to_datetime(chg["effective_date"])
    in_force = read_sql("SELECT symbol, ex_date, applied, method FROM corp_action_events WHERE symbol = ANY(:s) AND kind = 'SPLIT_BONUS'", params={"s": syms})
    in_force["ex_date"] = pd.to_datetime(in_force["ex_date"])
    reg = read_sql("SELECT symbol, nse_ex_date FROM adjustment_events_applied WHERE symbol = ANY(:s)", params={"s": syms})
    reg["nse_ex_date"] = pd.to_datetime(reg["nse_ex_date"])
    sig = pd.read_parquet(SIGNAL) if SIGNAL.exists() else pd.DataFrame(columns=["symbol", "date"])
    sig_date = pd.to_datetime(sig["date"]).max() if len(sig) else None

    head = [f"# Bucket A decision packet (compact): {'the ' + str(len(top)) + ' events without a decision' if args.remaining else 'top ' + str(len(top))} of {len(w)} events, status `official_match_price_mismatch`", "",
            "Nothing here approves or applies anything. The NSE record matches (type, ratio, ex-date, ISIN) but the observed price gap differs from the listed factor by 3-10%, outside the hard tolerance. "
            "Judge the **residual move after the listed factor**: an ordinary move (about 2-3 daily sigma, ordinary volume, no jump afterwards) supports the listed factor; a large or one-off residual does not.",
            "", "| # | symbol | action | listed x | ex-date | observed x | residual | sigma | flags | recommendation |", "|---|---|---|---|---|---|---|---|---|---|"]
    body, rows = [], []
    for n, r in enumerate(top.itertuples(), 1):
        g = bars[r.symbol]
        i = int(g.index[g["date"] >= r.gap_date][0])
        sigma = r.daily_sigma_60d
        pre60 = g.iloc[max(0, i - 60): i]
        med_vol = float(pre60["volume"].median()) if len(pre60) else float("nan")
        # flags ---------------------------------------------------------------------------------------------------------------------------------------------
        win = g.iloc[max(0, i - 5): i + 6]
        span_gaps = win["date"].diff().dt.days.fillna(0)
        zero_vol = int((win["volume"] <= 0).sum())
        halted = zero_vol > 0 or int((span_gaps > 4).sum()) > 0
        gv = float(r.gap_volume_x_median) * float(r.listed_factor)         # raw volume is in post-event share units after the action: scale to like-for-like with the pre-event median
        thin = bool(gv < 0.5)
        near = acts[(acts["symbol"] == r.symbol) & ((acts["ex_date"] - r.gap_date).abs() <= pd.Timedelta(days=10)) & (acts["ex_date"] != pd.Timestamp(r.nse_ex_date))]
        demerger = near[near["subject"].str.contains(DEMERGER, case=False, na=False) | near["category"].str.contains(DEMERGER, case=False, na=False)]
        other = near[~near.index.isin(demerger.index)]
        ren = chg[((chg["old_symbol"] == r.symbol) | (chg["new_symbol"] == r.symbol)) & ((chg["effective_date"] - r.gap_date).abs() <= pd.Timedelta(days=90))]
        flags = []
        if halted:
            flags.append(f"halt/missing bars ({zero_vol} zero-volume, {int((span_gaps > 4).sum())} calendar gaps > 4d in +/-5 sessions)")
        if thin:
            flags.append(f"thin gap bar ({gv:.2f}x the pre-event median volume, like-for-like share units)")
        if len(ren) or r.stitched:
            flags.append("rename / stitched security" + (": " + "; ".join(f"{a}->{b} {pd.Timestamp(d).date()}" for a, b, d in zip(ren["old_symbol"], ren["new_symbol"], ren["effective_date"])) if len(ren) else ""))
        if len(demerger):
            flags.append("demerger / scheme nearby: " + "; ".join(f"{str(s)[:50]} {pd.Timestamp(d).date()}" for s, d in zip(demerger["subject"], demerger["ex_date"])))
        if len(other):
            flags.append("other action within 10 days: " + "; ".join(f"{str(s)[:40]} {pd.Timestamp(d).date()}" for s, d in zip(other["subject"], other["ex_date"])))
        nxt = g.iloc[i + 1: i + 4]["close"].pct_change().abs().dropna()
        first = g.iloc[i + 1]["close"] / g.iloc[i]["close"] - 1.0 if i + 1 < len(g) else 0.0
        jump = max([abs(first)] + nxt.tolist()) / sigma if sigma else 0.0
        rr = r._replace(gap_volume_x_median=gv, flags=("; ".join(["demerger"] * bool(len(demerger)) + ["other action"] * bool(len(other)) + ["halt"] * halted + ["rename"] * bool(len(ren)))) or np.nan) if hasattr(r, "_replace") else r
        dec, why = recommend(rr, jump)
        # what is in force today ---------------------------------------------------------------------------------------------------------------------------
        ev = in_force[(in_force["symbol"] == r.symbol) & (in_force["ex_date"] == pd.Timestamp(r.nse_ex_date))]
        in_ohlcv = bool(len(ev) and ev["applied"].iloc[0])
        in_registry = bool(((reg["symbol"] == r.symbol) & (reg["nse_ex_date"] == pd.Timestamp(r.nse_ex_date))).any())
        recent = sig_date is not None and (sig_date - r.gap_date).days <= 380
        in_sig = bool(len(sig) and r.symbol in set(sig["symbol"]))
        state = (f"in force today: listed factor {r.listed_factor:.4f} {'APPLIED' if in_ohlcv else 'not applied'} in the official `ohlcv` series (NSE-source-confirmed method); "
                 f"{'present' if in_registry else 'NOT present'} in the governed registry `sbadj_v1`")
        impact = (f"; if excluded, history before {r.gap_date.date()} would show a false {(1 - r.listed_factor):.0%} cliff"
                  + (" inside the lookback of today's signal" if recent and in_sig else "")) if in_ohlcv else ""
        rows.append({"symbol": r.symbol, "gap_date": r.gap_date.date(), "recommended": dec, "reason": why, "flags": " | ".join(flags), "in_official_ohlcv": in_ohlcv, "in_registry": in_registry, "decision": ""})
        head.append(f"| {n} | {r.symbol} | {str(r.nse_subject)[:32]} | {r.listed_factor:.4f} | {pd.Timestamp(r.nse_ex_date).date()} | {r.observed_ratio:.4f} | {r.residual_overnight_after_listed_factor:+.1%} | "
                    f"{r.residual_in_sigma:+.1f} | {'none' if not flags else len(flags)} | **{dec}** |")
        blk = [f"### {n}. {r.symbol} | {r.nse_subject} | listed x{r.listed_factor:.4f} | ex-date {pd.Timestamp(r.nse_ex_date).date()} | ISIN {r.isin}", "",
               f"- **Gap:** first bar on/after ex-date {r.gap_date.date()}; observed x{r.observed_ratio:.4f} (listed x{r.listed_factor:.4f}, difference {r.factor_error:.1%}); prior close {r.prev_close:.2f} -> gap open {r.gap_open:.2f}, gap close {r.gap_close:.2f}",
               f"- **Residual after the listed factor:** overnight {r.residual_overnight_after_listed_factor:+.2%} ({r.residual_in_sigma:+.1f} sigma of the 60-day daily sigma {sigma:.2%}); close-to-close {r.residual_close_after_listed_factor:+.2%}; "
               f"gap-bar volume {gv:.1f}x the 60-session median (share-count adjusted); next-5-session return {r.next5d_return:+.1%}; largest move in the next 3 bars {jump:.1f} sigma",
               f"- **Flags:** " + ("none" if not flags else "; ".join(flags)),
               f"- **{state}{impact}**", f"- **Recommendation: `{dec}`** - {why}", "",
               "| session | date | open | close | close x listed factor | return after factor | volume x pre-event median (share-count adjusted) |", "|---|---|---|---|---|---|---|"]
        w2 = g.iloc[max(0, i - 5): i + 6].copy()
        w2["adj"] = np.where(w2["date"] < r.gap_date, w2["close"] * r.listed_factor, w2["close"])
        w2["ret"] = w2["adj"].pct_change()
        for k, x in zip(range(max(0, i - 5) - i, max(0, i - 5) - i + len(w2)), w2.itertuples()):
            tag = "gap bar" if k == 0 else (f"{k:+d}" if k else "0")
            vx = (x.volume * (r.listed_factor if x.date >= r.gap_date else 1.0)) / med_vol if med_vol and med_vol == med_vol else float("nan")
            blk.append(f"| {tag} | {x.date.date()} | {x.open:.2f} | {x.close:.2f} | {x.adj:.2f} | {'' if pd.isna(x.ret) else f'{x.ret:+.1%}'} | {vx:.1f} |")
        body += [""] + blk
    counts = pd.Series([x["recommended"] for x in rows]).value_counts().to_dict()
    head += ["", f"Recommendations: " + ", ".join(f"{k} {v}" for k, v in counts.items()) + ". Rules: `confirm` needs no flag, residual within 2.5 sigma, gap-bar volume at least 0.5x median and no jump above 3 sigma in the next 3 bars; "
             f"`reject` is a demerger / other action within 10 days or a residual above 4 sigma; everything else is `needs_more_evidence`. Decision column: `decision` in `{stem}.csv` is blank for you."]
    (OUT / f"{stem}.md").write_text("\n".join(head + body), encoding="utf-8")
    pd.DataFrame(rows).to_csv(OUT / f"{stem}.csv", index=False)
    print("\n".join(head))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
