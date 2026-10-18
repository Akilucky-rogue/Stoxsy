"""
Retrain candidates through the model-lab contract (registry labels -> CPCV harness -> trial ledger -> model card).

This replaces the legacy path (`build_training_dataset` close-of-day labels -> `lgbm_model` OOF evaluator -> DSR gate
with its own cost/horizon tables). Everything now flows through the same definitions the paper journal and the
Scorecard resolve with:

    qsde.ml.registry   horizons, label tb_nextopen_v1, cost models, cost stress levels
    qsde.ml.dataset    snapshot features + labels (entry at the OPEN of t+1) under an explicit price policy
    qsde.ml.experiment CPCV -> evaluation -> ledger trial (failures count) -> PBO -> model card

Costs: labels carry both `ret_gross` and `ret_net` (registry cost model, `promotion_cost_bps`); the model regresses on
`ret_net`; the harness charges the cost once per position and stresses 10/25/30 bps. Costs therefore enter in the
LABELS (as a flat per-position deduction) and again at evaluation and at the promotion gate - not at a separate
horizon table.

NOTHING IS PROMOTED HERE. Promotion is a separate, written decision (QSDE_ML_PROMOTION_ENABLED stays false until the
harness gate, the adjusted-price policy and the Scorecard entry convention are all aligned and a human signs off).
With --save-candidate a fitted model is written to D:\\Stoxsy\\data\\models\\candidates\\<horizon>\\ with a sidecar
describing exactly what it was trained on; the active weights and `model_runs` are never touched.

Review gate: on anything other than a PILOT/SYNTHETIC plumbing universe the run refuses to start unless you pass
`--reviewed-snapshot <id>` equal to the current snapshot id, i.e. you have read that snapshot's data-quality report
and the corporate-action reconciliation.

Usage:
    python scripts/retrain.py --horizons swing_5d                      # pilot: PLUMBING_ONLY, no verdict
    python scripts/retrain.py --horizons swing_5d position_20d --reviewed-snapshot <id> --save-candidate
    python scripts/retrain.py --horizons swing_5d --policy research    # explicit; card says adjustment incomplete
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [retrain] %(message)s")
log = logging.getLogger("retrain")

CARDS = Path(r"D:\Stoxsy\data\reports\model_cards")
SMOKE_LEDGER = Path(r"D:\Stoxsy\data\ledger_smoke")      # smoke trials never count towards a real key's DSR
CANDIDATES = Path(r"D:\Stoxsy\data\models\candidates")


def _banner(s: str) -> None:
    log.info("=" * 70)
    log.info(s)
    log.info("=" * 70)


def dataset_kwargs(args: argparse.Namespace, horizon: str) -> dict:
    """Arguments for the legacy-compatible `models.dataset.build_training_dataset` shim.

    apply_fracdiff is always False: features are fractionally differenced once, at ingest (factor_version
    ffd_v2). The old --no-fracdiff flag is accepted and ignored.
    """
    return {"horizon": horizon, "start_date": args.start_date, "label_method": args.label_method,
            "apply_fracdiff": False}


def is_plumbing_universe(universe_version: str) -> bool:
    u = str(universe_version).upper()
    return "PILOT" in u or "SURVIVOR" in u or "SYNTHETIC" in u


def review_gate(snapshot_id: str, universe_version: str, reviewed: str | None, save_candidate: bool) -> str | None:
    """None if the run may proceed, else the reason it must not."""
    plumbing = is_plumbing_universe(universe_version)
    if not plumbing and reviewed != snapshot_id:
        return (f"REVIEW GATE: snapshot {snapshot_id} ({universe_version}) is a full-universe snapshot. Read its "
                f"data-quality report (scripts/snapshot_dq_report.py) and the corporate-action reconciliation, then "
                f"rerun with --reviewed-snapshot {snapshot_id}")
    if plumbing and save_candidate:
        return f"refusing --save-candidate on a plumbing universe ({universe_version}): the result is not evidence"
    return None


def smoke_subset(symbols, n: int) -> list:
    """Explicit symbols, or the n most liquid names on the last universe date (deterministic, ties by symbol)."""
    if symbols:
        return sorted(set(s.upper() for s in symbols))
    from qsde.db.connection import read_sql
    d = read_sql("SELECT symbol FROM universe_pit WHERE method = 'adv60_top500_v1' AND date = "
                 "(SELECT max(date) FROM universe_pit WHERE method = 'adv60_top500_v1') ORDER BY rank, symbol LIMIT :n",
                 params={"n": n})
    return sorted(d["symbol"].tolist())


def save_candidate(ds, exp, horizon_id: str) -> Path:
    """Fit on ALL labelled rows and store as a CANDIDATE (never the active model)."""
    import joblib

    from qsde.ml.cv import average_uniqueness
    from qsde.ml.experiment import fit_lightgbm

    lab = ds.frame
    pos = {d: i for i, d in enumerate(ds.calendar)}
    t0 = lab["as_of_date"].map(pos).to_numpy().astype(int)
    w = average_uniqueness(lab["symbol"].to_numpy(), t0, lab["label_end_session"].to_numpy().astype(int))
    from qsde.ml.experiment import cross_sectional_target
    model = fit_lightgbm(lab[ds.feature_cols].to_numpy(float), cross_sectional_target(lab), w)   # same target the CPCV trial used
    out = CANDIDATES / horizon_id
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{exp['trial_id']}.joblib"
    joblib.dump(model, path)
    (out / f"{exp['trial_id']}.json").write_text(json.dumps({
        "trial_id": exp["trial_id"], "horizon": horizon_id, "features": ds.feature_cols, "dataset": ds.meta(),
        "verdict": exp["verdict"], "evidence_class": exp["median"]["evidence_class"], "promoted": False,
        "note": "CANDIDATE ONLY - not wired into signal generation"}, indent=2, default=str))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizons", nargs="+", default=["swing_5d"],
                    help="registry ids (intraday_1d swing_5d swing_10d position_20d) or legacy names")
    ap.add_argument("--policy", default="official", choices=["official", "research"])
    ap.add_argument("--factor-version", default=None,
                    help="feature version to read (default ffd_v2; ffd_v2s = features computed on series stitched across renames)")
    ap.add_argument("--groups", type=int, default=10)
    ap.add_argument("--k", type=int, default=2)
    ap.add_argument("--reviewed-snapshot", default=None,
                    help="current snapshot id; required for any non-plumbing universe (proves the DQ report was read)")
    ap.add_argument("--save-candidate", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="plumbing run: small explicit symbol subset, PLUMBING_ONLY card, no verdict, separate ledger, "
                         "entry-convention contract check; implies no candidate saving")
    ap.add_argument("--symbols", nargs="*", default=None, help="explicit symbols for --smoke (default: --smoke-n liquid names)")
    ap.add_argument("--smoke-n", type=int, default=60, help="size of the default deterministic smoke subset")
    ap.add_argument("--ledger-dir", default=None)
    ap.add_argument("--start-date", default="2018-01-01", help="legacy; used by dataset_kwargs only")
    ap.add_argument("--label-method", default="triple_barrier", help="legacy; only triple_barrier exists")
    ap.add_argument("--no-fracdiff", action="store_true", help="deprecated, ignored")
    ap.add_argument("--no-regen-signals", action="store_true", help="deprecated, ignored (nothing is promoted here)")
    args = ap.parse_args()

    from qsde.factors.versions import FFD_VERSION
    from qsde.features.store import current_snapshots
    from qsde.ml.dataset import build_labelled_dataset
    from qsde.ml.experiment import lightgbm_fit_predict, run_experiment
    from qsde.ml.ledger import LEDGER_DIR
    from qsde.ml.registry import get_horizon

    factor_version = args.factor_version or FFD_VERSION
    snaps = current_snapshots(factor_version)
    if not snaps:
        log.error("no current feature snapshot for %s", factor_version)
        return 1
    snap = [x for x in snaps if "full" in x.universe_version][-1] if any("full" in x.universe_version for x in snaps) else snaps[-1]
    if args.smoke and args.save_candidate:
        log.error("--smoke never saves candidates")
        return 3
    blocked = review_gate(snap.snapshot_id, snap.universe_version, args.reviewed_snapshot, args.save_candidate)
    if blocked:
        log.error(blocked)
        return 3

    ledger = Path(args.ledger_dir) if args.ledger_dir else (SMOKE_LEDGER if args.smoke else LEDGER_DIR)
    smoke_symbols = None
    if args.smoke:
        smoke_symbols = smoke_subset(args.symbols, args.smoke_n)
        log.info("SMOKE RUN on %d symbols: %s", len(smoke_symbols), ", ".join(smoke_symbols))
    t0 = time.time()
    results: dict[str, dict] = {}
    for h in args.horizons:
        spec = get_horizon(h)
        _banner(f"{spec.id}: dataset (policy={args.policy}, snapshot {snap.snapshot_id})")
        try:
            ds = build_labelled_dataset(spec, policy=args.policy, symbols=smoke_symbols, factor_version=factor_version,
                                        snapshot_ids=[snap.snapshot_id])
            params = {"n_estimators": 150, "num_leaves": 15}
            card = CARDS / (f"{'smoke_' if args.smoke else ''}{spec.id}_{factor_version}_{snap.snapshot_id}"
                            f"{'_research' if args.policy == 'research' else ''}.md")
            _banner(f"{spec.id}: CPCV {args.groups}x{args.k} -> harness -> ledger")
            exp = run_experiment(ds, lightgbm_fit_predict(**params), "lightgbm", params, ledger_dir=ledger,
                                 groups=6 if args.smoke else args.groups, k=args.k, card_path=card,
                                 baseline_seeds=3 if args.smoke else 10,
                                 force_plumbing=("smoke run on %d listed symbols: wiring check only" % len(smoke_symbols))
                                 if args.smoke else None)
            contract = None
            if args.smoke:
                from qsde.ml.contract import check_entry_contract
                contract = check_entry_contract(ds)
                log.info("entry-convention contract: %d labels regraded through the Scorecard resolver and the paper "
                         "journal, %d disagreements", contract["n_checked"], len(contract["failures"]))
            cand = save_candidate(ds, exp, spec.id) if args.save_candidate else None
            results[spec.id] = {"ok": True, "verdict": exp["verdict"], "evidence": exp["median"]["evidence_class"],
                                "pbo": exp["pbo"], "card": str(card), "candidate": str(cand) if cand else None,
                                "per_path_sharpe": exp["per_path_sharpe"],
                                "contract_failures": None if contract is None else contract["failures"][:5],
                                "contract_checked": None if contract is None else contract["n_checked"],
                                "rows": int(len(ds.frame)), "features": len(ds.feature_cols)}
        except Exception as e:  # noqa: BLE001
            log.exception("%s failed: %s", spec.id, e)
            results[spec.id] = {"ok": False, "error": str(e)}

    _banner(f"Retrain complete in {time.time() - t0:.0f}s  (nothing promoted)")
    for h, r in results.items():
        log.info("%s: %s", h, r)
    return 2 if any(not r.get("ok") for r in results.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
