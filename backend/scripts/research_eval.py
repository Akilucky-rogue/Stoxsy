"""Full-universe RESEARCH evaluation on a snapshot (rank-gauss target, shuffled-label null control, cost stress, variants, baselines).

RESEARCH_ONLY: nothing here is saved as a candidate or promoted, and the output is not an edge claim. One call = one trial on the
real ledger key, so run it deliberately.

    python scripts/research_eval.py --factor-version ffd_v2s --snapshot <id> --horizons swing_5d position_20d --reviewed-snapshot <id>

Variants evaluated on the median CPCV path of the model score (net of 10/25/30 bps, same sleeves and cost model as the harness):
  model long-only top-q | model long-short top/bottom-q (short side labelled with side=-1) | model residualised on ATR% (low-vol-controlled)
Baselines: random, equal-weight, 12-1 momentum (= the Tier-1 Jegadeesh-Titman score), MOP time-series momentum, low-vol (-ATR%).
Slices: universe regime and PIT liquidity tier (traded-value rank from universe_pit).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from qsde.ml import eval as ev
from qsde.ml.dataset import build_labelled_dataset
from qsde.ml.experiment import TOP_Q, MIN_NAMES, lightgbm_fit_predict, run_experiment
from qsde.ml.labels import make_labels
from qsde.ml.registry import COST_STRESS_BPS, get_horizon

log = logging.getLogger("research_eval")
CARDS = Path(r"D:\Stoxsy\data\reports\model_cards")
OUT = Path(r"D:\Stoxsy\data\reports\research")
BANNER = ("> **RESEARCH_ONLY.** Full-universe evaluation for deciding what to build next. Promotion is disabled "
          "(`QSDE_ML_PROMOTION_ENABLED=false`); nothing was saved as a candidate; this is not evidence of a tradable edge until the "
          "owner has reviewed it together with the data-quality report and the corporate-action reconciliation.")


def _series_by_cost(fn, costs):
    return {c: fn(c) for c in costs}


def _row(name, by_cost, extra=""):
    cells = []
    for c in sorted(by_cost):
        p = ev.perf(by_cost[c])
        cells.append(f"{p['sharpe_ann']:+.2f} / {p['mean_ann'] * 100:+.1f}%")
    last = ev.perf(by_cost[max(by_cost)])
    return f"| {name} | " + " | ".join(cells) + f" | {last['max_dd']:.2f} | {extra} |"


def _mop_scores(prices: pd.DataFrame) -> pd.DataFrame:
    from qsde.research.rule_engine import compute_mop_panel
    c = prices.pivot(index="date", columns="symbol", values="close").sort_index()
    c.index = pd.to_datetime(c.index)
    s = compute_mop_panel(c).stack().rename("score").reset_index()
    s.columns = ["as_of_date", "symbol", "score"]
    return s


def _residualise(d: pd.DataFrame, score: str, ctrl: str) -> pd.Series:
    """Per-date OLS residual of the percent-rank of `score` on the percent-rank of `ctrl` (removes the linear low-vol exposure)."""
    g = d.groupby("as_of_date")
    xs, xc = g[score].rank(pct=True), g[ctrl].rank(pct=True)
    xs_m, xc_m = xs.groupby(d["as_of_date"]).transform("mean"), xc.groupby(d["as_of_date"]).transform("mean")
    a, b = xs - xs_m, xc - xc_m
    cov = (a * b).groupby(d["as_of_date"]).transform("sum")
    var = (b * b).groupby(d["as_of_date"]).transform("sum").replace(0, np.nan)
    return (a - (cov / var) * b).rename("resid")


def evaluate_variants(ds, score: np.ndarray, spec) -> dict:
    h = spec.label_horizon_days
    cal = ds.calendar
    lab = ds.frame.copy()
    lab["score"] = score
    d = lab.dropna(subset=["score"]).copy()
    costs = sorted(set(COST_STRESS_BPS))
    rows, sections = [], {}
    uret = ev.universe_daily_return(ds.prices)

    def long_only(frame, col="score", minn=MIN_NAMES, q=TOP_Q):
        return _series_by_cost(lambda c: ev.run_portfolio(frame, cal, h, q, minn, c, col), costs)

    # model long-only
    model_long = long_only(d)
    rows.append(_row("model long-only top-q", model_long))
    # model residualised on ATR%
    if "tech_atr_pct" in d:
        d["score_res"] = _residualise(d, "score", "tech_atr_pct")
        rows.append(_row("model residualised on ATR% (low-vol-controlled), long-only", long_only(d, "score_res")))
        expo = ev.spearman_by_date(d.assign(neg_atr=-d["tech_atr_pct"]), "score", "neg_atr").mean()
    else:
        expo = float("nan")
    # long-short (short side needs its own side=-1 labels: the long-side path is not a short path)
    ls_note = ""
    try:
        n = d.groupby("as_of_date")["score"].transform("size")
        rk = d.groupby("as_of_date")["score"].rank(method="first", ascending=True)
        bottom = d[(n >= MIN_NAMES) & (rk <= np.maximum(1, np.round(TOP_Q * n)))]
        sh = make_labels(ds.prices, bottom[["symbol", "as_of_date"]].rename(columns={"as_of_date": "date"}), spec, side=-1,
                         drop_contaminated=False, keep_paths=True)
        sh = sh.merge(bottom[["symbol", "as_of_date"]], on=["symbol", "as_of_date"])
        sh["score"] = 1.0

        def ls(c):
            lo = ev.run_portfolio(d, cal, h, TOP_Q, MIN_NAMES, c, "score")
            so = ev.run_portfolio(sh, cal, h, 1.0, 1, c, "score")                       # all bottom names, short side labels
            both = pd.concat([lo, so], axis=1, keys=["l", "s"]).dropna()
            return 0.5 * (both["l"] + both["s"])
        rows.append(_row("model long-short top/bottom-q (gross 1, shorts at the delivery cost model - optimistic)",
                         _series_by_cost(ls, costs)))
        ls_note = "Shorting cash equities overnight is not available to a retail account; this leg would need futures (not costed here)."
    except Exception as e:  # noqa: BLE001
        ls_note = f"long-short not computed: {e}"
    # baselines
    base = []
    rng = np.random.default_rng(0)
    rnd = d.assign(score_r=rng.random(len(d)))
    base.append(_row("random top-q", long_only(rnd, "score_r")))
    base.append(_row("equal-weight universe", long_only(d.assign(one=1.0), "one", q=1.0)))
    mom = ev.momentum_12_1(ds.prices)
    dm = d.drop(columns=["score"]).merge(mom, on=["as_of_date", "symbol"])
    base.append(_row("12-1 momentum top-q (= Tier-1 Jegadeesh-Titman score)", long_only(dm)))
    mo = d.drop(columns=["score"]).merge(_mop_scores(ds.prices), on=["as_of_date", "symbol"])
    base.append(_row("MOP time-series momentum top-q (Tier-1)", long_only(mo.dropna(subset=["score"]))))
    if "tech_atr_pct" in d:
        base.append(_row("low-vol (-ATR%) top-q", long_only(d.assign(lv=-d["tech_atr_pct"]), "lv")))
    # regime slices of the headline series
    head = model_long[max(costs)]
    reg = ev.regime_slices(head, uret) if len(head) > 100 else {}
    # liquidity tiers
    tiers = {}
    try:
        from qsde.db.connection import read_sql
        u = read_sql("SELECT date, symbol, rank FROM universe_pit WHERE method = 'adv60_top500_v1'")
        u["as_of_date"] = pd.to_datetime(u["date"])
        dt = d.merge(u[["as_of_date", "symbol", "rank"]], on=["as_of_date", "symbol"], how="left")
        for name, lo_, hi_ in (("rank 1-100", 1, 100), ("rank 101-250", 101, 250), ("rank 251-500", 251, 500)):
            t = dt[(dt["rank"] >= lo_) & (dt["rank"] <= hi_)]
            if t["as_of_date"].nunique() < 100:
                continue
            ic = ev.spearman_by_date(t, "score", "ret_net", min_names=10)
            s = ev.run_portfolio(t, cal, h, TOP_Q, 10, max(costs))
            tiers[name] = {"ic": float(ic.mean()), "sharpe": ev.perf(s)["sharpe_ann"], "names_med": float(t.groupby("as_of_date").size().median())}
    except Exception as e:  # noqa: BLE001
        tiers = {"error": str(e)}
    return {"rows": rows, "base": base, "regimes": reg, "tiers": tiers, "lowvol_exposure_ic": float(expo), "ls_note": ls_note,
            "costs": costs}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--factor-version", default="ffd_v2s")
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--reviewed-snapshot", required=True, help="must equal --snapshot: proves the DQ report was read")
    ap.add_argument("--horizons", nargs="+", default=["swing_5d", "position_20d"])
    ap.add_argument("--groups", type=int, default=10)
    ap.add_argument("--k", type=int, default=2)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.reviewed_snapshot != args.snapshot:
        log.error("review gate: --reviewed-snapshot must equal --snapshot")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    for hid in args.horizons:
        spec = get_horizon(hid)
        log.info("== %s: dataset (full universe, official prices, %s, %s)", hid, args.factor_version, args.snapshot)
        ds = build_labelled_dataset(spec, policy="official", factor_version=args.factor_version, snapshot_ids=[args.snapshot])
        log.info("dataset: %d rows, %d symbols, %d features, exclusion events %d", len(ds.frame), ds.n_symbols, len(ds.feature_cols),
                 ds.exclusion_events)
        params = {"n_estimators": 150, "num_leaves": 15, "purpose": "research_full_universe"}
        card = CARDS / f"research_{hid}_{args.factor_version}_{args.snapshot}.md"
        exp = run_experiment(ds, lightgbm_fit_predict(**{k: v for k, v in params.items() if k != "purpose"}), "lightgbm", params,
                             groups=args.groups, k=args.k, card_path=card)
        card.write_text(BANNER + "\n\n" + card.read_text(encoding="utf-8"), encoding="utf-8")
        log.info("%s: verdict %s, per-path Sharpe %s, null %s", hid, exp["verdict"], exp["per_path_sharpe"], exp["null_control"])
        res = evaluate_variants(ds, exp["median_scores"], spec)
        m = exp["median"]
        L = [f"# RESEARCH_ONLY full-universe evaluation: {hid} on {args.factor_version} `{args.snapshot}`", "", BANNER, "",
             f"- dataset: {len(ds.frame):,} labelled rows, {ds.n_symbols} symbols, {len(ds.feature_cols)} features, universe `{ds.universe_version}`, "
             f"price policy `{ds.price_policy}`, label exclusion events {ds.exclusion_events}, dataset hash `{ds.dataset_hash[:16]}`",
             f"- target `cs_rank_gauss`; CPCV {args.groups}x{args.k}; harness verdict (informational): **{exp['verdict']}**; evidence class {m['evidence_class']}",
             f"- OOF rank IC {m['ic']['mean']:+.4f} (ICIR {m['ic']['icir']:.2f}, NW t {m['ic']['t_nw']:.1f}); null control "
             f"{exp['null_control']['null_ic_mean']:+.4f} ({'OK' if exp['null_control']['ok'] else 'BIASED'}, limit +/-{exp['null_control']['limit']:.3f}); "
             f"PBO {exp['pbo']}; DSR {m['dsr']['dsr']:.3f} (N={m['dsr']['n_trials']}, {m['dsr']['variance_source']})",
             f"- rank correlation of the model score with low volatility (-ATR%), mean per date: **{res['lowvol_exposure_ic']:+.3f}**",
             f"- per-path net Sharpe at the headline cost: {exp['per_path_sharpe']}", "",
             "## Net Sharpe / mean annual return at 10 / 25 / 30 bps (median CPCV path)", "",
             "| variant | " + " | ".join(f"{c:g} bps" for c in res["costs"]) + " | max DD @30 | note |",
             "|---|" + "---|" * (len(res["costs"]) + 2), *res["rows"], "", "### Baselines (same sleeves, same cost)", "",
             "| baseline | " + " | ".join(f"{c:g} bps" for c in res["costs"]) + " | max DD @30 | note |",
             "|---|" + "---|" * (len(res["costs"]) + 2), *res["base"], "", res["ls_note"], "",
             "## Regimes (model long-only, headline cost)", "", "| regime | days | Sharpe |", "|---|---|---|",
             *[f"| {k} | {v['n_days']} | {v['sharpe_ann']:+.2f} |" for k, v in res["regimes"].items()], "",
             "## Liquidity tiers (point-in-time traded-value rank; model IC and long-only Sharpe within the tier)", "",
             "| tier | median names/date | IC | Sharpe @30 |", "|---|---|---|---|",
             *[f"| {k} | {v['names_med']:.0f} | {v['ic']:+.4f} | {v['sharpe']:+.2f} |" for k, v in res["tiers"].items() if k != "error"], ""]
        (OUT / f"research_eval_{hid}_{args.factor_version}_{args.snapshot}.md").write_text("\n".join(L), encoding="utf-8")
        log.info("report written for %s", hid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
