"""Before/after report for the bhavcopy backfill (2026-05-01 .. 2026-10-16): universe, snapshot and data-quality changes.

    python scripts/backfill_report.py [--old <ffd_v2 snapshot id>] [--new <ffd_v2 snapshot id>]
Writes D:\\Stoxsy\\data\\reports\\backfill\\backfill_before_after.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "backfill")
BEFORE = data_path("reports", "universe_pit_before_backfill.parquet")
CUTOFF = pd.Timestamp("2026-04-30")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="20261018T153203Z-8615b43353")
    ap.add_argument("--new", default=None)
    args = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.factors.versions import FFD_VERSION
    from qsde.features.store import current_snapshots, read_features

    L = ["# Bhavcopy backfill: before / after", ""]
    # ── universe
    ub = pd.read_parquet(BEFORE)
    ub["date"] = pd.to_datetime(ub["date"])
    ua = read_sql("SELECT date, symbol, rank, adv_cr FROM universe_pit WHERE method = 'adv60_top500_v1' AND date >= '2026-03-01'")
    ua["date"] = pd.to_datetime(ua["date"])
    ov = ub.merge(ua, on=["date", "symbol"], suffixes=("_b", "_a"))
    changed_old = int(((ov["rank_b"] != ov["rank_a"]) | (ov["adv_cr_b"].sub(ov["adv_cr_a"]).abs() > 1e-6)).sum())
    L += ["## Universe (point-in-time)", "",
          f"- rows through {CUTOFF.date()} that changed after the backfill: **{changed_old}** of {len(ov):,} (must be 0: ranks use only trailing data)",
          f"- universe now runs to {ua['date'].max().date()} (was {ub['date'].max().date()}); 500 members per session"]
    new = ua[ua["date"] > CUTOFF]
    ever_before = set(read_sql("SELECT DISTINCT symbol FROM universe_pit WHERE date <= '2026-04-30'")["symbol"])
    entrants = sorted(set(new["symbol"]) - ever_before)
    last_b, last_a = ub[ub["date"] == CUTOFF], ua[ua["date"] == ua["date"].max()]
    kept = len(set(last_b["symbol"]) & set(last_a["symbol"]))
    j = last_b.merge(last_a, on="symbol", suffixes=("_b", "_a"))
    L += [f"- symbols that entered the universe for the FIRST time after {CUTOFF.date()}: **{len(entrants)}**"
          + (f" ({', '.join(entrants[:25])}{' ...' if len(entrants) > 25 else ''})" if entrants else ""),
          f"- turnover: {kept} of 500 members on {CUTOFF.date()} are still members on {ua['date'].max().date()} "
          f"(liquidity-rank Spearman among survivors {j['rank_b'].corr(j['rank_a'], method='spearman'):.2f})", ""]

    # ── raw data anomalies
    d = read_sql("SELECT date, count(*) n, count(*) FILTER (WHERE series = 'EQ') eq, "
                 "count(*) FILTER (WHERE high < low OR close <= 0 OR open <= 0 OR close > high * 1.0001 OR close < low * 0.9999) bad, "
                 "count(*) FILTER (WHERE volume = 0) zero_vol FROM eod_raw WHERE date > '2026-04-30' GROUP BY 1 ORDER BY 1")
    L += ["## Raw data (116 new sessions)", "",
          f"- sessions {len(d)}, rows {int(d['n'].sum()):,}; rows/session min {int(d['n'].min())}, median {int(d['n'].median())}, max {int(d['n'].max())}",
          f"- OHLC-inconsistent rows: {int(d['bad'].sum())}; zero-volume rows: {int(d['zero_vol'].sum()):,} "
          f"({d['zero_vol'].sum() / d['n'].sum():.2%})",
          "- non-session weekdays (NSE served the previous day's file): 2026-05-01, 05-28, 06-26, 09-14, 10-02 (all NSE holidays)", ""]
    # unexplained cliffs in the new period (the official series has no source for post-April actions except owner approvals)
    px = read_sql("SELECT symbol, date, close FROM ohlcv WHERE date > '2026-04-24' ORDER BY symbol, date")
    px["ret"] = px.groupby("symbol")["close"].pct_change()
    cl = px[(px["date"] > CUTOFF.date()) & (px["ret"].abs() >= 0.18)].copy()
    ev = read_sql("SELECT symbol, ex_date FROM corp_action_events WHERE ex_date > '2026-04-24'")
    ca = read_sql("SELECT symbol, ex_date, category, subject FROM nse_corporate_actions WHERE ex_date > '2026-04-24' "
                  "AND category IN ('SPLIT_BONUS','CONSOLIDATION','DEMERGER')")
    ca["ex_date"] = pd.to_datetime(ca["ex_date"])
    cl["date"] = pd.to_datetime(cl["date"])

    def explain(r):
        n = ca[(ca["symbol"] == r.symbol) & ((ca["ex_date"] - r.date).abs() <= pd.Timedelta(days=4))]
        return n.iloc[0].subject[:50] if len(n) else "none"
    cl["nse_action_nearby"] = [explain(r) for r in cl.itertuples()]
    L += [f"- one-day moves of 18% or more in the new period: **{len(cl)}**; with an NSE split/bonus/demerger record within 4 days: "
          f"{int((cl['nse_action_nearby'] != 'none').sum())} "
          "(these are NOT applied to prices: the new NSE actions are candidates until individually reconciled)"]
    for r in cl[cl["nse_action_nearby"] != "none"].head(12).itertuples():
        L.append(f"  - {r.symbol} {r.date.date()} {r.ret:+.0%}: {r.nse_action_nearby}")
    L.append("")

    # ── snapshots
    snaps = [s for s in current_snapshots(FFD_VERSION) if "full" in s.universe_version]
    new_snap = next((s for s in snaps if s.snapshot_id == args.new), snaps[-1])
    A = read_features(FFD_VERSION, snapshot_ids=[args.old]).drop(columns=["known_at"])
    B = read_features(FFD_VERSION, snapshot_ids=[new_snap.snapshot_id]).drop(columns=["known_at"])
    for f in (A, B):
        f["as_of_date"] = pd.to_datetime(f["as_of_date"])
    cols = [c for c in A.columns if c not in ("symbol", "as_of_date")]
    A, B = A.sort_values(["symbol", "as_of_date"]).reset_index(drop=True), B.sort_values(["symbol", "as_of_date"]).reset_index(drop=True)
    old_part = B[B["as_of_date"] <= CUTOFF].reset_index(drop=True)
    same_keys = A[["symbol", "as_of_date"]].equals(old_part[["symbol", "as_of_date"]])
    a, b = A[cols].to_numpy(float), old_part[cols].to_numpy(float) if same_keys else (None, None)
    L += ["## Feature snapshot (ffd_v2)", "",
          f"- old `{args.old}`: {len(A):,} rows to {A['as_of_date'].max().date()}; successor `{new_snap.snapshot_id}` (hash `{new_snap.dataset_hash[:12]}`): "
          f"{len(B):,} rows to {B['as_of_date'].max().date()}, {B['symbol'].nunique()} symbols",
          f"- new rows: {int((B['as_of_date'] > CUTOFF).sum()):,} (new symbol-days after {CUTOFF.date()})",
          f"- the part up to {CUTOFF.date()} has identical keys: **{same_keys}**"]
    if same_keys:
        ch = ~np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)
        L.append(f"- feature cells in that part that CHANGED: **{int(ch.sum()):,}** in {int(ch.any(axis=1).sum()):,} rows "
                 f"({len(set(A.loc[ch.any(axis=1), 'symbol']))} symbols): from corporate-action rebuild changes, not from new bars")
    nb = B[B["as_of_date"] > CUTOFF]
    nan_share = nb[cols].isna().mean().mean()
    first = B.groupby("symbol")["as_of_date"].min()
    newlist = first[first > CUTOFF].index
    warm = nb[nb["symbol"].isin(newlist)][cols].isna().mean().mean() if len(newlist) else float("nan")
    L += [f"- NaN share in the new rows: {nan_share:.2%} (old snapshot {A[cols].isna().mean().mean():.2%}); symbols with their FIRST feature row "
          f"after the cutoff: {len(newlist)} (their new warm-up NaN share {warm:.0%})", ""]
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "backfill_before_after.md").write_text("\n".join(L), encoding="utf-8")
    cl.to_csv(OUT / "new_period_large_moves.csv", index=False)
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
