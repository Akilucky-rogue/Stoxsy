"""One experiment = dataset -> CPCV -> harness evaluation -> ledger trial -> model card.

This is the only path by which a model gets a verdict. Replaces the legacy
`models.lgbm_model` evaluator (OOF folds scored with its own cost and horizon tables).

    ds  = build_labelled_dataset("swing_5d")
    exp = run_experiment(ds, lightgbm_fit_predict(), "lightgbm", {"n_estimators": 150})
    exp["card"]  -> markdown; exp["median"] -> harness result of the median CPCV path

Rules enforced here, not left to callers:
  * every call appends a trial to the ledger (failed fits too), so the DSR trial count cannot be gamed
  * the RESEARCH price policy (low-confidence price-inferred corporate actions applied) can never yield a verdict
  * PBO is computed from the stored trial series as soon as the key has >= 2 of them
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import pandas as pd

from qsde.ml import cv as cvmod
from qsde.ml.dataset import LabelledDataset
from qsde.ml.eval import evaluate_scores, leak_check, pbo_cscv, run_portfolio
from qsde.ml.ledger import LEDGER_DIR, TrialKey, load_trial_series, record_trial, trial_count, trial_sharpes
from qsde.ml.report import render_card, verdict

log = logging.getLogger(__name__)

TOP_Q = 0.2
MIN_NAMES = 10


def fit_lightgbm(Xtr, ytr, wtr=None, n_estimators: int = 150, num_leaves: int = 15, learning_rate: float = 0.05,
                 min_child_samples: int = 200, seed: int = 0, n_jobs: int = 8):
    """Regression on the NET barrier return (ret_net), sample-weighted by label uniqueness."""
    import lightgbm as lgb

    m = lgb.LGBMRegressor(n_estimators=n_estimators, num_leaves=num_leaves, learning_rate=learning_rate,
                          min_child_samples=min_child_samples, subsample=0.8, subsample_freq=1,
                          colsample_bytree=0.7, reg_lambda=5.0, n_jobs=n_jobs, verbose=-1, random_state=seed)
    m.fit(Xtr, ytr, sample_weight=wtr)
    return m


def lightgbm_fit_predict(**kw):
    def fp(Xtr, ytr, wtr, Xte):
        return fit_lightgbm(Xtr, ytr, wtr, **kw).predict(Xte)
    return fp


def trial_key(ds: LabelledDataset) -> TrialKey:
    # the price policy is part of the key: research-policy trials must not deflate/inflate official ones
    uv = ds.universe_version if ds.price_policy == "official" else f"{ds.universe_version}|{ds.price_policy}"
    return TrialKey(ds.spec.id, ds.spec.label, uv, ds.factor_version)


def run_experiment(ds: LabelledDataset, fit_predict: Callable, model_name: str, params: dict,
                   ledger_dir: Path = LEDGER_DIR, groups: int = 10, k: int = 2, baseline_seeds: int = 10,
                   card_path: Optional[Path] = None, force_plumbing: Optional[str] = None) -> dict:
    spec, lab = ds.spec, ds.frame
    key = trial_key(ds)
    ledger_dir = Path(ledger_dir)
    if lab.empty:
        record_trial(key, model_name, params, status="failed", error="empty labelled dataset", root=ledger_dir)
        raise ValueError("empty labelled dataset")
    try:
        flagged = leak_check(lab[ds.feature_cols], lab)
        if flagged:
            log.warning("leak check: single features with |IC| > 0.30: %s", flagged)

        pos = pd.Series(np.arange(len(ds.calendar)), index=ds.calendar)
        t0 = pos.reindex(lab["as_of_date"]).to_numpy().astype(int)
        t1 = lab["label_end_session"].to_numpy().astype(int)
        w = cvmod.average_uniqueness(lab["symbol"].to_numpy(), t0, t1)
        t_fit = time.time()
        out = cvmod.run_cpcv(lab[ds.feature_cols].to_numpy(float), lab["ret_net"].to_numpy(float), t0, t1,
                             fit_predict, weights=w, n_groups=groups, k_test=k, embargo=spec.embargo_days)
        log.info("CPCV %dx%d: %d splits, %d paths, %.0fs", groups, k, len(out["splits"]), out["n_paths"],
                 time.time() - t_fit)

        n_prior = trial_count(key, ledger_dir)
        prior_sr = trial_sharpes(key, root=ledger_dir)
        results = []
        for s in out["paths"]:
            f = lab.copy()
            f["score"] = s
            results.append(evaluate_scores(f, ds.calendar, spec.id, prices=ds.prices,
                                           universe_version=ds.universe_version, n_trials=n_prior + 1,
                                           trial_sharpes_daily=prior_sr, baseline_seeds=baseline_seeds,
                                           top_q=TOP_Q, min_names=MIN_NAMES))
    except Exception as e:  # noqa: BLE001 - failed trials count toward DSR
        record_trial(key, model_name, params, status="failed", error=str(e)[:300], root=ledger_dir)
        raise

    order = sorted(range(len(results)), key=lambda i: results[i]["headline"]["sharpe_ann"])
    mid = order[len(order) // 2]
    med = results[mid]
    if force_plumbing:                             # smoke run: the result is a wiring check, never evidence
        med["verdict_allowed"] = False
        med["evidence_class"] = "PLUMBING_ONLY"
        med["evidence_reasons"] = list(med["evidence_reasons"]) + [force_plumbing]
    if not ds.adjustment_complete:                 # research policy: never a verdict
        med["verdict_allowed"] = False
        med["evidence_class"] = "PLUMBING_ONLY"
        med["evidence_reasons"] = list(med["evidence_reasons"]) + [
            "research price policy: low-confidence price-inferred corporate actions applied; adjustment incomplete"]

    f_mid = lab.copy()
    f_mid["score"] = out["paths"][mid]
    series = run_portfolio(f_mid.dropna(subset=["score"]), ds.calendar, spec.label_horizon_days, TOP_Q, MIN_NAMES,
                           float(lab["cost_bps"].iloc[0]))
    trial_id = record_trial(key, model_name, params,
                            metrics={"sharpe_daily": med["headline"]["sharpe_daily"], "ic_mean": med["ic"]["mean"],
                                     "evidence_class": med["evidence_class"], "price_policy": ds.price_policy},
                            series=series, root=ledger_dir)
    stored = load_trial_series(key, ledger_dir)
    pbo = pbo_cscv(stored)["pbo"] if stored.shape[1] >= 2 else None
    if pbo is not None and not np.isfinite(pbo):
        pbo = None

    meta = {"model_family": f"{model_name} ({'pilot, plumbing' if not med['verdict_allowed'] else 'candidate'})",
            "snapshot_ids": ds.snapshot_ids, "dataset_hash": ds.dataset_hash[:16],
            "universe_version": ds.universe_version, "n_symbols": ds.n_symbols, "label_id": spec.label,
            "params": {**params, "cpcv": f"{groups}x{k}", "median_of_paths": len(results),
                       "price_policy": ds.price_policy, "exclusion_events": ds.exclusion_events}}
    try:
        from qsde.features.store import git_sha
        meta["git_sha"] = git_sha()
    except Exception:  # noqa: BLE001
        meta["git_sha"] = "unknown"
    card = render_card(med, meta, pbo)
    if ds.notes:
        card = card.replace("\n## Lineage", "\n> " + " ".join(ds.notes) + "\n\n## Lineage", 1)
    if card_path is not None:
        Path(card_path).parent.mkdir(parents=True, exist_ok=True)
        Path(card_path).write_text(card, encoding="utf-8")
    v, why = verdict(med, pbo)
    return {"trial_id": trial_id, "key": key, "results": results, "median": med, "pbo": pbo, "verdict": v,
            "verdict_reasons": why, "card": card, "leak_flags": flagged,
            "per_path_sharpe": [round(r["headline"]["sharpe_ann"], 3) for r in results]}
