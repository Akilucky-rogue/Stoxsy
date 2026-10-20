"""Presentation layer for the closed loop (pure): turns ledger rows + outcome rows into what the Live compare page shows.

Hard rules kept here, not left to the page:
  * a prediction is shown exactly as logged (nothing is recomputed or edited);
  * an outcome is shown only when it exists AND the resolver's data cut-off had reached the exit date: nothing is joined before the window resolved;
  * "expected" figures are shown only if the model logged them (a model that made no forecast is not graded against one);
  * the state of a model is `research`, `paper` or `approved`: the word "guaranteed" never appears and an unknown model is `research`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from qsde.decision.cards import add_sessions
from qsde.live_loop import monitor
from qsde.ml.registry import get_horizon

STATES = ("research", "paper", "approved")
REGISTRY = Path(__file__).resolve().parents[3] / "infra" / "data" / "model_registry.json"
LINEAGE_COLS = ("model_card_id", "snapshot_id", "factor_version", "universe_version", "feature_hash", "ca_source_hash", "git_sha")


def model_registry(path: Path = REGISTRY) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("models", {})
    except (OSError, ValueError):
        return {}


def model_state(model_version: str, reg: Optional[dict] = None) -> str:
    reg = model_registry() if reg is None else reg
    s = (reg.get(model_version) or {}).get("state", "research")
    return s if s in STATES else "research"


def _f(x) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) or pd.isna(x) else float(x)


def _d(x) -> Optional[str]:
    return None if x is None or pd.isna(x) else str(pd.Timestamp(x).date())


def expected_window(as_of, horizon_id: str) -> dict:
    """Estimated entry date and latest exit date (weekday sessions; exchange holidays after today are not known here, so dates are estimates)."""
    spec = get_horizon(horizon_id)
    a = pd.Timestamp(as_of).date()
    entry = add_sessions(a, 1)
    return {"entry_date": entry.isoformat(), "exit_by": add_sessions(a, spec.label_horizon_days + 1).isoformat(), "sessions": spec.label_horizon_days, "estimate": True}


def shape_prediction(p: dict, o: Optional[dict], reg: Optional[dict] = None) -> dict:
    """p: a prediction_log row (dict); o: its prediction_outcomes row or None."""
    spec = get_horizon(p["horizon_id"])
    sigma = _f(p.get("sigma"))
    win = expected_window(p["as_of_date"], p["horizon_id"])
    outcome, state = None, "pending"
    if o is not None and o.get("status") is not None:
        exit_d, through = _d(o.get("exit_date")), _d(o.get("data_through"))
        if exit_d is not None and through is not None and through >= exit_d:        # never show an outcome the resolver could not yet have seen
            exp = _f(p.get("expected_return"))
            net = _f(o.get("ret_net"))
            outcome = {"status": o["status"], "entry_date": _d(o.get("entry_date")), "entry_price": _f(o.get("entry_price")), "exit_date": exit_d, "exit_price": _f(o.get("exit_price")),
                       "stop_price": _f(o.get("stop_price")), "target_price": _f(o.get("target_price")), "gap_exit": None if o.get("gap_exit") is None else bool(o["gap_exit"]),
                       "ret_gross": _f(o.get("ret_gross")), "ret_net": net, "cost_bps": _f(o.get("cost_bps")), "excluded": bool(o.get("excluded")), "data_through": through,
                       "expected_return": exp, "realized_minus_expected": None if exp is None or net is None else net - exp}
            state = "excluded" if outcome["excluded"] else "resolved"
    return {"prediction_id": str(p["prediction_id"]).strip(), "symbol": p["symbol"], "model_version": p["model_version"], "horizon_id": p["horizon_id"], "side": int(p["side"]),
            "as_of_date": _d(p["as_of_date"]), "prediction_time": pd.Timestamp(p["prediction_time"]).isoformat(), "state": state,
            "score": _f(p.get("score")), "rank_pct": _f(p.get("rank_pct")), "p_win": _f(p.get("p_win")), "expected_return": _f(p.get("expected_return")),
            "ci_low": _f(p.get("ci_low")), "ci_high": _f(p.get("ci_high")), "position_size": _f(p.get("position_size")), "regime": p.get("regime"),
            "ref_price": _f(p.get("ref_price")), "sigma": sigma,
            "entry_rule": spec.entry_price_rule.replace("_", " "), "exit_rule": spec.exit_rule.replace("_", " "), "window": win,
            "barriers": {"stop_sigmas": spec.barrier.sl_mult, "target_sigmas": spec.barrier.pt_mult, "stop_distance": None if sigma is None else spec.barrier.sl_mult * sigma,
                         "target_distance": None if sigma is None else spec.barrier.pt_mult * sigma},
            "cost_model": spec.cost_model, "lineage": {k: (p.get(k) if p.get(k) is not None and not (isinstance(p.get(k), float) and pd.isna(p.get(k))) else None) for k in LINEAGE_COLS},
            "model_state": model_state(p["model_version"], reg), "outcome": outcome}


def health(joined: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> dict:
    """Calibration, rolling rank IC, economics, drift and kill-switch status for ONE model and horizon, from resolved predictions only."""
    now = pd.Timestamp.now() if now is None else pd.Timestamp(now)
    out: dict = {"n_resolved": 0, "n_dates": 0, "notes": []}
    if joined.empty:
        out["notes"].append("no resolved predictions yet: calibration, rank IC and economics need outcomes, which appear only after each window has elapsed")
        out["kill_switch"], out["retrain"] = [], None
        return out
    j = joined.copy()
    j["as_of_date"] = pd.to_datetime(j["as_of_date"])
    rep = monitor.health_report(j)
    out.update({k: v for k, v in rep.items() if k in ("n_resolved", "n_dates", "ranking", "economics", "calibration", "by_regime")})
    d = j[~j["excluded"].fillna(False)] if "excluded" in j else j
    ic = monitor.rank_ic(d) if len(d) else pd.Series(dtype=float)
    out["ic_series"] = {"dates": [str(pd.Timestamp(i).date()) for i in ic.index], "values": [None if not np.isfinite(v) else float(v) for v in ic.values],
                        "rolling60": [None if not np.isfinite(v) else float(v) for v in ic.rolling(60, min_periods=10).mean().values]} if len(ic) else None
    if "p_win" in d and d["p_win"].notna().sum() >= 50:
        c = d.dropna(subset=["p_win"])
        out["reliability"] = [{"bin": int(r.bin), "mean_p": float(r.mean_p), "observed": float(r.observed), "n": int(r.n)} for r in monitor.reliability_table(c["p_win"], (c["ret_net"] > 0).astype(float)).itertuples()]
    else:
        out["notes"].append("calibration not available: this model logs no win probability (nothing to calibrate) or fewer than 50 resolved probabilities")
    dates = sorted(d["as_of_date"].unique())
    if len(dates) >= 40:
        half = len(dates) // 2
        ref, cur = d[d["as_of_date"].isin(dates[:20])]["score"], d[d["as_of_date"].isin(dates[-20:])]["score"]
        v = monitor.psi(ref, cur)
        out["score_drift"] = {"psi": None if not np.isfinite(v) else float(v), "reference": "first 20 dates", "current": "last 20 dates", "flag": bool(np.isfinite(v) and v > 0.25)}
    else:
        out["notes"].append("score drift needs at least 40 resolved dates")
    out["notes"].append("feature-level drift is not monitored here: only a hash of the feature set is logged per prediction")
    th = monitor.Thresholds()
    hid = str(j["horizon_id"].iloc[0])
    out["kill_switch"] = monitor.kill_switch(rep)
    out["retrain"] = monitor.retrain_decision(rep, hid, now)
    if rep.get("n_resolved", 0) < th.min_resolved or rep.get("n_dates", 0) < th.min_dates:
        out["notes"].append(f"evidence is thin: {rep.get('n_resolved', 0)} resolved over {rep.get('n_dates', 0)} dates (the monitor acts on at least {th.min_resolved} / {th.min_dates})")
    return out
