"""
Training-dataset builder - COMPATIBILITY SHIM over the registry pipeline.

The legacy builder labelled with `models.triple_barrier` (entry at the CLOSE of t, bar t inside the window,
close-of-day forward returns, its own horizon and cost tables). That labelling is gone. This function now delegates to
`qsde.ml.dataset.build_labelled_dataset`, i.e.

    features  : Parquet snapshots (qsde.features.store), NaN preserved, point-in-time
    labels    : registry `tb_nextopen_v1` - entry at the OPEN of t+1, window t+1..t+h on the market calendar,
                one shared barrier walker (qsde.ml.barrier), registry horizon / cost definitions
    prices    : the OFFICIAL series by default (NSE-confirmed corporate actions only; windows touching
                price-inferred events are excluded); `policy="research"` is explicit and never promotion-grade

Columns returned: symbol, as_of_date, <features...>, plus the label frame (`label` in {-1,0,+1}, `ret_net`,
`ret_gross`, `label_end_session`, r1..rh ...). For legacy consumers `target` is the barrier label and `target_ret`
the NET barrier return. New code should call `build_labelled_dataset` / `run_experiment` directly.
"""

import logging
from typing import Literal, Optional

import pandas as pd

from qsde.factors.versions import FFD_VERSION

log = logging.getLogger(__name__)

IntradayTarget = Literal["close_to_close", "open_to_close"]
LabelMethod = Literal["fixed_horizon", "triple_barrier"]


def build_training_dataset(
    horizon: str = "swing",
    start_date: str = "2018-01-01",
    end_date: Optional[str] = None,
    intraday_target: IntradayTarget = "open_to_close",
    label_method: LabelMethod = "triple_barrier",
    apply_fracdiff: bool = False,
    fracdiff_d: float = 0.4,
    factor_version: str = FFD_VERSION,
    snapshot_ids: Optional[list] = None,
    universe_version: Optional[str] = None,
    policy: str = "official",
) -> pd.DataFrame:
    """Labelled (features, target) frame for `horizon` (registry id or legacy alias: intraday/swing/long)."""
    from qsde.ml.dataset import build_labelled_dataset
    from qsde.ml.registry import get_horizon

    if apply_fracdiff:
        raise ValueError(
            f"factor_version={factor_version!r} rows are already fractionally differenced at ingest; "
            f"apply_fracdiff=True would difference them twice. Leave apply_fracdiff=False.")
    if label_method != "triple_barrier":
        raise ValueError(
            "only the registry label tb_nextopen_v1 exists now; the close-to-close 'fixed_horizon' target was "
            "removed because it is not what paper trading resolves")
    if intraday_target != "open_to_close":
        log.warning("intraday_target=%r ignored: the registry label always enters at the next open", intraday_target)

    spec = get_horizon(horizon)
    ds = build_labelled_dataset(spec, snapshot_ids=snapshot_ids, universe_version=universe_version, policy=policy,
                                start=start_date, end=end_date, factor_version=factor_version)
    out = ds.frame.copy()
    if out.empty:
        return out
    out["target"] = out["label"]
    out["target_ret"] = out["ret_net"]
    out.attrs["dataset_meta"] = ds.meta()
    return out
