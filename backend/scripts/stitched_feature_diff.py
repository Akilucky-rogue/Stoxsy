"""Before/after feature diff: per-symbol features (ffd_v2) vs features computed on series stitched across official renames (ffd_v2s).

    python scripts/stitched_feature_diff.py [--a <snapshot id>] [--b <snapshot id>]

Reports which symbols and feature families changed, how many warm-up NaNs the stitching removed, and a price-continuity check at
every hand-off (a fake cliff at a join would poison the stitched features). Neither snapshot is modified.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "stitching")
FAMILIES = [("returns/momentum", ("mom", "roc", "ret", "trend")), ("volatility", ("vol", "atr", "std", "range")),
            ("drawdown/range", ("dd", "drawdown", "52w", "high", "low")), ("distance", ("dist", "ema", "sma", "vwap")),
            ("volume/flow", ("obv", "cmf", "mfi", "volume", "flow"))]


def family(col: str) -> str:
    c = col.lower()
    for name, keys in FAMILIES:
        if any(k in c for k in keys):
            return name
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default=None, help="ffd_v2 snapshot id (default: latest full)")
    ap.add_argument("--b", default=None, help="ffd_v2s snapshot id (default: latest full)")
    args = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.factors.versions import FFD_STITCHED_VERSION, FFD_VERSION
    from qsde.features.store import current_snapshots, read_features

    def pick(version, sid):
        snaps = [s for s in current_snapshots(version) if "full" in s.universe_version]
        if sid:
            snaps = [s for s in snaps if s.snapshot_id == sid]
        return snaps[-1]

    sa, sb = pick(FFD_VERSION, args.a), pick(FFD_STITCHED_VERSION, args.b)
    A = read_features(FFD_VERSION, snapshot_ids=[sa.snapshot_id]).drop(columns=["known_at"])
    B = read_features(FFD_STITCHED_VERSION, snapshot_ids=[sb.snapshot_id]).drop(columns=["known_at"])
    key = ["symbol", "as_of_date"]
    A, B = A.sort_values(key).reset_index(drop=True), B.sort_values(key).reset_index(drop=True)
    same_keys = A[key].equals(B[key])
    cols = [c for c in A.columns if c not in key]
    iv = read_sql("SELECT security_id, symbol FROM security_master")
    stitched_syms = set(iv["symbol"])
    in_st = A["symbol"].isin(stitched_syms).to_numpy()

    a, b = A[cols].to_numpy(float), B[cols].to_numpy(float)
    changed = ~np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)
    row_changed = changed.any(axis=1)
    nan_before, nan_after = np.isnan(a), np.isnan(b)
    fixed = nan_before & ~nan_after
    broke = ~nan_before & nan_after

    L = ["# Stitched-feature diff", "",
         f"- A (per symbol)  `{sa.snapshot_id}` factor `{FFD_VERSION}`  hash `{sa.dataset_hash[:12]}`",
         f"- B (stitched)    `{sb.snapshot_id}` factor `{FFD_STITCHED_VERSION}`  hash `{sb.dataset_hash[:12]}`",
         f"- identical row keys: **{same_keys}** ({len(A):,} vs {len(B):,} rows)", ""]
    if not same_keys:
        L.append("**Row keys differ** - the diff below is on the intersection only.")
        m = A[key].merge(B[key], on=key)
        A, B = A.merge(m, on=key), B.merge(m, on=key)
        a, b = A[cols].to_numpy(float), B[cols].to_numpy(float)
        changed = ~np.isclose(a, b, rtol=1e-9, atol=1e-12, equal_nan=True)
        row_changed, nan_before, nan_after = changed.any(axis=1), np.isnan(a), np.isnan(b)
        fixed, broke = nan_before & ~nan_after, ~nan_before & nan_after
        in_st = A["symbol"].isin(stitched_syms).to_numpy()
    L += [f"- rows with any changed feature: **{int(row_changed.sum()):,}** ({row_changed.mean():.2%}); of those in stitched securities: "
          f"{int((row_changed & in_st).sum()):,}; **outside stitched securities: {int((row_changed & ~in_st).sum()):,}** (must be 0)",
          f"- changed cells: {int(changed.sum()):,} of {changed.size:,}",
          f"- warm-up NaN cells removed by stitching: **{int(fixed.sum()):,}**; NaN cells introduced: **{int(broke.sum()):,}** (should be ~0)", ""]
    fam = pd.DataFrame({"feature": cols, "family": [family(c) for c in cols], "changed_cells": changed.sum(0),
                        "nan_removed": fixed.sum(0), "nan_added": broke.sum(0)})
    fam["mean_abs_change_in_sd"] = [float(np.nanmean(np.abs(a[changed[:, j], j] - b[changed[:, j], j])) / (np.nanstd(a[:, j]) or 1.0))
                                    if changed[:, j].any() else 0.0 for j in range(len(cols))]
    L += ["## By feature family", "", "| family | features changed | changed cells | NaN removed | NaN added |", "|---|---|---|---|---|"]
    for f, g in fam.groupby("family"):
        L.append(f"| {f} | {int((g.changed_cells > 0).sum())}/{len(g)} | {int(g.changed_cells.sum()):,} | {int(g.nan_removed.sum()):,} | {int(g.nan_added.sum()):,} |")
    L += ["", "## Most affected features", "", "| feature | changed cells | NaN removed | mean |change| (sd of A) |", "|---|---|---|---|"]
    for r in fam.sort_values("changed_cells", ascending=False).head(12).itertuples():
        L.append(f"| {r.feature} | {r.changed_cells:,} | {r.nan_removed:,} | {r.mean_abs_change_in_sd:.3f} |")

    per_sym = (pd.DataFrame({"symbol": A["symbol"], "changed_rows": row_changed, "nan_removed": fixed.sum(1)})
               .groupby("symbol").sum().query("changed_rows > 0").sort_values("changed_rows", ascending=False))
    L += ["", f"## Symbols with changed features: {len(per_sym)}", "", "| symbol | rows changed | NaN cells removed |", "|---|---|---|"]
    for s, r in per_sym.head(25).iterrows():
        L.append(f"| {s} | {int(r.changed_rows):,} | {int(r.nan_removed):,} |")

    # price continuity at every hand-off
    ho = read_sql("""SELECT o.security_id, o.symbol AS old_symbol, n.symbol AS new_symbol, n.valid_from AS handoff
                     FROM security_master o JOIN security_master n ON n.security_id = o.security_id AND n.valid_from = o.valid_to + 1""")
    bad = []
    for r in ho.itertuples():
        px = read_sql("SELECT symbol, date, open, close FROM ohlcv WHERE symbol IN (:a, :b) AND date BETWEEN CAST(:d AS DATE) - 40 AND CAST(:d AS DATE) + 40",
                      params={"a": r.old_symbol, "b": r.new_symbol, "d": str(r.handoff)})
        o, n = px[px.symbol == r.old_symbol].sort_values("date"), px[px.symbol == r.new_symbol].sort_values("date")
        if len(o) and len(n):
            jump = float(n.iloc[0]["open"] / o.iloc[-1]["close"] - 1)
            if abs(jump) > 0.20:
                bad.append((r.security_id, r.old_symbol, r.new_symbol, str(r.handoff), jump))
    L += ["", f"## Hand-off price continuity: {len(ho)} hand-offs, {len(bad)} with an overnight jump over 20%", ""]
    for b_ in bad[:20]:
        L.append(f"- {b_[0]}: {b_[1]} -> {b_[2]} on {b_[3]}: {b_[4]:+.1%} (check for an unadjusted event around the rename)")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "stitched_feature_diff.md").write_text("\n".join(L), encoding="utf-8")
    fam.to_csv(OUT / "stitched_feature_diff_by_feature.csv", index=False)
    per_sym.to_csv(OUT / "stitched_feature_diff_by_symbol.csv")
    print("\n".join(L[:14]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
