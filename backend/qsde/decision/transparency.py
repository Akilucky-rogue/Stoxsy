"""What a decision card book rests on, stated next to the cards: universe version, risk inputs, exclusions, corporate-action policy, data as-of.

`explain_exclusions` and `risk_inputs` are pure (testable without a database); `gather` adds the database-backed facts and degrades to notes when a
source is unreachable instead of failing the cards.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

UNIVERSE_METHOD = "adv60_top500_v1"
SIGNAL_COMPONENTS = ["ATR% (14d)", "60d volatility", "60d downside deviation", "drawdown from 120-session high", "liquidity (median traded value)"]
CA_POLICY = ("Prices are NSE closes from the official `ohlcv` series, adjusted for splits and bonuses whose NSE record matched the price gap (method `source_confirmed`); dividends are not in the price series. "
             "A stricter governed series (`sbadj_v1`: confirmed or owner-approved events only, bounded and hash-reproducible) is built separately and is NOT yet the series the cards read: the two disagree on the events listed below. "
             "Price-only split candidates that no NSE record backs are not adjusted.")


def explain_exclusions(scores: pd.DataFrame, p, skipped_reasons: dict) -> dict:
    """Where the names in the signal universe dropped out on the way to the cards (counts, in the order the filters are applied)."""
    n0 = len(scores)
    below_bucket = scores["risk_bucket"] < p.min_bucket
    s1 = scores[~below_bucket]
    illiquid = s1["liq"] < p.min_traded_value
    s2 = s1[~illiquid]
    deep_dd = s2["dd120"] < p.max_drawdown_from_high
    s3 = s2[~deep_dd]
    return {"signal_universe": n0,
            "dropped_not_defensive_enough": int(below_bucket.sum()), "dropped_below_liquidity_floor": int(illiquid.sum()), "dropped_too_far_below_120d_high": int(deep_dd.sum()),
            "eligible_after_filters": int(len(s3)), "dropped_during_construction": dict(skipped_reasons),
            "filters": {"min_defensive_bucket": p.min_bucket, "min_median_traded_value_inr": p.min_traded_value, "max_drawdown_from_120d_high": p.max_drawdown_from_high,
                        "max_return_correlation_with_chosen_names": p.max_pair_corr}}


def risk_inputs(p, summary: dict, spec) -> dict:
    return {"portfolio": {k: v for k, v in asdict(p).items() if k != "holidays"},
            "regime": summary.get("regime"), "regime_risk_multiplier": summary.get("regime_risk_multiplier"), "exposure_cap": summary.get("exposure_cap"),
            "stop": {"horizon": p.horizon, "sessions": spec.label_horizon_days, "stop_multiple_of_daily_vol": spec.barrier.sl_mult, "daily_vol_span": spec.barrier.vol_span},
            "sizing_rule": "quantity = min(risk budget / stop distance, max position, adv participation x median traded value, remaining regime exposure), rounded down",
            "signal_components": SIGNAL_COMPONENTS,
            "signal_rule": "equal-weight percent ranks of the five components; bucket 5 = most defensive. A risk-reduction signal: no net return edge has been shown."}


def gather(scores: pd.DataFrame, price_last_date: Optional[str]) -> dict:
    """Database-backed facts: instruments excluded, BSE coverage, adjustment series, latest data dates. Never raises."""
    from qsde.db.connection import read_sql
    from qsde.universe import instruments

    out: dict = {"notes": []}
    out["universe"] = {"method": UNIVERSE_METHOD, "description": "point-in-time top 500 NSE equities by 60-session traded value, re-ranked daily",
                       "non_equity_exclusion": instruments.tag(), "non_equity_symbols_excluded": len(instruments.non_equity_symbols()),
                       "signal_rows": int(len(scores)), "exchange": "NSE only"}
    try:
        bse = read_sql("""SELECT (SELECT count(*) FROM bse_listing_master WHERE instrument_type = 'equity' AND last_date = (SELECT max(date) FROM bse_eod_raw)) AS active,
                                 (SELECT count(*) FROM listing_xref WHERE link_status LIKE 'unlinked%') AS unlinked""").iloc[0]
        out["not_covered"] = {"bse_active_equities": int(bse["active"]),
                              "reason": "BSE prices are raw_prices_only: there is no corporate-action source for them, so BSE-only names and BSE prices are excluded from the universe, the signal and the cards",
                              "unlinked_nse_bse_pairs": int(bse["unlinked"]), "other_asset_classes": "mutual funds / ETFs, F&O and commodities are separate layers and are not in these cards"}
    except Exception as ex:      # noqa: BLE001
        out["notes"].append(f"BSE coverage facts unavailable ({type(ex).__name__})")
    try:
        from qsde.alerts.facts import series_consistency
        sc = series_consistency()
        adj = read_sql("SELECT version, validation_status, n_events, built_at FROM adjustment_runs WHERE kind = 'split_bonus' ORDER BY built_at DESC LIMIT 1")
        row = adj.iloc[0] if len(adj) else None
        out["corporate_actions"] = {"policy": CA_POLICY, "series": "ohlcv (official: NSE-source-confirmed adjustments)", "validation": "method source_confirmed", "events_applied": sc["applied_in_ohlcv"],
                                    "built_at": None if row is None else pd.Timestamp(row["built_at"]).isoformat(),
                                    "governed_registry": {"version": None if row is None else str(row["version"]), "events": sc["registry_events"], "applied_in_ohlcv_not_in_registry": sc["applied_not_in_registry"],
                                                          "in_registry_not_applied_in_ohlcv": sc["registry_not_applied"]}}
    except Exception as ex:      # noqa: BLE001
        out["corporate_actions"] = {"policy": CA_POLICY}
        out["notes"].append(f"adjustment series facts unavailable ({type(ex).__name__})")
    try:
        d = read_sql("SELECT (SELECT max(date) FROM eod_raw) AS eod, (SELECT max(date) FROM ohlcv) AS ohlcv").iloc[0]
        out["data_as_of"] = {"signal_date": str(pd.Timestamp(scores["date"].iloc[0]).date()), "prices_last_date": price_last_date,
                             "eod_raw_latest": str(pd.Timestamp(d["eod"]).date()) if pd.notna(d["eod"]) else None,
                             "ohlcv_latest": str(pd.Timestamp(d["ohlcv"]).date()) if pd.notna(d["ohlcv"]) else None,
                             "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except Exception as ex:      # noqa: BLE001
        out["data_as_of"] = {"signal_date": str(pd.Timestamp(scores["date"].iloc[0]).date()), "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        out["notes"].append(f"data date facts unavailable ({type(ex).__name__})")
    return out
