"""
Retrain LightGBM models for all horizons.

Cost handling (AUDIT_v2 C3 / Stage 2C.3 correction):
  The previous docstring here claimed the dataset builder subtracted
  round-trip cost from the forward-return target before training. That
  claim was TRUE only for `label_method="fixed_horizon"` — which nothing
  uses. Under the default `label_method="triple_barrier"`, targets are
  categorical (+1 / 0 / -1) and `target_ret` is the GROSS barrier-exit
  return. The cost subtraction in dataset.py's fwd_returns_pivot path is
  dead code under the default settings.

  Costs enter the pipeline at THE PROMOTION GATE, not in the labels:
    qsde.models.lgbm_model.train_lightgbm_model()
        -> _evaluate_oos_with_costs(oos, cost_bps_round_trip=...)
        -> should_promote(dsr_net)

  So `dsr_net` is what decides whether new weights are promoted. Set the
  cost via QSDE_PROMOTION_COST_BPS (default 10 bps for intraday MIS;
  bump to 25-30 for CNC delivery).

  If you want cost-aware LABELS too (belt-and-braces), pass
  `cost_bps=<n>` to apply_triple_barrier_labels via a dataset builder
  option — it shifts PT/SL barriers so a +1 label means the trade
  cleared its target NET of cost. Off by default because it changes
  the label distribution and would require re-tuning downstream
  thresholds; use for research experiments, not the main gate.

Sequence per horizon:
  1. build_training_dataset(horizon, ...)        -> labelled rows
  2. train_lightgbm_model(dataset, horizon)      -> purged CV, DSR gate,
                                                    saved weights + audit row
After all horizons:
  * Optional: regenerate fresh signals from the new weights, so the dashboard
    picks them up immediately (controlled by --regen-signals; on by default).

Usage:
    python backend/scripts/retrain.py
    python backend/scripts/retrain.py --horizons intraday swing
    python backend/scripts/retrain.py --start-date 2018-01-01
    python backend/scripts/retrain.py --no-regen-signals

By default training requires the DSR promotion threshold to clear before
weights are promoted. To force-promote (useful when reseeding from a fresh
data slice and you accept the model whether or not DSR clears):
    QSDE_FORCE_PROMOTE=true python backend/scripts/retrain.py
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [retrain] %(message)s",
)
log = logging.getLogger("retrain")


def _banner(s: str) -> None:
    log.info("=" * 70)
    log.info(s)
    log.info("=" * 70)


def _was_promoted(horizon: str) -> bool:
    """The gate's OWN audit truth: did this horizon's latest run clear the DSR
    gate and become active? (model_runs.promoted, written by lgbm_model). The
    training function returns a model object whether or not it promoted, so this
    is the only honest source for the summary."""
    try:
        from qsde.db.connection import read_sql
        df = read_sql(
            "SELECT promoted FROM model_runs WHERE horizon = :h "
            "ORDER BY created_at DESC LIMIT 1",
            params={"h": horizon},
        )
        return bool(df.iloc[0]["promoted"]) if not df.empty else False
    except Exception:  # noqa: BLE001
        return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizons", nargs="+", default=["intraday", "swing", "long"],
                    choices=["intraday", "swing", "long"],
                    help="Which horizons to retrain.")
    ap.add_argument("--start-date", default="2018-01-01",
                    help="Earliest as_of_date for the training dataset.")
    ap.add_argument("--no-regen-signals", action="store_true",
                    help="Skip regenerating fresh signals from the new weights.")
    ap.add_argument("--no-fracdiff", action="store_true",
                    help="Disable fractional differencing on features.")
    ap.add_argument("--label-method", default="triple_barrier",
                    choices=["triple_barrier", "fixed_horizon"])
    args = ap.parse_args()

    from qsde.models.dataset    import build_training_dataset
    from qsde.models.lgbm_model import train_lightgbm_model
    from qsde.risk.costs        import cost_bps

    t0 = time.time()
    log.info("Retrain run starting — horizons=%s start_date=%s label=%s fracdiff=%s",
             args.horizons, args.start_date, args.label_method, not args.no_fracdiff)
    for h in args.horizons:
        log.info("  %s round-trip cost in target = %.1f bps (%.4f frac)",
                 h, cost_bps(h), cost_bps(h) / 10000.0)

    results: dict[str, dict] = {}
    for horizon in args.horizons:
        _banner(f"Building dataset for {horizon}")
        try:
            dataset = build_training_dataset(
                horizon=horizon,
                start_date=args.start_date,
                label_method=args.label_method,
                apply_fracdiff=not args.no_fracdiff,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("dataset build failed for %s: %s", horizon, e)
            results[horizon] = {"ok": False, "error": str(e), "stage": "dataset"}
            continue

        if dataset is None or len(dataset) == 0:
            log.warning("Empty dataset for %s — skipping", horizon)
            results[horizon] = {"ok": False, "error": "empty dataset", "stage": "dataset"}
            continue

        n_samples  = len(dataset)
        n_symbols  = dataset["symbol"].nunique() if "symbol" in dataset.columns else None
        n_dates    = dataset["as_of_date"].nunique() if "as_of_date" in dataset.columns else None
        log.info("Dataset ready: %d rows  symbols=%s  dates=%s",
                 n_samples, n_symbols, n_dates)

        _banner(f"Training {horizon} (purged CV -> DSR gate)")
        try:
            model = train_lightgbm_model(dataset, horizon=horizon)
        except Exception as e:  # noqa: BLE001
            log.exception("training failed for %s: %s", horizon, e)
            results[horizon] = {"ok": False, "error": str(e), "stage": "train"}
            continue

        # `model is not None` only means training SUCCEEDED — the candidate is
        # saved whether or not it cleared the DSR gate. Promotion truth is the
        # gate's own audit row (model_runs.promoted), NOT "did we get a model".
        trained = model is not None
        results[horizon] = {
            "ok":        trained,
            "n_samples": n_samples,
            "promoted":  _was_promoted(horizon) if trained else False,
        }
        if not trained:
            log.warning("%s: training failed (no dataset/model).", horizon)
        elif not results[horizon]["promoted"]:
            log.info("%s: trained + candidate saved, but NOT promoted (DSR below "
                     "gate; active model unchanged). This is the expected honest "
                     "outcome until net edge clears the gate.", horizon)

    # Regenerate fresh signals so dashboard & paper-taker pick up new weights.
    if not args.no_regen_signals:
        _banner("Regenerating signals from new weights")
        try:
            from qsde.models.signal_generator import generate_signals
            for h in args.horizons:
                if not results.get(h, {}).get("promoted"):
                    log.warning("  %s: skipping regen — model not promoted", h)
                    continue
                n = generate_signals(h)
                log.info("  %s: %s signals", h, n)
        except Exception as e:  # noqa: BLE001
            log.exception("signal regen failed: %s", e)

    elapsed = time.time() - t0
    _banner(f"Retrain complete in {elapsed:.0f}s")
    trained  = [h for h, r in results.items() if r.get("ok")]
    promoted = [h for h, r in results.items() if r.get("promoted")]
    failed   = [h for h, r in results.items() if not r.get("ok")]
    log.info("Trained:  %s", trained or "[]")
    log.info("Promoted (cleared net-DSR gate): %s", promoted or "[] — none cleared the gate")
    if failed:
        log.warning("Training FAILED: %s", failed)

    log.info(
        "Next step: re-run scripts/simulate_strategies.py and "
        "scripts/stress_test_intraday.py to refresh weights/edge_stats.json. "
        "The drift report's backtest comparison reads from there."
    )

    # Exit non-zero only on a real TRAINING failure. A clean run that promotes
    # nothing (DSR below gate) is the EXPECTED honest outcome, not a failure.
    sys.exit(2 if failed else 0)


if __name__ == "__main__":
    main()
