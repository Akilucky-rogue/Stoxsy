"""Full-universe data-quality report for a feature snapshot + the official price series behind it.

Run immediately after a snapshot is promoted; review BEFORE any model training.

    python scripts/snapshot_dq_report.py                # current snapshot
    python scripts/snapshot_dq_report.py --snapshot ID

Sections: manifest/provenance; coverage vs the point-in-time universe (per year); per-feature NaN share and
degenerate features; extreme values; per-symbol continuity (missing sessions, stale/flat runs); residual price cliffs
in the OFFICIAL ohlcv (>= 18% close-to-close) split into explained / excluded / UNEXPLAINED; corporate-action
policy counts. Writes Markdown + JSON to D:\\Stoxsy\\data\\reports\\data_quality\\.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

OUT = Path(r"D:\Stoxsy\data\reports\data_quality")
CLIFF = 0.18


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None)
    ap.add_argument("--factor-version", default=None, help="ffd_v2 (default) or ffd_v2s")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    from qsde.db.connection import read_sql
    from qsde.factors.versions import FFD_VERSION
    from qsde.features.store import current_snapshots, read_features
    from qsde.ml.data import OFFICIAL, label_exclusion_events, load_prices

    fv = args.factor_version or FFD_VERSION
    snaps = [x for x in current_snapshots(fv) if "full" in x.universe_version]
    snap = next((s for s in snaps if s.snapshot_id == args.snapshot), None) if args.snapshot else (snaps[-1] if snaps else None)
    if snap is None:
        print("no snapshot found")
        return 1
    m = snap.manifest
    f = read_features(fv, snapshot_ids=[snap.snapshot_id])
    f["as_of_date"] = pd.to_datetime(f["as_of_date"])
    cols = [c for c in f.columns if c not in ("symbol", "as_of_date", "known_at")]
    rep: dict = {"snapshot_id": snap.snapshot_id, "dataset_hash": snap.dataset_hash,
                 "universe_version": snap.universe_version, "factor_version": snap.factor_version}

    # ── coverage vs the PIT universe
    uni = read_sql("SELECT date, symbol FROM universe_pit WHERE adv_cr >= :a AND method = :m",
                   params={"a": m["params"].get("min_adv_cr", 10.0), "m": m["params"].get("universe_method", "adv60_top500_v1")})
    uni["date"] = pd.to_datetime(uni["date"])
    uni = uni[uni["symbol"].isin(f["symbol"].unique())]
    have = uni.merge(f[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"}), on=["symbol", "date"], how="left", indicator=True)
    cov_year = (have.assign(year=have["date"].dt.year).groupby("year")["_merge"]
                .apply(lambda s: float((s == "both").mean())).round(4))
    extra_rows = int(len(f) - (have["_merge"] == "both").sum())
    rep["coverage"] = {"universe_symbol_days": int(len(uni)), "present": int((have["_merge"] == "both").sum()),
                       "share": float((have["_merge"] == "both").mean()), "rows_outside_universe": extra_rows,
                       "by_year": cov_year.to_dict()}

    # ── per-feature quality
    v = f[cols]
    nan_share = v.isna().mean()
    std = v.std()
    const = [c for c in cols if not np.isfinite(std[c]) or std[c] == 0]
    q = v.quantile([0.001, 0.999])
    ext = {}
    for c in cols:
        s = std[c]
        if np.isfinite(s) and s > 0:
            ext[c] = float(max(abs(v[c].max() - v[c].median()), abs(v[c].min() - v[c].median())) / s)
    rep["features"] = {"n": len(cols), "mean_nan_share": float(nan_share.mean()),
                       "worst_nan": nan_share.sort_values(ascending=False).head(10).round(4).to_dict(),
                       "constant_or_undefined": const,
                       "most_extreme_in_sigma": dict(sorted(ext.items(), key=lambda kv: -kv[1])[:10]),
                       "inf_cells": int(np.isinf(v.to_numpy(dtype="float64")).sum())}

    # ── per-symbol continuity of the feature rows
    g = f.sort_values(["symbol", "as_of_date"]).groupby("symbol")["as_of_date"]
    cal = pd.DatetimeIndex(sorted(f["as_of_date"].unique()))
    pos = pd.Series(np.arange(len(cal)), index=cal)
    f["_p"] = pos.reindex(f["as_of_date"]).to_numpy()
    gaps = f.sort_values(["symbol", "_p"]).groupby("symbol")["_p"].diff().dropna()
    big_gap = f.sort_values(["symbol", "_p"]).assign(gap=lambda d: d.groupby("symbol")["_p"].diff())
    big_gap = big_gap[big_gap["gap"] > 5].copy()
    big_gap["prev_date"] = f.sort_values(["symbol", "_p"]).groupby("symbol")["as_of_date"].shift(1).reindex(big_gap.index)

    # ── official price series: residual cliffs
    px = load_prices(sorted(f["symbol"].unique()), OFFICIAL)
    px = px.sort_values(["symbol", "date"])
    # gaps in the feature rows: universe exit/re-entry (bars exist in between) vs missing bars (no trading / suspension)
    bars_by_sym = {k: v["date"].to_numpy() for k, v in px.groupby("symbol")}
    n_bars = [int(((bars_by_sym.get(r.symbol, np.array([], dtype="datetime64[ns]")) > np.datetime64(r.prev_date)) &
                   (bars_by_sym.get(r.symbol, np.array([], dtype="datetime64[ns]")) < np.datetime64(r.as_of_date))).sum())
              for r in big_gap.itertuples()]
    big_gap["bars_in_gap"] = n_bars
    exits = int((big_gap["bars_in_gap"] >= 3).sum())
    rep["continuity"] = {"symbols": int(f["symbol"].nunique()), "rows_per_symbol_median": float(f.groupby("symbol").size().median()),
                         "gaps_gt_5_sessions": int(len(big_gap)), "symbols_with_gaps": int(big_gap["symbol"].nunique()),
                         "gaps_universe_exit_reentry (bars exist, symbol left the top-500)": exits,
                         "gaps_missing_bars (no/low trading in the gap)": int(len(big_gap) - exits)}
    px["ret"] = px.groupby("symbol")["close"].pct_change()
    cliffs = px[px["ret"].abs() >= CLIFF][["symbol", "date", "ret"]].copy()
    ev = read_sql("SELECT symbol, ex_date, kind, source, applied, confidence FROM corp_action_events")
    ev["ex_date"] = pd.to_datetime(ev["ex_date"])
    excl = label_exclusion_events(sorted(f["symbol"].unique()), OFFICIAL)
    excl["ex_date"] = pd.to_datetime(excl["ex_date"])

    def tag(r):
        near = ev[(ev["symbol"] == r.symbol) & ((ev["ex_date"] - r.date).abs() <= pd.Timedelta(days=5))]
        if near.empty:
            return "no_event_nearby"
        if near["applied"].any():
            # +-20% on/near a correctly adjusted ex-date is a circuit-limit day (checked: ADANIPOWER 2025-09-22 raw open/prev
            # close 0.209 = the 1:5 split, close/open 1.149), not a failed adjustment
            return ("circuit_limit_day_near_adjusted_event" if abs(abs(r.ret) - 0.20) < 0.02
                    else "official_event_applied_but_cliff_remains (review)")
        e2 = excl[(excl["symbol"] == r.symbol) & ((excl["ex_date"] - r.date).abs() <= pd.Timedelta(days=5))]
        return "excluded_from_labels" if len(e2) else "event_not_applied_not_excluded"

    if len(cliffs):
        cliffs["status"] = [tag(r) for r in cliffs.itertuples()]
    rep["official_price_cliffs"] = {"threshold": CLIFF, "count": int(len(cliffs)),
                                    "by_status": cliffs["status"].value_counts().to_dict() if len(cliffs) else {}}
    rep["corporate_action_policy"] = m.get("corporate_action_source", {})
    rep["excluded_event_count"] = m.get("excluded_event_count")

    # ── write
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = f"snapshot_dq_{fv}_{snap.snapshot_id}"
    (out / f"{stem}.json").write_text(json.dumps(rep, indent=2, default=str))
    if len(cliffs):
        cliffs.sort_values(["status", "symbol", "date"]).to_csv(out / f"{stem}_price_cliffs.csv", index=False)
    md = [f"# Snapshot data-quality report - {snap.snapshot_id}", "",
          f"- dataset_hash `{snap.dataset_hash[:16]}`  universe `{snap.universe_version}`  factor `{snap.factor_version}`",
          f"- rows {snap.n_rows:,}  symbols {snap.n_symbols:,}  features {len(cols)}  dates {snap.date_min} .. {snap.date_max}",
          f"- cells {m.get('cells', 0):,}  NaN cells {m.get('nan_cells', 0):,}  "
          f"({100 * m.get('nan_cells', 0) / max(m.get('cells', 1), 1):.1f}% - warm-up and thin history, preserved not filled)",
          f"- price policy `{m.get('price_policy')}`; corporate-action source `{rep['corporate_action_policy'].get('file')}` "
          f"sha256 `{str(rep['corporate_action_policy'].get('sha256'))[:12]}`; excluded events {m.get('excluded_event_count')}",
          "", "## Coverage of the point-in-time universe", "",
          f"{rep['coverage']['present']:,} of {rep['coverage']['universe_symbol_days']:,} universe symbol-days "
          f"({100 * rep['coverage']['share']:.2f}%); rows outside the universe: {rep['coverage']['rows_outside_universe']:,}", "",
          "| year | coverage |", "|---|---|"] + [f"| {y} | {c:.4f} |" for y, c in rep["coverage"]["by_year"].items()]
    md += ["", "## Features", "",
           f"mean NaN share {rep['features']['mean_nan_share']:.3f}; constant/undefined: {rep['features']['constant_or_undefined'] or 'none'}; "
           f"inf cells {rep['features']['inf_cells']}", "", "Worst NaN shares: " +
           ", ".join(f"{k} {v:.2f}" for k, v in rep["features"]["worst_nan"].items()), "",
           "Most extreme (max |x - median| / std): " +
           ", ".join(f"{k} {v:.0f}σ" for k, v in rep["features"]["most_extreme_in_sigma"].items()),
           "", "## Continuity", "", json.dumps(rep["continuity"]), "",
           f"## Residual price cliffs in the OFFICIAL series (|1d return| >= {CLIFF:.0%})", "",
           f"{rep['official_price_cliffs']['count']} cliffs: {rep['official_price_cliffs']['by_status']}", "",
           "`no_event_nearby` is dominated by genuine +-20% circuit-limit days in small caps and known crashes (INFIBEAM 2018, YESBANK 2020, DHFL); "
           "a sample is NOT a bad-print finding. Review only `official_event_applied_but_cliff_remains` and `event_not_applied_not_excluded`. "
           "`excluded_from_labels` are handled by the label builder."]
    (out / f"{stem}.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md[:30]))
    print(f"\nreport: {out / (stem + '.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
