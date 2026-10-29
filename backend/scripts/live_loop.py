"""Closed prediction loop CLI. Read-mostly and safe: it never trains, promotes or places anything.

    python scripts/live_loop.py resolve                      # write outcomes for predictions whose window is complete
    python scripts/live_loop.py report --model-version X     # calibration / ranking / economics / retrain recommendation
Predictions are written by whatever scores the model (`qsde.live_loop.ledger.append_predictions`); candidates run in SHADOW mode
(logged, never traded) until the owner promotes them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd


def cmd_resolve(_args) -> int:
    from qsde.live_loop.outcomes import resolve_pending
    from qsde.ml import data as mldata

    prices = mldata.load_prices()
    out = resolve_pending(prices)
    print(json.dumps(out))
    return 0


def cmd_report(args) -> int:
    from qsde.db.connection import read_sql
    from qsde.live_loop import monitor
    from qsde.live_loop.outcomes import join_outcomes

    pred = read_sql("SELECT * FROM prediction_log WHERE model_version = :m", params={"m": args.model_version})
    if pred.empty:
        print(f"no predictions logged for {args.model_version}")
        return 1
    pred["prediction_id"] = pred["prediction_id"].str.strip()
    pred["as_of_date"] = pd.to_datetime(pred["as_of_date"])
    oc = read_sql("SELECT * FROM prediction_outcomes WHERE prediction_id = ANY(:ids)", params={"ids": pred["prediction_id"].tolist()})
    oc["prediction_id"] = oc["prediction_id"].str.strip()
    joined = join_outcomes(pred, oc)
    rep = monitor.health_report(joined)
    hid = pred["horizon_id"].iloc[0]
    dec = monitor.retrain_decision(rep, hid, pd.Timestamp.now(), last_retrain=None)
    out = {"model_version": args.model_version, "predictions": int(len(pred)), "resolved": int(len(joined)),
           "unresolved": int(len(pred) - len(joined)), "report": rep, "retrain": dec, "kill_switch": monitor.kill_switch(rep)}
    print(json.dumps(out, indent=2, default=str))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resolve").set_defaults(fn=cmd_resolve)
    r = sub.add_parser("report")
    r.add_argument("--model-version", required=True)
    r.set_defaults(fn=cmd_report)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
