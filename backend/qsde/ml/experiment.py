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
TARGETS = ("cs_rank_gauss", "raw")
NULL_IC_LIMIT = 0.015          # a shuffled-label run must score |IC| below this, or the harness/target is biased


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


def cross_sectional_target(lab: pd.DataFrame, col: str = "ret_net", date: str = "as_of_date") -> np.ndarray:
    """Per-date rank -> normal score of the net return: the model learns WHICH names do better on a date, not the market level.

    Regressing the pooled raw return lets trees spend their capacity on the market component and the heavy tails (measured on
    60 names: raw target OOF rank IC ~ -0.01 while a single feature scored +0.14 through the same harness; rank-gauss target
    recovered ~+0.12-0.13; the shuffled-label null stayed ~0)."""
    from scipy.special import ndtri

    g = lab.groupby(date)[col]
    n = g.transform("count").to_numpy(float)
    r = g.rank(method="average").to_numpy(float)
    p = np.clip((r - 0.5) / np.where(n > 0, n, 1.0), 1e-3, 1 - 1e-3)
    z = ndtri(p)
    z[n < 3] = 0.0
    return np.nan_to_num(z, nan=0.0)


def null_control(lab: pd.DataFrame, X: np.ndarray, y: np.ndarray, t0, t1, w, fit_predict: Callable, groups: int, k: int,
                 embargo: int, seed: int = 0) -> dict:
    """Same CPCV, same model, labels shuffled WITHIN each date, scored against those same shuffled labels.

    With no cross-sectional structure left the OOF rank IC is zero in expectation, so anything beyond sampling noise is a
    mechanical bias of the CV/evaluation pipeline (leakage through overlap, identity memorisation, an alignment bug).
    `ic_vs_true` (same OOF scores against the REAL returns) is informational only: a low-capacity model with a strong feature
    can score +/- that feature's IC from the sign of a noise coefficient, so it cannot be gated."""
    from qsde.ml.eval import spearman_by_date

    rng = np.random.default_rng(seed)
    ys = np.asarray(y, float).copy()
    for idx in lab.groupby("as_of_date").indices.values():
        ys[idx] = rng.permutation(ys[idx])
    out = cvmod.run_cpcv(X, ys, t0, t1, fit_predict, weights=w, n_groups=groups, k_test=k, embargo=embargo)
    ics, ics_true = [], []
    for sc in out["paths"]:
        f = lab[["as_of_date", "ret_net"]].copy()
        f["score"], f["shuf"] = sc, ys
        f = f.dropna(subset=["score"])
        ics.append(float(spearman_by_date(f, "score", "shuf", min_names=MIN_NAMES).mean()))
        ics_true.append(float(spearman_by_date(f, "score", "ret_net", min_names=MIN_NAMES).mean()))
    m = float(np.nanmean(ics))
    n_dates = max(lab["as_of_date"].nunique(), 1)
    names = max(float(lab.groupby("as_of_date").size().median()), 2.0)
    limit = max(NULL_IC_LIMIT, 3.0 / np.sqrt(n_dates * names))      # small samples get a statistically honest allowance
    return {"null_ic_mean": m, "null_ic_paths": [round(i, 4) for i in ics], "ic_vs_true": round(float(np.nanmean(ics_true)), 4),
            "limit": float(limit), "ok": bool(abs(m) < limit)}


def trial_key(ds: LabelledDataset) -> TrialKey:
    # the price policy is part of the key: research-policy trials must not deflate/inflate official ones
    uv = ds.universe_version if ds.price_policy == "official" else f"{ds.universe_version}|{ds.price_policy}"
    return TrialKey(ds.spec.id, ds.spec.label, uv, ds.factor_version)


def run_experiment(ds: LabelledDataset, fit_predict: Callable, model_name: str, params: dict,
                   ledger_dir: Path = LEDGER_DIR, groups: int = 10, k: int = 2, baseline_seeds: int = 10,
                   card_path: Optional[Path] = None, force_plumbing: Optional[str] = None,
                   target: str = "cs_rank_gauss", run_null_control: bool = True) -> dict:
    if target not in TARGETS:
        raise ValueError(f"target must be one of {TARGETS}")
    spec, lab = ds.spec, ds.frame
    params = {**params, "target": target}
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
        X = lab[ds.feature_cols].to_numpy(float)
        y = cross_sectional_target(lab) if target == "cs_rank_gauss" else lab["ret_net"].to_numpy(float)
        out = cvmod.run_cpcv(X, y, t0, t1, fit_predict, weights=w, n_groups=groups, k_test=k, embargo=spec.embargo_days)
        null = (null_control(lab, X, y, t0, t1, w, fit_predict, groups, k, spec.embargo_days) if run_null_control else None)
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
    if null is not None and not null["ok"]:       # the harness reads noise as signal in this configuration
        med["verdict_allowed"] = False
        med["evidence_class"] = "PLUMBING_ONLY"
        med["evidence_reasons"] = list(med["evidence_reasons"]) + [
            f"null control biased: shuffled-label OOF rank IC {null['null_ic_mean']:+.4f} (limit +/-{null['limit']:.3f})"]
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
    if null is not None:
        meta["params"]["null_control_ic"] = round(null["null_ic_mean"], 4)
    try:
        from qsde.features.store import git_sha
        meta["git_sha"] = git_sha()
    except Exception:  # noqa: BLE001
        meta["git_sha"] = "unknown"
    card = render_card(med, meta, pbo)
    if null is not None:
        nl = chr(10)
        card = card.replace(nl + "## Known limits",
                            f"- null control (labels shuffled within date, same model/CV): OOF rank IC "
                            f"vs the shuffled labels {null['null_ic_mean']:+.4f} (vs real returns, informational: {null['ic_vs_true']:+.4f}) per-path {null['null_ic_paths']} - {'OK' if null['ok'] else 'BIASED'} "
                            f"(limit +/-{null['limit']:.3f})" + nl + f"- training target: `{target}`" + nl + nl + "## Known limits", 1)
    if ds.notes:
        card = card.replace("\n## Lineage", "\n> " + " ".join(ds.notes) + "\n\n## Lineage", 1)
    if card_path is not None:
        Path(card_path).parent.mkdir(parents=True, exist_ok=True)
        Path(card_path).write_text(card, encoding="utf-8")
    v, why = verdict(med, pbo)
    return {"trial_id": trial_id, "key": key, "results": results, "median": med, "pbo": pbo, "verdict": v,
            "verdict_reasons": why, "card": card, "leak_flags": flagged, "null_control": null,
            "per_path_sharpe": [round(r["headline"]["sharpe_ann"], 3) for r in results]}
