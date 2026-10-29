"""Append-only trial ledger.

Every model variant, hyper-parameter draw and failed run is recorded, keyed by
(horizon_id, label_id, universe_version, feature_version). The number of trials
and the spread of their Sharpe ratios are what a Deflated Sharpe Ratio needs; a
ledger that only keeps winners makes DSR meaningless.

Format: one JSON object per line in <LEDGER_DIR>/trials.jsonl, never rewritten.
Optional daily net-return series are stored as parquet next to it (needed for PBO).
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from qsde.config.paths import data_path

LEDGER_DIR = Path(os.getenv("STOXSY_LEDGER_DIR", str(data_path("ledger"))))


@dataclass(frozen=True)
class TrialKey:
    horizon_id: str
    label_id: str
    universe_version: str
    feature_version: str

    def as_dict(self) -> dict:
        return {"horizon_id": self.horizon_id, "label_id": self.label_id,
                "universe_version": self.universe_version, "feature_version": self.feature_version}


def _file(root: Path) -> Path:
    return Path(root) / "trials.jsonl"


def record_trial(key: TrialKey, family: str, params: dict, status: str = "ok",
                 metrics: Optional[dict] = None, series: Optional[pd.Series] = None,
                 error: Optional[str] = None, root: Path = LEDGER_DIR) -> str:
    """Append one trial. status 'ok' | 'failed'. Failed trials COUNT as trials."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    trial_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
    series_path = None
    if series is not None and len(series):
        (root / "series").mkdir(exist_ok=True)
        series_path = f"series/{trial_id}.parquet"
        series.rename("ret").to_frame().to_parquet(root / series_path)
    rec = {"trial_id": trial_id, "ts": datetime.now(timezone.utc).isoformat(), **key.as_dict(),
           "family": family, "params": params, "status": status, "metrics": metrics or {},
           "error": error, "series": series_path}
    with open(_file(root), "a", encoding="utf-8") as f:       # append-only
        f.write(json.dumps(rec, default=str, sort_keys=True) + "\n")
    return trial_id


def load_trials(key: Optional[TrialKey] = None, root: Path = LEDGER_DIR) -> list[dict]:
    p = _file(root)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if key is None or all(r.get(k) == v for k, v in key.as_dict().items()):
                out.append(r)
    return out


def trial_count(key: TrialKey, root: Path = LEDGER_DIR) -> int:
    """Number of trials of ANY status under this key."""
    return len(load_trials(key, root))


def trial_sharpes(key: TrialKey, metric: str = "sharpe_daily", root: Path = LEDGER_DIR) -> np.ndarray:
    vals = [t["metrics"].get(metric) for t in load_trials(key, root) if t["status"] == "ok"]
    return np.array([v for v in vals if v is not None and np.isfinite(v)], dtype=float)


def load_trial_series(key: TrialKey, root: Path = LEDGER_DIR) -> pd.DataFrame:
    """T x N matrix of daily net returns of the ok trials that stored a series (for PBO)."""
    cols = {}
    for t in load_trials(key, root):
        if t["status"] == "ok" and t.get("series"):
            cols[t["trial_id"]] = pd.read_parquet(Path(root) / t["series"])["ret"]
    return pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()
