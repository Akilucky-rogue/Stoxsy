"""Model card generator + verdict rule.

A verdict is only ever produced for results whose `evidence_class` is EVIDENCE_CANDIDATE.
Pilot / synthetic / narrow-universe results get "NOT ASSESSED" and a banner saying so.

Verdict rule (ADR 0001, go-live criteria):
  PROMOTE_TO_PAPER   DSR >= 0.95 AND PBO < 0.5 AND net Sharpe lower 95% bound > 0 AND beats the
                     random and equal-weight baselines at the headline cost
  REJECT             net Sharpe <= 0 or DSR < 0.5
  KEEP_RESEARCHING   everything else
(Capital allocation is a separate, written decision; this only decides what goes to paper trading.)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from qsde.ml.registry import get_horizon


def verdict(result: dict, pbo: Optional[float] = None) -> tuple[str, list[str]]:
    if not result.get("verdict_allowed"):
        return "NOT_ASSESSED", [f"evidence class {result.get('evidence_class')}: " + "; ".join(result.get("evidence_reasons", []))]
    h, d = result["headline"], result["dsr"]
    reasons = []
    if h["sharpe_ann"] <= 0 or d["dsr"] < 0.5:
        return "REJECT", [f"net Sharpe {h['sharpe_ann']:.2f}, DSR {d['dsr']:.2f}"]
    ci_lo = result.get("sharpe_ci", {}).get("sharpe_ann_lo95")
    base = result["baselines"]
    checks = {
        "DSR >= 0.95": d["dsr"] >= 0.95,
        "PBO known and < 0.5": pbo is not None and pbo < 0.5,
        "net Sharpe 95% lower bound > 0": ci_lo is not None and ci_lo > 0,
        "beats random baseline": h["sharpe_ann"] > base["random_top_q"]["sharpe_ann_mean"] + 2 * base["random_top_q"]["sharpe_ann_std"],
        "beats equal-weight universe": h["sharpe_ann"] > base["equal_weight_universe"]["sharpe_ann"],
    }
    reasons = [f"{'PASS' if ok else 'FAIL'}: {k}" for k, ok in checks.items()]
    return ("PROMOTE_TO_PAPER" if all(checks.values()) else "KEEP_RESEARCHING"), reasons


def render_card(result: dict, meta: dict, pbo: Optional[float] = None) -> str:
    v, why = verdict(result, pbo)
    h, ic, d = result["headline"], result["ic"], result["dsr"]
    L = []
    banner = ("> **NOT EVIDENCE - PLUMBING ONLY.** " + "; ".join(result["evidence_reasons"]) +
              ". No verdict is issued from this run.") if not result["verdict_allowed"] else ""
    L += [f"# Model card: {meta.get('model_family', '?')} on {result['horizon']}", ""]
    if banner:
        L += [banner, ""]
    L += [f"**Verdict: {v}**", ""] + [f"- {w}" for w in why] + [""]
    L += ["## Lineage", "",
          f"- generated: {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
          f"- git: `{meta.get('git_sha', 'unknown')}`",
          f"- feature snapshot(s): `{meta.get('snapshot_ids')}`  dataset hash `{meta.get('dataset_hash')}`",
          f"- universe: `{meta.get('universe_version')}` ({meta.get('n_symbols')} symbols)",
          f"- label: `{meta.get('label_id')}`  horizon `{result['horizon']}`  cost model headline {result['cost_bps_headline']:.0f} bps",
          f"- trials under this key (ledger, failures included): **{d['n_trials']}**  (DSR variance source: {d['variance_source']})",
          f"- model params: `{json.dumps(meta.get('params', {}), default=str)}`", ""]
    L += ["## Results (out of sample, combinatorial purged CV paths)", "",
          f"- samples {result['n_samples']:,}, dates {result['n_dates']:,}, median names/date {result['median_names_per_date']:.0f}",
          f"- rank IC mean {ic['mean']:.4f}  ICIR {ic['icir']:.2f}  Newey-West t {ic['t_nw']:.2f}  hit {ic['hit']:.0%}",
          f"- top-{result['top_q']:.0%} long-only, overlapping sleeves: net Sharpe **{h['sharpe_ann']:.2f}**, "
          f"mean {h['mean_ann']:.1%}/yr, vol {h['vol_ann']:.1%}, max drawdown {h['max_dd']:.1%}, n={h['n_days']} days",
          f"- Sharpe 95% bootstrap interval: {result['sharpe_ci'].get('sharpe_ann_lo95', float('nan')):.2f} to "
          f"{result['sharpe_ci'].get('sharpe_ann_hi95', float('nan')):.2f}",
          f"- DSR {d['dsr']:.3f} (N={d['n_trials']}, SR0 daily {d['sr0_daily']:.4f}); PSR vs 0: {d['psr_vs_zero']:.3f}",
          f"- PBO: {'n/a (needs >=2 stored trial series)' if pbo is None else f'{pbo:.2f}'}", ""]
    L += ["### Cost stress (net Sharpe)", "", "| round-trip bps | Sharpe | mean/yr |", "|---|---|---|"]
    for k, p in result["cost_stress"].items():
        L.append(f"| {k} | {p['sharpe_ann']:.2f} | {p['mean_ann']:.1%} |")
    L += ["", "### Baselines (same machinery, same cost)", ""]
    b = result["baselines"]
    L.append(f"- random top-q: {b['random_top_q']['sharpe_ann_mean']:.2f} +/- {b['random_top_q']['sharpe_ann_std']:.2f}")
    L.append(f"- equal-weight universe, same sleeves and costs (turns the whole book over every "
             f"{get_horizon(result['horizon']).label_horizon_days} sessions): {b['equal_weight_universe']['sharpe_ann']:.2f}")
    if "universe_buy_and_hold_gross" in b:
        L.append(f"- universe buy-and-hold, gross, no trading: Sharpe {b['universe_buy_and_hold_gross']['sharpe_ann']:.2f}, "
                 f"mean {b['universe_buy_and_hold_gross']['mean_ann']:.1%}/yr (context only)")
    if "momentum_12_1_top_q" in b:
        L.append(f"- 12-1 momentum top-q: {b['momentum_12_1_top_q']['sharpe_ann']:.2f}")
    if result["regimes"]:
        L += ["", "### Regimes (universe trend / volatility)", "", "| regime | days | Sharpe |", "|---|---|---|"]
        for k, r in result["regimes"].items():
            L.append(f"| {k} | {r['n_days']} | {r['sharpe_ann']:.2f} |")
    if result["flags"]:
        L += ["", "### Flags", ""] + [f"- {f}" for f in result["flags"]]
    L += ["", "## Known limits", "",
          "- Corporate actions: mild bonuses (0.80-0.97), rights, dividends, demergers are not adjusted; "
          "windows touching UNEXPLAINED events are excluded.",
          "- No index / VIX history: baselines use the universe itself; regimes are universe-derived proxies.",
          "- Survivorship: universe is point-in-time by liquidity, but only names that stayed listed under the same symbol have full history."]
    return "\n".join(L) + "\n"
