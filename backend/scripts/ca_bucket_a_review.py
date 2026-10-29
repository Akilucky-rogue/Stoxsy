"""Evidence worksheet for triage bucket A: status `official_match_price_mismatch`.

The NSE record matches (type, ratio, ex-date, ISIN) but the observed gap differs from the listed factor by 3-10%, outside the hard
tolerance. This does NOT widen the tolerance and approves nothing. For every event it assembles what a reviewer needs to decide that
event on its own: the NSE record, the price series around the gap bar, whether the name was halted/thin/renamed/in a demerger or had a
neighbouring action, the proposed factor and its source, and a before/after continuity check (is the remaining move after applying the
LISTED factor an ordinary move for this stock?). A blank `decision` column is for the owner.

    python scripts/ca_bucket_a_review.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")
STATUS = "official_match_price_mismatch"


def main() -> int:
    from qsde.db.connection import read_sql
    from qsde.ingestion import nse_eod

    tri = pd.read_csv(OUT / "triage_price_only_splits.csv")
    a = tri[tri["bucket"] == "A"].copy()
    a["gap_date"] = pd.to_datetime(a["gap_date"])
    syms = sorted(a["symbol"].unique())
    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE' AND symbol = ANY(:s)",
                   params={"s": syms})
    raw["date"] = pd.to_datetime(raw["date"])
    bars = {s: g.sort_values("date").reset_index(drop=True) for s, g in nse_eod.pick_primary_series(raw).groupby("symbol")}
    nse = read_sql("""SELECT symbol, category, subject, ex_date, record_date, isin, face_value, factor FROM nse_corporate_actions
                      WHERE symbol = ANY(:s)""", params={"s": syms})
    nse["ex_date"] = pd.to_datetime(nse["ex_date"])
    chg = read_sql("SELECT old_symbol, new_symbol, effective_date FROM symbol_changes WHERE verification_status = 'official'")
    chg["effective_date"] = pd.to_datetime(chg["effective_date"])
    uni = read_sql("SELECT date, symbol, rank FROM universe_pit WHERE method = 'adv60_top500_v1' AND symbol = ANY(:s)", params={"s": syms})
    uni["date"] = pd.to_datetime(uni["date"])

    rows = []
    for r in a.itertuples():
        g = bars.get(r.symbol)
        rec = {"symbol": r.symbol, "gap_date": r.gap_date.date(), "status": STATUS, "nse_subject": r.subject,
               "nse_ex_date": r.nse_ex_date, "isin": r.isin, "listed_factor": r.listed_factor, "applied_in_ohlcv": r.applied_factor,
               "observed_ratio": r.observed_ratio, "factor_error": r.factor_error}
        if g is None or g.empty:
            rec["flags"] = "no bars"
            rows.append(rec)
            continue
        i = int(g.index[g["date"] >= r.gap_date][0]) if (g["date"] >= r.gap_date).any() else None
        if i is None or i < 1:
            rec["flags"] = "no gap bar"
            rows.append(rec)
            continue
        prev, bar = g.iloc[i - 1], g.iloc[i]
        base = g.iloc[max(0, i - 61): i - 1]
        vol = base["close"].pct_change().std()
        med_v = base["volume"].median() if len(base) else np.nan
        adj_move = (bar["open"] / prev["close"]) / r.listed_factor - 1.0                    # overnight move left after the LISTED factor
        adj_close_move = (bar["close"] / prev["close"]) / r.listed_factor - 1.0
        nxt = g.iloc[i + 1: i + 6]
        post = (nxt["close"].iloc[-1] / bar["close"] - 1.0) if len(nxt) else np.nan
        # neighbouring actions / renames / demerger / circuit
        near = nse[(nse["symbol"] == r.symbol) & ((nse["ex_date"] - r.gap_date).abs() <= pd.Timedelta(days=10))]
        other = near[~near["category"].isin(["SPLIT_BONUS", "CONSOLIDATION"])]
        renamed = chg[((chg["old_symbol"] == r.symbol) | (chg["new_symbol"] == r.symbol)) &
                      ((chg["effective_date"] - r.gap_date).abs() <= pd.Timedelta(days=45))]
        u = uni[(uni["symbol"] == r.symbol) & ((uni["date"] - r.gap_date).abs() <= pd.Timedelta(days=60))]
        flags = []
        gapdays = (bar["date"] - prev["date"]).days
        if gapdays > 5:
            flags.append(f"{gapdays}-day hole before the gap bar")
        if pd.notna(med_v) and med_v > 0 and bar["volume"] < 0.2 * med_v:
            flags.append("thin volume on the gap bar")
        if (other["category"] == "DEMERGER").any():
            flags.append("demerger within 10 days")
        if len(other):
            flags.append("other action within 10 days: " + ", ".join(sorted(set(other["category"].astype(str)))))
        if len(renamed):
            flags.append("official rename within 45 days")
        if pd.notna(vol) and vol > 0 and abs(adj_move) > 3 * vol:
            flags.append(f"residual move {adj_move:+.1%} exceeds 3 daily sigma ({vol:.1%})")
        rec.update({"prev_date": prev["date"].date(), "prev_close": prev["close"], "gap_open": bar["open"], "gap_close": bar["close"],
                    "residual_overnight_after_listed_factor": adj_move, "residual_close_after_listed_factor": adj_close_move,
                    "daily_sigma_60d": vol, "residual_in_sigma": (adj_move / vol) if vol else np.nan, "next5d_return": post,
                    "gap_volume_x_median": (bar["volume"] / med_v) if med_v else np.nan,
                    "in_universe_within_60d": bool(len(u)), "best_universe_rank": int(u["rank"].min()) if len(u) else np.nan,
                    "proposed_factor": r.listed_factor, "proposed_factor_source": f"NSE corporate-actions record ({r.subject})",
                    "flags": "; ".join(flags), "decision": ""})
        rows.append(rec)
    df = pd.DataFrame(rows)
    df["clean"] = df["flags"].fillna("").eq("")
    df = df.sort_values(["in_universe_within_60d", "clean", "gap_date"], ascending=[False, False, True])
    df.to_csv(OUT / "review_bucket_a_official_match_price_mismatch.csv", index=False)
    n_in, n_clean = int(df["in_universe_within_60d"].sum()), int(df["clean"].sum())
    L = [f"# Bucket A review worksheet - status `{STATUS}` ({len(df)} events)", "",
         "No tolerance was widened and nothing is approved. Each event is decided on its own evidence by the owner.", "",
         f"- events whose symbol was in the point-in-time universe within 60 days of the event (reviewed first): **{n_in}**",
         f"- events with no flag (no hole, no thin bar, no neighbouring action or rename, residual within 3 sigma): **{n_clean}**",
         f"- median residual overnight move after applying the LISTED factor: {df['residual_overnight_after_listed_factor'].median():+.2%}; "
         f"median daily sigma {df['daily_sigma_60d'].median():.2%}", "",
         "| symbol | gap bar | NSE subject | listed x | observed x | residual after listed | in sigma | in universe | flags |",
         "|---|---|---|---|---|---|---|---|---|"]
    for r in df.itertuples():
        L.append(f"| {r.symbol} | {r.gap_date} | {str(r.nse_subject)[:38]} | {r.listed_factor:.3f} | {r.observed_ratio:.3f} | "
                 f"{r.residual_overnight_after_listed_factor:+.1%} | {r.residual_in_sigma:+.1f} | {'yes' if r.in_universe_within_60d else 'no'} | {r.flags} |"
                 if pd.notna(getattr(r, 'residual_overnight_after_listed_factor', np.nan)) else f"| {r.symbol} | {r.gap_date} | | | | | | | {r.flags} |")
    (OUT / "review_bucket_a_official_match_price_mismatch.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
