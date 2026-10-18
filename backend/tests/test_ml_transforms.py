import numpy as np
import pandas as pd
import pytest

from qsde.ml.transforms import heavy_tailed_columns, model_matrix, per_date_rank_gauss


def _frame(n_dates=40, n_sym=30, seed=0):
    rng = np.random.default_rng(seed)
    d = np.repeat(pd.bdate_range("2024-01-01", periods=n_dates), n_sym)
    x = rng.normal(size=len(d))
    heavy = rng.normal(size=len(d))
    heavy[::600] = 400.0                                   # two absurd distances
    return pd.DataFrame({"as_of_date": d, "symbol": np.tile(np.arange(n_sym), n_dates), "ok": x, "smc_x_dist": heavy})


def test_only_heavy_tailed_columns_are_flagged():
    f = _frame()
    assert heavy_tailed_columns(f, ["ok", "smc_x_dist"]) == ["smc_x_dist"]


def test_rank_gauss_is_bounded_per_date_and_keeps_nan():
    f = _frame()
    f.loc[3, "smc_x_dist"] = np.nan
    g = per_date_rank_gauss(f, ["smc_x_dist"])
    assert np.isnan(g.loc[3, "smc_x_dist"]) and g["smc_x_dist"].abs().max() <= 4.0
    per_date_mean = g.groupby("as_of_date")["smc_x_dist"].mean().abs().max()
    assert per_date_mean < 0.1                             # centred within every date


def test_transform_uses_only_same_date_information():
    f = _frame()
    g1 = per_date_rank_gauss(f, ["smc_x_dist"])
    f2 = f.copy()
    last = f2["as_of_date"].max()
    f2.loc[f2["as_of_date"] == last, "smc_x_dist"] *= 1000      # change only the final date
    g2 = per_date_rank_gauss(f2, ["smc_x_dist"])
    earlier = f["as_of_date"] < last
    assert np.allclose(g1.loc[earlier, "smc_x_dist"], g2.loc[earlier, "smc_x_dist"], equal_nan=True)


def test_tree_gets_raw_and_linear_gets_transformed_without_mutating_the_input():
    f = _frame()
    raw = f["smc_x_dist"].copy()
    t, rep_t = model_matrix(f, ["ok", "smc_x_dist"], "tree")
    lin, rep_l = model_matrix(f, ["ok", "smc_x_dist"], "linear")
    assert t["smc_x_dist"].equals(raw) and rep_t["transformed"] == []
    assert rep_l["transformed"] == ["smc_x_dist"] and lin["smc_x_dist"].abs().max() <= 4.0
    assert f["smc_x_dist"].equals(raw)
    with pytest.raises(ValueError):
        model_matrix(f, ["ok"], "svm")
