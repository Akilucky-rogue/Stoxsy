"""Run the full pipeline (store -> labels -> CPCV model -> harness -> model card) on a feature snapshot.

On the 46-symbol pilot this is a PLUMBING check only: the harness stamps the result
PLUMBING_ONLY and the card carries no verdict.

    python scripts/run_harness_pilot.py --horizon swing_5d
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
log = logging.getLogger("harness_pilot")


def lgbm_fit_predict(n_estimators: int = 150):
    import lightgbm as lgb

    def fp(Xtr, ytr, wtr, Xte):
        m = lgb.LGBMRegressor(n_estimators=n_estimators, num_leaves=15, learning_rate=0.05, min_child_samples=200,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.7, reg_lambda=5.0,
                              n_jobs=8, verbose=-1, random_state=0)
        m.fit(Xtr, ytr, sample_weight=wtr)
        return m.predict(Xte)
    return fp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="swing_5d")
    ap.add_argument("--groups", type=int, default=6)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--ledger-dir", default=r"D:\Stoxsy\data\ledger_pilot")
    ap.add_argument("--out", default=r"D:\Stoxsy\data\reports\model_cards")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    import numpy as np
    import pandas as pd

    from qsde.db.connection import read_sql
    from qsde.factors.versions import FFD_VERSION
    from qsde.features.store import current_snapshots, git_sha, read_features
    from qsde.ml import cv as cvmod
    from qsde.ml.eval import evaluate_scores, leak_check
    from qsde.ml.labels import make_labels
    from qsde.ml.ledger import TrialKey, record_trial, trial_count, trial_sharpes
    from qsde.ml.registry import get_horizon
    from qsde.ml.report import render_card

    spec = get_horizon(args.horizon)
    snaps = current_snapshots(FFD_VERSION)
    if not snaps:
        log.error("no current feature snapshot")
        return 1
    snap = snaps[-1]
    feats = read_features(FFD_VERSION, snapshot_ids=[snap.snapshot_id]).drop(columns=["known_at"])
    feats["as_of_date"] = pd.to_datetime(feats["as_of_date"])
    log.info("features: %d rows x %d cols from %s", len(feats), feats.shape[1], snap.snapshot_id)

    syms = sorted(feats["symbol"].unique())
    prices = read_sql("SELECT symbol, date, open, high, low, close FROM ohlcv WHERE symbol = ANY(:s) ORDER BY symbol, date",
                      params={"s": syms})
    prices["date"] = pd.to_datetime(prices["date"])
    cal = pd.DatetimeIndex(sorted(prices["date"].unique()))
    ue = read_sql("SELECT symbol, ex_date FROM corp_action_events WHERE kind = 'UNEXPLAINED' AND symbol = ANY(:s)",
                  params={"s": syms})
    log.info("prices: %d rows; unexplained events in scope: %d", len(prices), len(ue))

    t0 = time.time()
    ev = feats[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"})
    lab = make_labels(prices, ev, spec, unexplained=ue)
    lab = lab.merge(feats, on=["symbol", "as_of_date"], how="inner").reset_index(drop=True)
    fcols = [c for c in feats.columns if c not in ("symbol", "as_of_date")]
    log.info("labelled %d events in %.1fs; NaN feature cells %.1f%%", len(lab), time.time() - t0,
             100 * lab[fcols].isna().to_numpy().mean())

    flagged = leak_check(lab[fcols], lab)
    log.info("leak check: %s", flagged or "no single feature exceeds |IC| 0.30")

    pos = pd.Series(np.arange(len(cal)), index=cal)
    tt0 = pos.reindex(lab["as_of_date"]).to_numpy().astype(int)
    tt1 = lab["label_end_session"].to_numpy().astype(int)
    w = cvmod.average_uniqueness(lab["symbol"].to_numpy(), tt0, tt1)
    t0 = time.time()
    out = cvmod.run_cpcv(lab[fcols].to_numpy(float), lab["ret_net"].to_numpy(float), tt0, tt1,
                         lgbm_fit_predict(), weights=w, n_groups=args.groups, k_test=args.k,
                         embargo=spec.embargo_days)
    log.info("CPCV: %d splits, %d paths, %.0fs", len(out["splits"]), out["n_paths"], time.time() - t0)

    key = TrialKey(spec.id, spec.label, snap.universe_version, FFD_VERSION)
    results = []
    for s in out["paths"]:
        f = lab.copy()
        f["score"] = s
        results.append(evaluate_scores(f, cal, spec.id, prices=prices, universe_version=snap.universe_version,
                                       n_trials=trial_count(key, Path(args.ledger_dir)) + 1,
                                       trial_sharpes_daily=trial_sharpes(key, root=Path(args.ledger_dir)),
                                       baseline_seeds=6))
    med = sorted(results, key=lambda r: r["headline"]["sharpe_ann"])[len(results) // 2]
    record_trial(key, "lightgbm", {"n_estimators": 150, "num_leaves": 15, "groups": args.groups, "k": args.k},
                 metrics={"sharpe_daily": med["headline"]["sharpe_daily"], "ic_mean": med["ic"]["mean"],
                          "evidence_class": med["evidence_class"]}, root=Path(args.ledger_dir))

    meta = {"model_family": "LightGBM (pilot, plumbing)", "git_sha": git_sha(), "snapshot_ids": [snap.snapshot_id],
            "dataset_hash": snap.dataset_hash[:16], "universe_version": snap.universe_version,
            "n_symbols": snap.n_symbols, "label_id": spec.label,
            "params": {"n_estimators": 150, "num_leaves": 15, "cpcv": f"{args.groups}x{args.k}",
                       "median_of_paths": len(results)}}
    Path(args.out).mkdir(parents=True, exist_ok=True)
    card = Path(args.out) / f"pilot_lgbm_{spec.id}.md"
    card.write_text(render_card(med, meta), encoding="utf-8")
    log.info("model card -> %s", card)
    print("per-path net Sharpe:", [round(r["headline"]["sharpe_ann"], 2) for r in results])
    print("evidence:", med["evidence_class"], med["evidence_reasons"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
