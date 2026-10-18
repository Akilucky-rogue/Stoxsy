"""Cross-validation for overlapping financial labels (ADR 0001, AUDIT M6).

  * Splits are made on DATE blocks (never inside a date), so one date cannot
    straddle train and test.
  * PURGE is per sample: a train sample is dropped if its own label span
    [as_of session, label_end session] overlaps the span of ANY test sample.
    The registry's `purge_days` is only a floor / sanity check.
  * EMBARGO is counted in market SESSIONS (not calendar days) after each test block.
  * `cpcv_*` implements Combinatorial Purged CV: N date groups, k test groups per
    split -> C(N,k) splits and k*C(N,k)/N backtest paths, each a full-sample
    out-of-sample prediction series.
"""
from __future__ import annotations

from itertools import combinations
from math import comb
from typing import Callable, Iterator, Optional, Sequence

import numpy as np
import pandas as pd


def session_positions(dates: Sequence) -> pd.Series:
    """date -> market-session position, from the sorted unique dates present."""
    u = pd.DatetimeIndex(sorted(pd.to_datetime(pd.Series(dates)).unique()))
    return pd.Series(np.arange(len(u)), index=u)


def _covered(n_pos: int, starts: np.ndarray, ends: np.ndarray, extra_after: int = 0) -> np.ndarray:
    """Boolean mask over session positions covered by any [start, end(+extra_after)] span."""
    diff = np.zeros(n_pos + 2, dtype=np.int64)
    np.add.at(diff, starts, 1)
    np.add.at(diff, np.minimum(ends + extra_after + 1, n_pos + 1), -1)
    return np.cumsum(diff[:-1])[:n_pos] > 0


def _overlaps(mask: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """For each [start,end] span: does it touch any True position of `mask`?"""
    pref = np.concatenate([[0], np.cumsum(mask.astype(np.int64))])
    return (pref[ends + 1] - pref[starts]) > 0


def purge_train_indices(test_idx: np.ndarray, t0: np.ndarray, t1: np.ndarray,
                        n_pos: int, embargo: int, candidate_train: Optional[np.ndarray] = None) -> np.ndarray:
    """Train indices after purging overlap with the test spans and embargoing after each test span.

    t0/t1: session positions of every sample's as-of date and label end.
    """
    cand = np.arange(len(t0)) if candidate_train is None else candidate_train
    cand = np.setdiff1d(cand, test_idx, assume_unique=False)
    cov = _covered(n_pos, t0[test_idx], t1[test_idx], extra_after=embargo)
    bad = _overlaps(cov, t0[cand], t1[cand])
    return cand[~bad]


def _blocks(sess: np.ndarray, n_blocks: int) -> list[np.ndarray]:
    """Contiguous blocks of DATES (session positions), each returned as an index array of samples."""
    uniq = np.unique(sess)
    edges = np.array_split(uniq, n_blocks)
    return [np.where(np.isin(sess, e))[0] for e in edges if len(e)]


def purged_group_kfold(t0: np.ndarray, t1: np.ndarray, n_splits: int = 5, embargo: int = 5,
                       min_purge: Optional[int] = None) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield (train_idx, test_idx) with date-block folds and per-sample purge + session embargo."""
    n_pos = int(max(t1.max(), t0.max())) + 1
    bad = int(((t1 - t0) < 0).sum())
    if bad:
        raise ValueError(f"{bad} samples end before they start")
    for test in _blocks(t0, n_splits):
        train = purge_train_indices(test, t0, t1, n_pos, embargo)
        yield train, test


def cpcv_splits(t0: np.ndarray, t1: np.ndarray, n_groups: int = 10, k_test: int = 2,
                embargo: int = 5) -> list[dict]:
    """All C(n_groups, k_test) splits as dicts {test_groups, train_idx, test_idx}."""
    n_pos = int(max(t1.max(), t0.max())) + 1
    groups = _blocks(t0, n_groups)
    out = []
    for combo in combinations(range(len(groups)), k_test):
        test = np.sort(np.concatenate([groups[g] for g in combo]))
        train = purge_train_indices(test, t0, t1, n_pos, embargo)
        out.append({"test_groups": combo, "train_idx": train, "test_idx": test})
    return out


def cpcv_paths(n_groups: int = 10, k_test: int = 2) -> list[dict[int, int]]:
    """Backtest paths: list of {group -> split_index}. A path takes, for every group, the
    prediction from one split that had that group in its test set; there are
    k*C(N,k)/N = C(N-1,k-1) paths and each (split, group) pair is used exactly once."""
    combos = list(combinations(range(n_groups), k_test))
    per_group: dict[int, list[int]] = {g: [] for g in range(n_groups)}
    for si, c in enumerate(combos):
        for g in c:
            per_group[g].append(si)
    n_paths = comb(n_groups - 1, k_test - 1)
    return [{g: per_group[g][p] for g in range(n_groups)} for p in range(n_paths)]


FitPredict = Callable[[np.ndarray, np.ndarray, Optional[np.ndarray], np.ndarray], np.ndarray]


def run_cpcv(X: np.ndarray, y: np.ndarray, t0: np.ndarray, t1: np.ndarray, fit_predict: FitPredict,
             weights: Optional[np.ndarray] = None, n_groups: int = 10, k_test: int = 2,
             embargo: int = 5, min_train: int = 200) -> dict:
    """Run a model through every CPCV split and stitch the backtest paths.

    fit_predict(X_train, y_train, w_train, X_test) -> scores for X_test.
    Returns {'paths': [array of OOS scores, len == n samples], 'splits': [...], 'n_paths': int,
             'groups': per-sample group id, 'skipped_splits': int}.
    """
    splits = cpcv_splits(t0, t1, n_groups, k_test, embargo)
    groups = np.full(len(t0), -1, dtype=int)
    for g, idx in enumerate(_blocks(t0, n_groups)):
        groups[idx] = g
    pred_by_split: dict[int, np.ndarray] = {}
    skipped = 0
    for si, sp in enumerate(splits):
        if len(sp["train_idx"]) < min_train:
            skipped += 1
            continue
        w = None if weights is None else weights[sp["train_idx"]]
        pred_by_split[si] = fit_predict(X[sp["train_idx"]], y[sp["train_idx"]], w, X[sp["test_idx"]])
    paths = []
    for path in cpcv_paths(n_groups, k_test):
        s = np.full(len(t0), np.nan)
        for g, si in path.items():
            if si not in pred_by_split:
                continue
            sp = splits[si]
            in_g = groups[sp["test_idx"]] == g
            s[sp["test_idx"][in_g]] = pred_by_split[si][in_g]
        paths.append(s)
    return {"paths": paths, "splits": splits, "n_paths": len(paths), "groups": groups,
            "skipped_splits": skipped}


def average_uniqueness(symbols: np.ndarray, t0: np.ndarray, t1: np.ndarray) -> np.ndarray:
    """AFML ch.4 average uniqueness of each label span within its own symbol (1 = no overlap)."""
    w = np.ones(len(t0))
    n_pos = int(max(t1.max(), t0.max())) + 2
    for sym in np.unique(symbols):
        m = np.where(symbols == sym)[0]
        diff = np.zeros(n_pos + 1)
        np.add.at(diff, t0[m], 1)
        np.add.at(diff, t1[m] + 1, -1)
        conc = np.cumsum(diff)[:n_pos]
        inv = np.where(conc > 0, 1.0 / np.maximum(conc, 1), 0.0)
        pref = np.concatenate([[0.0], np.cumsum(inv)])
        w[m] = (pref[t1[m] + 1] - pref[t0[m]]) / (t1[m] - t0[m] + 1)
    return w
