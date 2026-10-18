"""Evaluate the defensive allocation signal as PORTFOLIO CONSTRUCTION (risk reduction), not as a return predictor.

    python scripts/defensive_eval.py            # backtest + report -> data/reports/defensive/
    python scripts/defensive_eval.py --latest   # also write the latest scores for the cockpit -> data/signals/defensive/

Universe: the point-in-time top-500 by traded value (universe_pit). Monthly (21-session) rebalance, weights set from the close of day t and
executed at the next close, 12.5 bps one way (25 bps round trip, the registry delivery cost) on traded notional, daily returns winsorised at +/-40%.
Compared with equal-weight on the same universe, same rebalance, same costs. Paired block bootstrap for the differences. A smaller drawdown or
volatility is the expected, mechanical result of a low-risk tilt; the question the report answers is whether risk-adjusted return survives costs.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

from qsde.defensive import signal as dfs

OUT = Path(r"D:\Stoxsy\data\reports\defensive")
SIG = Path(r"D:\Stoxsy\data\signals\defensive")
SCHEMES = ("ew", "tilt", "tilt_gated", "top_defensive")


def _paired_bootstrap(a: pd.Series, b: pd.Series, block: int = 21, n: int = 500, seed: int = 0) -> dict:
    d = pd.concat([a, b], axis=1, keys=["a", "b"]).dropna()
    x = d.to_numpy()
    T = len(x)
    rng = np.random.default_rng(seed)
    nb = int(np.ceil(T / block))
    ds, dd, dv = [], [], []
    for _ in range(n):
        idx = np.concatenate([np.arange(s, s + block) for s in rng.integers(0, T - block + 1, nb)])[:T]
        xa, xb = x[idx, 0], x[idx, 1]
        sa = xa.mean() / xa.std() * np.sqrt(252)
        sb = xb.mean() / xb.std() * np.sqrt(252)
        ea, eb = np.cumprod(1 + xa), np.cumprod(1 + xb)
        ds.append(sa - sb)
        dd.append((ea / np.maximum.accumulate(ea) - 1).min() - (eb / np.maximum.accumulate(eb) - 1).min())
        dv.append(xa.std() / xb.std())
    q = lambda v: (float(np.percentile(v, 2.5)), float(np.percentile(v, 97.5)))      # noqa: E731
    return {"sharpe_diff": q(ds), "maxdd_diff": q(dd), "vol_ratio": q(dv)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--latest", action="store_true")
    args = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.ml import data as mldata

    px = mldata.load_prices(policy="official")
    uni = read_sql("SELECT date, symbol, rank FROM universe_pit WHERE method = 'adv60_top500_v1'")
    uni["date"] = pd.to_datetime(uni["date"])
    feat = dfs.risk_features(px)
    sc = dfs.defensive_scores(feat, eligible=uni, with_warnings=False)
    close = px.pivot(index="date", columns="symbol", values="close").sort_index()
    uret = close.pct_change().clip(-0.4, 0.4).mean(axis=1)
    reg = dfs.regime_state(uret)
    runs = {s: dfs.backtest(px, sc, reg, s) for s in SCHEMES}
    ew = runs["ew"][0]
    OUT.mkdir(parents=True, exist_ok=True)
    L = ["# Defensive allocation signal - portfolio-construction evaluation (RESEARCH_ONLY)", "",
         "This is risk-aware portfolio construction, not a return forecast. Universe: PIT top-500 by traded value; monthly rebalance; weights from the close of t executed at the "
         "next close; 12.5 bps one way on traded notional; daily returns winsorised at +/-40%. Compared with equal-weight on the same universe and rebalance.", "",
         f"- period {ew.index.min().date()} .. {ew.index.max().date()} ({len(ew)} sessions); scored symbol-days {len(sc):,}; median names per date {int(sc.groupby('date').size().median())}", "",
         "| scheme | ann return | ann vol | Sharpe | max DD | Calmar | downside dev | worst month | turnover/yr (one-way x2) |", "|---|---|---|---|---|---|---|---|---|"]
    for k, (r, t) in runs.items():
        s = dfs.stats(r)
        L.append(f"| {k} | {s['ann_return']:+.1%} | {s['ann_vol']:.1%} | {s['sharpe']:.2f} | {s['max_dd']:.1%} | {s['calmar']:.2f} | {s['downside_dev']:.1%} | "
                 f"{s['worst_month']:.1%} | {t.sum() / (len(t) / 252):.1f} |")
    L += ["", "## Differences vs equal-weight (paired block bootstrap, 95% interval, 21-session blocks)", "",
          "| scheme | Sharpe diff | max DD diff (less negative = better) | vol ratio |", "|---|---|---|---|"]
    for k in SCHEMES[1:]:
        b = _paired_bootstrap(runs[k][0], ew)
        pt = (dfs.stats(runs[k][0])["sharpe"] - dfs.stats(ew)["sharpe"], dfs.stats(runs[k][0])["max_dd"] - dfs.stats(ew)["max_dd"],
              dfs.stats(runs[k][0])["ann_vol"] / dfs.stats(ew)["ann_vol"])
        L.append(f"| {k} | {pt[0]:+.2f} [{b['sharpe_diff'][0]:+.2f}, {b['sharpe_diff'][1]:+.2f}] | {pt[1]:+.1%} [{b['maxdd_diff'][0]:+.1%}, {b['maxdd_diff'][1]:+.1%}] | "
                 f"{pt[2]:.2f} [{b['vol_ratio'][0]:.2f}, {b['vol_ratio'][1]:.2f}] |")
    # downside capture and regimes
    base = ew
    worst = base <= base.quantile(0.2)
    L += ["", "## Downside behaviour", "", "| scheme | return on the worst 20% equal-weight days (vs ew) | return in risk-off regime days | return in risk-on days |", "|---|---|---|---|"]
    rg = reg["regime"].reindex(base.index)
    for k, (r, _) in runs.items():
        L.append(f"| {k} | {r[worst].mean() * 1e4:+.1f} bps/day ({r[worst].mean() / base[worst].mean():.2f}x) | "
                 f"{r[rg == 'risk_off'].mean() * 1e4:+.1f} bps/day (n={int((rg == 'risk_off').sum())}) | {r[rg == 'risk_on'].mean() * 1e4:+.1f} bps/day (n={int((rg == 'risk_on').sum())}) |")
    L += ["", "## By calendar year (return / max drawdown)", "", "| year | " + " | ".join(SCHEMES) + " |", "|---|" + "---|" * len(SCHEMES)]
    for y in sorted(set(ew.index.year)):
        cells = []
        for k in SCHEMES:
            r = runs[k][0][runs[k][0].index.year == y]
            eq = (1 + r).cumprod()
            cells.append(f"{eq.iloc[-1] - 1:+.1%} / {(eq / eq.cummax() - 1).min():.1%}")
        L.append(f"| {y} | " + " | ".join(cells) + " |")
    L += ["", "## Cost sensitivity (tilt vs ew, Sharpe)", "", "| one-way bps | ew | tilt |", "|---|---|---|"]
    for c in (0.0, 12.5, 25.0):
        a = dfs.stats(dfs.backtest(px, sc, reg, "ew", one_way_cost_bps=c)[0])["sharpe"]
        b = dfs.stats(dfs.backtest(px, sc, reg, "tilt", one_way_cost_bps=c)[0])["sharpe"]
        L.append(f"| {c:g} | {a:.2f} | {b:.2f} |")
    (OUT / "defensive_eval.md").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))
    if args.latest:
        last = sc[sc["date"] == sc["date"].max()].copy()
        last = dfs.defensive_scores(feat[feat["date"] == last["date"].iloc[0]], eligible=uni[uni["date"] == last["date"].iloc[0]])
        last["regime"] = reg["regime"].iloc[-1]
        SIG.mkdir(parents=True, exist_ok=True)
        last.to_parquet(SIG / "latest.parquet", index=False)
        last.to_csv(SIG / f"defensive_{last['date'].iloc[0].date()}.csv", index=False)
        print(f"\nlatest: {last['date'].iloc[0].date()} regime {last['regime'].iloc[0]} {len(last)} names -> {SIG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
