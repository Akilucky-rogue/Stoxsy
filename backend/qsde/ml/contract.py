"""Entry-convention contract: labels, Scorecard resolver and paper journal must agree.

`check_entry_contract(ds)` regrades a sample of the labelled dataset three ways and returns what disagrees:
  1. every label enters at the OPEN of the session after the decision (never the decision close)
  2. the Scorecard resolver, given the idea as it would be logged (reference = close of t, levels scaled to it), lands on
     the same fill, status, exit price and net return as the label
  3. the paper-journal path (`levels_at_fill` + the shared barrier walker) lands on the same status and exit price
A non-empty `failures` list means training, paper trading and the Scorecard are grading different things.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from qsde.ml.dataset import LabelledDataset


def check_entry_contract(ds: LabelledDataset, n: int = 300, seed: int = 0) -> dict:
    from qsde.execution.paper_journal import _resolve_barrier_daily
    from qsde.ml.barrier import levels_at_fill
    from qsde.scorecard.resolver import resolve_idea_outcome

    lab, prices, cal = ds.frame, ds.prices.copy(), ds.calendar
    prices["date"] = pd.to_datetime(prices["date"])
    px = prices.set_index(["symbol", "date"]).sort_index()
    h = ds.spec.label_horizon_days
    pos = {d: i for i, d in enumerate(cal)}
    out = {"n_checked": 0, "failures": []}
    if lab.empty:
        return out
    sample = lab.sample(min(n, len(lab)), random_state=seed)
    legacy = ds.spec.legacy
    for r in sample.itertuples():
        i = pos[r.as_of_date]
        out["n_checked"] += 1
        if not r.entry_date > r.as_of_date:
            out["failures"].append((r.symbol, str(r.as_of_date.date()), "entry not after decision"))
            continue
        try:
            bars = px.loc[r.symbol].reindex(cal[i + 1:i + 1 + h]).rename_axis("date").reset_index()
            ref = float(px.loc[(r.symbol, r.as_of_date), "close"])
        except KeyError:
            continue
        if bars[["open", "high", "low", "close"]].isna().any().any():
            continue                                             # a missing bar inside the window: not comparable
        if not np.isclose(bars.iloc[0]["open"], r.entry):
            out["failures"].append((r.symbol, str(r.as_of_date.date()), "label entry is not the next open"))
            continue
        k = ref / r.entry
        # 2. Scorecard
        sc = resolve_idea_outcome(ref, r.stop * k, r.target * k, r.side, legacy, bars)
        if sc is None or sc["status"] != r.status or not np.isclose(sc["exit_price"], r.exit_price) \
                or not np.isclose(sc["entry_fill_price"], r.entry):
            out["failures"].append((r.symbol, str(r.as_of_date.date()), f"scorecard disagrees: {sc}"))
            continue
        # 3. paper journal
        stop_f, tgt_f = levels_at_fill(ref, float(bars.iloc[0]["open"]), r.stop * k, r.target * k)
        status, exit_px, _ = _resolve_barrier_daily(bars, float(bars.iloc[0]["open"]), tgt_f, stop_f, int(r.side))
        if status != r.status or not np.isclose(exit_px, r.exit_price):
            out["failures"].append((r.symbol, str(r.as_of_date.date()), f"paper journal disagrees: {status} {exit_px}"))
    return out
