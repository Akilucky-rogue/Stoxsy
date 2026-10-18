"""The ONE training-dataset builder: snapshot features + registry labels under an explicit price policy.

    ds = build_labelled_dataset("swing_5d")                     # official prices, label tb_nextopen_v1
    ds.frame            features + label columns (ret_net, label, label_end_session, r1..rh ...)
    ds.feature_cols     model inputs (NaN preserved - tree models handle it; deep models must impute)
    ds.meta()           provenance for model cards / the ledger

Replaces the legacy `models.dataset.build_training_dataset` labelling (entry at the close of t, bar t inside the
window). Everything downstream - CPCV, the harness, the ledger, the cards - consumes this object, so training,
paper resolution and evaluation share one label/cost/horizon definition (qsde.ml.registry).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from qsde.ml import data as mldata
from qsde.ml.registry import HorizonSpec, get_horizon

log = logging.getLogger(__name__)

NON_FEATURE = ("symbol", "as_of_date", "known_at")


@dataclass
class LabelledDataset:
    spec: HorizonSpec
    frame: pd.DataFrame
    feature_cols: list
    calendar: pd.DatetimeIndex
    prices: pd.DataFrame
    snapshot_ids: list
    universe_version: str
    factor_version: str
    dataset_hash: str
    price_policy: str
    exclusion_events: int
    n_symbols: int
    notes: list = field(default_factory=list)

    def meta(self) -> dict:
        return {"horizon": self.spec.id, "label_id": self.spec.label, "snapshot_ids": self.snapshot_ids,
                "dataset_hash": self.dataset_hash[:16], "universe_version": self.universe_version,
                "factor_version": self.factor_version, "n_symbols": self.n_symbols,
                "price_policy": self.price_policy, "exclusion_events": self.exclusion_events,
                "rows": int(len(self.frame)), "n_features": len(self.feature_cols), "notes": self.notes}

    def model_frame(self, kind: str = "tree"):
        """Frame + report with features as the model family should see them (heavy tails ranked per date for linear/deep)."""
        from qsde.ml.transforms import model_matrix
        return model_matrix(self.frame, self.feature_cols, kind)

    @property
    def adjustment_complete(self) -> bool:
        """False under the research policy: low-confidence price-inferred events were applied."""
        return self.price_policy == mldata.OFFICIAL


def build_labelled_dataset(horizon: str | HorizonSpec, snapshot_ids: Optional[list] = None,
                           universe_version: Optional[str] = None, policy: str = mldata.OFFICIAL,
                           side: int = 1, start: Optional[str] = None, end: Optional[str] = None,
                           cost_bps: Optional[float] = None, factor_version: Optional[str] = None,
                           feats: Optional[pd.DataFrame] = None, prices: Optional[pd.DataFrame] = None,
                           exclusions: Optional[pd.DataFrame] = None, snapshot=None,
                           symbols: Optional[list] = None) -> LabelledDataset:
    """Features from the Parquet store, labels from `qsde.ml.labels.make_labels`.

    `feats`/`prices`/`exclusions`/`snapshot` can be injected for tests; in normal use they are read from the
    store and the DB under `policy` (see qsde.ml.data).
    """
    from qsde.factors.versions import FFD_VERSION
    from qsde.features.store import current_snapshots, read_features
    from qsde.ml.labels import make_labels

    spec = get_horizon(horizon) if isinstance(horizon, str) else horizon
    factor_version = factor_version or FFD_VERSION
    if feats is None:
        snaps = current_snapshots(factor_version)
        if snapshot_ids:
            snaps = [s for s in snaps if s.snapshot_id in set(snapshot_ids)] or snaps
        if not snaps:
            raise RuntimeError(f"no current feature snapshot for {factor_version}")
        snapshot = snaps[-1] if len(snaps) == 1 or not snapshot_ids else None
        ids = [snapshot.snapshot_id] if snapshot is not None else [s.snapshot_id for s in snaps]
        feats = read_features(factor_version, start=start, end=end, snapshot_ids=ids,
                              universe_version=universe_version).drop(columns=["known_at"], errors="ignore")
        snapshot = snapshot or snaps[-1]
    feats = feats.copy()
    feats["as_of_date"] = pd.to_datetime(feats["as_of_date"])
    if symbols is not None:                       # explicit subset (smoke runs): the list is part of the provenance
        feats = feats[feats["symbol"].isin(set(symbols))]
    syms = sorted(feats["symbol"].unique())

    if prices is None:
        prices = mldata.load_prices(syms, policy)
    if exclusions is None:
        exclusions = mldata.label_exclusion_events(syms, policy)
    cal = pd.DatetimeIndex(sorted(pd.to_datetime(prices["date"]).unique()))

    ev = feats[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"})
    lab = make_labels(prices, ev, spec, side=side, cost_bps=cost_bps, unexplained=exclusions)
    frame = lab.merge(feats, on=["symbol", "as_of_date"], how="inner").reset_index(drop=True)
    fcols = [c for c in feats.columns if c not in NON_FEATURE]
    notes = []
    if symbols is not None:
        notes.append(f"restricted to {len(symbols)} explicitly listed symbols")
    const = [c for c in fcols if frame[c].nunique(dropna=True) <= 1]
    if const:                                   # the snapshot validator only rejects all-NaN columns
        fcols = [c for c in fcols if c not in const]
        notes.append(f"constant features excluded from the model inputs: {const}")
    if policy != mldata.OFFICIAL:
        notes.append("RESEARCH price policy: low-confidence price-inferred corporate-action events were applied; "
                     "corporate-action adjustment is incomplete - not eligible for promotion")
    log.info("dataset %s: %d labelled rows, %d features, %d symbols, policy=%s, exclusion events=%d",
             spec.id, len(frame), len(fcols), len(syms), policy, len(exclusions))
    return LabelledDataset(
        spec=spec, frame=frame, feature_cols=fcols, calendar=cal, prices=prices,
        snapshot_ids=[snapshot.snapshot_id] if snapshot is not None else [],
        universe_version=getattr(snapshot, "universe_version", "injected"),
        factor_version=factor_version, dataset_hash=getattr(snapshot, "dataset_hash", "injected"),
        price_policy=policy, exclusion_events=int(len(exclusions)), n_symbols=len(syms), notes=notes)
