"""Run dataset -> CPCV -> harness -> ledger -> model card on the current feature snapshot.

On the 46-symbol pilot (or any PILOT/SYNTHETIC/narrow universe) this is a PLUMBING check only: the harness stamps the
result PLUMBING_ONLY and the card carries no verdict.

Default is the OFFICIAL price series (NSE-source-confirmed adjustments only; windows touching price-inferred events
are excluded from labels). `--include-inferred` selects the RESEARCH policy and the card says adjustment is incomplete.

    python scripts/run_harness_pilot.py --horizon swing_5d
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
log = logging.getLogger("harness_pilot")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", default="swing_5d")
    ap.add_argument("--groups", type=int, default=6)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--include-inferred", action="store_true",
                    help="RESEARCH policy: also apply low-confidence price-inferred corporate-action events "
                         "(card states adjustment is incomplete; no verdict is ever issued)")
    ap.add_argument("--ledger-dir", default=r"D:\Stoxsy\data\ledger_pilot")
    ap.add_argument("--out", default=r"D:\Stoxsy\data\reports\model_cards")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from qsde.ml.data import OFFICIAL, RESEARCH
    from qsde.ml.dataset import build_labelled_dataset
    from qsde.ml.experiment import lightgbm_fit_predict, run_experiment

    policy = RESEARCH if args.include_inferred else OFFICIAL
    ds = build_labelled_dataset(args.horizon, policy=policy)
    log.info("dataset: %s", ds.meta())
    card = Path(args.out) / f"pilot_lgbm_{ds.spec.id}{'_research' if args.include_inferred else ''}.md"
    params = {"n_estimators": 150, "num_leaves": 15}
    exp = run_experiment(ds, lightgbm_fit_predict(**params), "lightgbm", params, ledger_dir=Path(args.ledger_dir),
                         groups=args.groups, k=args.k, baseline_seeds=6, card_path=card)
    log.info("model card -> %s", card)
    print("per-path net Sharpe:", exp["per_path_sharpe"])
    print("evidence:", exp["median"]["evidence_class"], exp["median"]["evidence_reasons"])
    print("verdict:", exp["verdict"], "| PBO:", exp["pbo"], "| leak flags:", exp["leak_flags"] or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
