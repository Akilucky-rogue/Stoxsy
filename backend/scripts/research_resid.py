"""Bounded research experiment: does anything remain once the low-volatility exposure cannot be learned?

PRE-REGISTERED (written before any run; the track is CLOSED as REJECT unless ALL hold for the best-of-3 trial by residual IC):
  1. residual IC (OOF score vs the ATR-neutral realised target) mean >= 0.02 and Newey-West t >= 3
  2. the trial's model long-only net Sharpe at 25 bps exceeds the best of {12-1 momentum, MOP, low-vol} at 25 bps
  3. DSR >= 0.95 using the real ledger trial count for the key (earlier trials on the same key count)
  4. the shuffled-label null control is OK
Exactly 3 trials, position_20d only (swing_5d is dominated by cost: even equal-weight is -1.59 Sharpe at 30 bps). No further iteration.

    python scripts/research_resid.py --snapshot <id> --reviewed-snapshot <id>
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

from scripts.research_eval import BANNER, evaluate_variants
from qsde.ml import eval as ev
from qsde.ml.dataset import build_labelled_dataset
from qsde.ml.experiment import atr_neutral_target, lightgbm_fit_predict, run_experiment
from qsde.ml.registry import get_horizon

log = logging.getLogger("research_resid")
OUT = data_path("reports", "research")
CARDS = data_path("reports", "model_cards")
CONFIGS = [
    ("A", {"n_estimators": 150, "num_leaves": 15}),
    ("B", {"n_estimators": 300, "num_leaves": 31, "learning_rate": 0.03, "min_child_samples": 500}),
    ("C", {"n_estimators": 150, "num_leaves": 7, "min_child_samples": 1000}),
]


def sharpe_at(rows: list, name_prefix: str, col: int) -> float:
    for r in rows:
        if r.startswith(f"| {name_prefix}"):
            cell = r.split("|")[2 + col].strip()
            return float(cell.split("/")[0])
    return float("nan")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--reviewed-snapshot", required=True)
    ap.add_argument("--factor-version", default="ffd_v2s")
    ap.add_argument("--horizon", default="position_20d")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.reviewed_snapshot != args.snapshot:
        log.error("review gate: --reviewed-snapshot must equal --snapshot")
        return 2
    spec = get_horizon(args.horizon)
    ds = build_labelled_dataset(spec, policy="official", factor_version=args.factor_version, snapshot_ids=[args.snapshot])
    lab = ds.frame.copy()
    lab["y_res"] = atr_neutral_target(lab)
    results = []
    for tag, kw in CONFIGS:
        log.info("== trial %s %s", tag, kw)
        card = CARDS / f"research_resid_{tag}_{args.horizon}_{args.factor_version}_{args.snapshot}.md"
        exp = run_experiment(ds, lightgbm_fit_predict(**kw), "lightgbm", {**kw, "purpose": "research_atr_neutral", "trial": tag},
                             groups=10, k=2, card_path=card, target="cs_rank_gauss_atr_neutral", run_null_control=(tag == "A"))
        card.write_text(BANNER + "\n\n" + card.read_text(encoding="utf-8"), encoding="utf-8")
        sc = exp["median_scores"]
        f = lab.assign(score=sc)
        res_ic = ev.spearman_by_date(f, "score", "y_res", min_names=10)
        s = ev.ic_summary(res_ic, spec.label_horizon_days)
        raw_ic = exp["median"]["ic"]
        expo = ev.spearman_by_date(f.assign(neg=-f["tech_atr_pct"]), "score", "neg").mean()
        var = evaluate_variants(ds, sc, spec)
        row = {"tag": tag, "params": kw, "resid_ic": s["mean"], "resid_t": s["t_nw"], "raw_ic": raw_ic["mean"], "lowvol_corr": float(expo),
               "dsr": exp["median"]["dsr"], "null": exp["null_control"], "variants": var,
               "model_s25": sharpe_at(var["rows"], "model long-only", 1), "mop_s25": sharpe_at(var["base"], "MOP", 1),
               "mom_s25": sharpe_at(var["base"], "12-1", 1), "lv_s25": sharpe_at(var["base"], "low-vol", 1)}
        log.info("trial %s: resid IC %.4f (t %.1f) raw IC %.4f lowvol corr %.2f  S25 model %.2f vs mop %.2f mom %.2f lv %.2f  DSR %.3f",
                 tag, row["resid_ic"], row["resid_t"], row["raw_ic"], row["lowvol_corr"], row["model_s25"], row["mop_s25"], row["mom_s25"],
                 row["lv_s25"], row["dsr"]["dsr"])
        results.append(row)
    best = max(results, key=lambda r: r["resid_ic"])
    null_ok = next(r for r in results if r["tag"] == "A")["null"]["ok"]
    c1 = best["resid_ic"] >= 0.02 and best["resid_t"] >= 3
    c2 = best["model_s25"] > max(best["mop_s25"], best["mom_s25"], best["lv_s25"])
    c3 = best["dsr"]["dsr"] >= 0.95
    verdict = "CONTINUE (owner review required)" if (c1 and c2 and c3 and null_ok) else "REJECT - directional-ranker track closed"
    L = [f"# RESEARCH_ONLY bounded experiment: ATR-neutral rank model, {args.horizon}, {args.factor_version} `{args.snapshot}`", "", BANNER, "",
         "Pre-registered criteria (all required for the best-of-3 trial by residual IC): residual IC >= 0.02 and NW t >= 3; net Sharpe @25 bps above "
         "the best of {12-1, MOP, low-vol}; DSR >= 0.95 on the real ledger trial count; null control OK.", "",
         "| trial | params | residual IC | t | raw IC | corr(score, -ATR) | Sharpe@25 model | MOP | 12-1 | low-vol | DSR (N) |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        L.append(f"| {r['tag']} | {r['params']} | {r['resid_ic']:+.4f} | {r['resid_t']:.1f} | {r['raw_ic']:+.4f} | {r['lowvol_corr']:+.2f} | "
                 f"{r['model_s25']:+.2f} | {r['mop_s25']:+.2f} | {r['mom_s25']:+.2f} | {r['lv_s25']:+.2f} | {r['dsr']['dsr']:.3f} ({r['dsr']['n_trials']}) |")
    L += ["", f"- best trial by residual IC: **{best['tag']}**", f"- criteria: residual IC {'PASS' if c1 else 'FAIL'}; beats best baseline @25 bps "
          f"{'PASS' if c2 else 'FAIL'}; DSR {'PASS' if c3 else 'FAIL'}; null control {'OK' if null_ok else 'BIASED'}", f"- **verdict: {verdict}**", ""]
    (OUT / f"research_resid_{args.horizon}_{args.factor_version}_{args.snapshot}.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
