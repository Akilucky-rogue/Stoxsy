"""Tier 1 rule-based signal engine — orchestrator.

Loads OHLCV for the active liquid universe, computes 4 factor scores per
symbol per horizon, cross-sectionally ranks within the universe, builds the
IC-weighted composite, and returns a per-symbol signal frame ready for the
signal writer.

Strategies emitted per (symbol, date, horizon):
  - tier1_jt        Jegadeesh-Titman 12-1 cross-sectional momentum
  - tier1_mop       Moskowitz-Ooi-Pedersen time-series momentum (vol-scaled)
  - tier1_bab       Frazzini-Pedersen betting-against-beta tilt
  - tier1_rsi2      Connors-Alvarez RSI(2) mean reversion (swing only)
  - tier1_composite IC-weighted blend of the above

Direction (+1 / 0 / -1) is assigned by cross-sectional decile:
  top decile (rank >= 0.9)    -> +1 long
  bottom decile (rank <= 0.1) -> -1 short
  else                        -> 0 hold

Important design choices
------------------------
1. Live signals compute scores directly from raw OHLCV. We do NOT read
   factor_pit for the four Tier 1 factors — the formulas are pure and
   cheap (sub-second for 500 symbols). factor_pit is reserved for the
   weekly drift / IC backfill jobs.
2. Liquidity gate matches the ML path exactly: trailing-20d ADV >= ₹10cr
   (env-overridable). Same `LIQUIDITY_MIN_RUPEES` constant.
3. The market benchmark for BAB is an equal-weighted NIFTY 50 daily-close
   series built from OHLCV. When market_cap data is available we
   upgrade to cap-weighting; until then equal-weight is a fine first pass.
4. Composite IC weights come from `rule_factor_ic_latest`. If that view
   has no rows (cold start), we fall back to equal-weight. As paper
   sessions accumulate, the weekly validator backfills IC and the
   composite self-tunes.
"""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Optional

import numpy as np
import pandas as pd

from qsde.db.connection import read_sql
from qsde.factors.rules import (
    FACTOR_NAMES,
    HORIZON_FACTORS,
    bab_score,
    composite_rank_ic_weighted,
    connors_rsi2_score,
    cross_sectional_rank,
    jegadeesh_titman_score,
    mop_tsmom_score,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Tunables (env-overridable so we can experiment without code changes)
# ──────────────────────────────────────────────────────────────────────

LIQUIDITY_MIN_RUPEES = float(os.getenv("QSDE_LIQUIDITY_MIN_CR", "10")) * 1e7

# How much OHLCV history to load. JT needs 252+21 = 273 days; we add buffer
# for warmup of vol estimation, SMA(200), etc.
OHLCV_LOOKBACK_DAYS = int(os.getenv("QSDE_TIER1_OHLCV_LOOKBACK", "400"))

# Decile thresholds for direction assignment. Default top/bottom 10%.
LONG_DECILE = float(os.getenv("QSDE_TIER1_LONG_DECILE", "0.10"))   # top 10%
SHORT_DECILE = float(os.getenv("QSDE_TIER1_SHORT_DECILE", "0.10")) # bottom 10%

# Benchmark — we use NIFTY 50 as the broad-market proxy for BAB.
BENCHMARK_INDEX = os.getenv("QSDE_TIER1_BENCHMARK", "NIFTY 50")

# Momentum-tilted COLD-START PRIOR for the composite weights.
#
# The full-history backtest (scripts/backtest_rules.py, 164 tradeable NSE names,
# 2019-2026, adj_close total-return, realistic next-session entry, net of 12bps)
# measured which tier-1 factors actually carry a net-of-cost edge that beats both
# costs and an equal-weight buy-and-hold benchmark:
#   * jt  (Jegadeesh-Titman 12-1)     -> REAL EDGE  (+0.6-0.9 Sharpe excess, DSR 0.96+)
#   * mop (time-series momentum)      -> REAL EDGE  (+0.5-0.7 Sharpe excess, DSR 0.94+)
#   * bab (betting-against-beta)      -> DEAD        (NEGATIVE excess + negative L/S spread
#                                                     in BOTH sub-periods; the Indian bull
#                                                     rewarded high beta, opposite of BAB)
#   * rsi2 (Connors RSI(2) reversion) -> UNRELIABLE  (marginal->bad across regimes, 68%
#                                                     turnover, cost-sensitive)
# Equal-weighting all four DILUTES the momentum alpha to ~benchmark (excess ~0).
# So until live paper accumulates >= MIN_OBSERVATIONS of resolved IC per factor,
# we anchor the composite to a momentum-tilted prior. See memory: momentum-is-the-real-edge.
MOMENTUM_PRIOR: dict[str, float] = {"jt": 0.5, "mop": 0.5, "bab": 0.0, "rsi2": 0.0}

# Shrinkage anchor toward the backtest prior when blending with live IC.
#
# The live IC (rule_factor_ic) is a 60-SESSION ROLLING Spearman IC — structurally
# a ~3-month regime estimate. On its own it chases whatever worked last quarter:
# e.g. the June-2026 paper month was mean-reverting, so live IC put ~100% of the
# swing composite on rsi2 (the factor the 7-year backtest proved is the WORST) and
# ZERO on momentum. A 3-month rolling estimate must NOT be trusted to fully
# override a 7-year, regime-stable backtest. So we Bayesian-shrink live IC toward
# the durable prior: final = SHRINK*prior + (1-SHRINK)*normalized_live_IC. Live
# data can TILT the composite but never HIJACK it — momentum keeps a hard floor.
PRIOR_SHRINK = float(os.getenv("QSDE_TIER1_PRIOR_SHRINK", "0.70"))


# ──────────────────────────────────────────────────────────────────────
# Data loaders
# ──────────────────────────────────────────────────────────────────────

def load_active_universe() -> pd.DataFrame:
    """Active symbols with their NSE/BSE-index membership.

    Honors the `liquidity_tier` column populated by
    qsde.universe.liquidity_filter.refresh_universe_tiers() when present:
    returns only `tradeable` rows. When the column is NULL across the
    whole table (pre-expansion bootstrap state), returns ALL active rows
    so the engine still works — the inline ADV gate downstream catches
    illiquid names defensively.

    Returns
    -------
    pd.DataFrame
        columns: symbol, index_membership (list[str]), market_cap, sector,
                 liquidity_tier (str or NULL), adv_60d (float or NULL)
    """
    df = read_sql(
        """
        SELECT symbol,
               index_membership,
               market_cap,
               sector,
               liquidity_tier,
               adv_60d
          FROM universe
         WHERE is_active = TRUE
        """
    )
    if df.empty:
        log.warning("Active universe is empty; rule engine will produce no signals")
        return df

    # If liquidity_tier has been populated anywhere, trust it and trim
    # to tradeable. Otherwise (NULL everywhere = pre-expansion bootstrap)
    # let the downstream inline ADV gate handle the cut.
    has_tiers = df["liquidity_tier"].notna().any()
    if has_tiers:
        tradeable = df[df["liquidity_tier"] == "tradeable"].reset_index(drop=True)
        log.info("Universe: %d total active, %d tradeable (tier-filtered)",
                 len(df), len(tradeable))
        return tradeable
    log.info("Universe: %d active (no liquidity_tier yet; inline ADV gate will apply)",
             len(df))
    return df


def load_close_panel(
    symbols: list[str],
    lookback_days: int = OHLCV_LOOKBACK_DAYS,
) -> pd.DataFrame:
    """OHLCV close prices for `symbols` over the last `lookback_days` sessions.

    Returns
    -------
    pd.DataFrame
        Rows = trading dates (ascending), columns = symbols, values = close.
        Missing symbol/date combos are NaN.
    """
    if not symbols:
        return pd.DataFrame()

    # Use parameter binding for the IN clause via a literal join — psycopg2
    # doesn't bind a Python list well across all driver versions.
    sym_list = ",".join(f"'{s}'" for s in symbols)
    sql = f"""
        SELECT symbol, date, close
          FROM ohlcv
         WHERE symbol IN ({sym_list})
           AND date >= (CURRENT_DATE - INTERVAL '{int(lookback_days)} days')
      ORDER BY symbol, date
    """
    long_df = read_sql(sql)
    if long_df.empty:
        return pd.DataFrame()
    long_df["date"] = pd.to_datetime(long_df["date"])
    long_df["close"] = long_df["close"].astype(float)
    wide = long_df.pivot(index="date", columns="symbol", values="close").sort_index()
    return wide


def compute_adv_map() -> dict[str, float]:
    """Trailing-20d average daily value traded (₹) per symbol.

    Mirrors signal_generator._compute_adv_map() so the rule-engine and
    ML pipeline gate liquidity identically.
    """
    ohlcv = read_sql(
        """
        SELECT symbol, date, close, volume
          FROM ohlcv
         WHERE date >= (CURRENT_DATE - INTERVAL '45 days')
        """
    )
    if ohlcv.empty:
        return {}
    ohlcv["dv"] = ohlcv["close"].astype(float) * ohlcv["volume"].astype(float)
    adv = (
        ohlcv.sort_values(["symbol", "date"])
             .groupby("symbol")["dv"]
             .apply(lambda s: s.tail(20).mean())
    )
    return {sym: float(v) for sym, v in adv.items() if pd.notna(v)}


def load_latest_atr_map() -> dict[str, float]:
    """Latest tech_atr_pct per symbol (used for trade-level computation).

    Reads the Parquet feature store (same store training and serving use).
    """
    from qsde.features.store import latest_factor

    s = latest_factor("tech_atr_pct", max_age_days=20)
    return {str(k): float(v) for k, v in s.items() if pd.notna(v)}


def shrink_toward_prior(
    live_pos: dict[str, float],
    prior: dict[str, float],
    shrink: float = PRIOR_SHRINK,
) -> dict[str, float]:
    """Bayesian-shrink positive live IC weights toward the backtest prior.

    final[f] = shrink * prior[f] + (1 - shrink) * live_norm[f]

    where live_norm is `live_pos` renormalized to sum 1. Both inputs are keyed by
    factor name over the same key set. If live_pos sums to <= 0 (cold start), the
    prior is returned unchanged. Pure — no I/O — so the momentum-floor property is
    unit-testable: with shrink=0.7 and prior {jt:.5,mop:.5}, jt+mop weight is
    always >= 0.7 no matter how live IC is distributed.
    """
    keys = list(prior.keys())
    live_total = sum(max(live_pos.get(k, 0.0), 0.0) for k in keys)
    if live_total <= 0.0:
        return dict(prior)
    return {
        k: shrink * prior[k] + (1.0 - shrink) * (max(live_pos.get(k, 0.0), 0.0) / live_total)
        for k in keys
    }


def load_ic_weights(horizon: str) -> dict[str, float]:
    """Composite factor weights = backtest prior Bayesian-shrunk toward live IC.

    The live IC (rule_factor_ic) is a 60-session ROLLING estimate — a ~3-month
    regime snapshot. Trusting it to FULLY set the composite lets a single quarter
    hijack the blend (June-2026 paper put ~100% weight on rsi2, the factor the
    7-year backtest proved is worst, and 0% on momentum). So instead of replacing
    the prior with live IC, we shrink toward the durable prior:

        final[f] = PRIOR_SHRINK * prior[f] + (1 - PRIOR_SHRINK) * live_norm[f]

    where prior is MOMENTUM_PRIOR (jt/mop) and live_norm is the positive live IC
    weights renormalized to sum 1. Live data can TILT the composite but momentum
    keeps a hard floor (>= PRIOR_SHRINK * its prior weight). composite_rank_ic_
    weighted re-normalizes downstream, so only relative magnitudes matter here.

    Returns a dict over all 4 FACTOR_NAMES.
    """
    prior = {name: MOMENTUM_PRIOR.get(name, 0.0) for name in FACTOR_NAMES}
    df = read_sql(
        """
        SELECT factor_name, composite_weight
          FROM rule_factor_ic_latest
         WHERE horizon = :h
        """,
        params={"h": horizon},
    )
    if df.empty:
        return prior

    raw = {str(r["factor_name"]): float(r["composite_weight"]) for _, r in df.iterrows()}
    live_pos = {name: max(raw.get(name, 0.0), 0.0) for name in FACTOR_NAMES}
    # Shrink live IC toward the durable prior (returns the prior outright at cold
    # start when live IC has produced no positive weight).
    return shrink_toward_prior(live_pos, prior)


# ──────────────────────────────────────────────────────────────────────
# Benchmark construction
# ──────────────────────────────────────────────────────────────────────

def build_benchmark_series(
    close_panel: pd.DataFrame,
    universe_df: pd.DataFrame,
    index_name: str = BENCHMARK_INDEX,
) -> Optional[pd.Series]:
    """Equal-weighted close series for the constituents of `index_name`.

    Used as the market series in BAB beta estimation. We deliberately do
    NOT load a separate index quote — building the benchmark from the same
    OHLCV that drives stock scores means costs/holidays/halts are aligned.

    Returns None if no constituent symbols are present in close_panel.
    """
    # universe_df.index_membership is a JSONB list. Cast carefully.
    def _has_index(membership) -> bool:
        if not membership:
            return False
        try:
            return index_name in membership
        except TypeError:
            return False

    members = universe_df.loc[
        universe_df["index_membership"].apply(_has_index), "symbol"
    ].tolist()
    available = [s for s in members if s in close_panel.columns]
    if not available:
        log.warning("Benchmark %s has no available constituents in OHLCV panel", index_name)
        return None
    # Equal-weight returns -> reconstruct synthetic level series.
    # fill_method=None per pandas 3.0 future-proofing: we explicitly do NOT
    # forward-fill missing prices into fake "no-change" returns; halted/
    # missing days stay NaN and drop out of the mean.
    rets = close_panel[available].pct_change(fill_method=None)
    avg_ret = rets.mean(axis=1)
    synth_level = (1.0 + avg_ret.fillna(0)).cumprod() * 100.0
    synth_level.name = f"{index_name}_eq"
    return synth_level


# ──────────────────────────────────────────────────────────────────────
# Per-factor score computation across the universe
# ──────────────────────────────────────────────────────────────────────

def compute_jt_panel(close_panel: pd.DataFrame) -> pd.DataFrame:
    """Apply Jegadeesh-Titman to every column, return wide DataFrame."""
    return close_panel.apply(jegadeesh_titman_score, axis=0)


def compute_mop_panel(close_panel: pd.DataFrame) -> pd.DataFrame:
    return close_panel.apply(mop_tsmom_score, axis=0)


def compute_bab_panel(close_panel: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    """BAB per symbol vs the synthetic benchmark."""
    return close_panel.apply(lambda s: bab_score(s, benchmark), axis=0)


def compute_rsi2_panel(close_panel: pd.DataFrame) -> pd.DataFrame:
    return close_panel.apply(connors_rsi2_score, axis=0)


# ──────────────────────────────────────────────────────────────────────
# Main entry: per-horizon orchestrator
# ──────────────────────────────────────────────────────────────────────

def run_for_horizon(
    horizon: str,
    as_of_date: Optional[date] = None,
) -> pd.DataFrame:
    """Generate Tier 1 signals for `horizon` as of `as_of_date` (default today).

    Returns
    -------
    pd.DataFrame
        One row per (strategy, symbol) for symbols with a non-NaN score.
        Columns: strategy, symbol, date, horizon, score, rank_pct,
                 direction, confidence.

        Empty for intraday (Tier 1 deliberately does not trade intraday —
        daily-bar factors are unsuitable for sub-day horizons).
    """
    if horizon not in HORIZON_FACTORS:
        raise ValueError(f"Unknown horizon: {horizon!r}")
    if not HORIZON_FACTORS[horizon]:
        log.info("Tier 1 has no factors for horizon=%s; skipping", horizon)
        return pd.DataFrame()

    as_of_date = as_of_date or date.today()

    # ── 1. Load data ─────────────────────────────────────────────────
    universe_df = load_active_universe()
    if universe_df.empty:
        return pd.DataFrame()

    symbols = universe_df["symbol"].tolist()
    close_panel = load_close_panel(symbols)
    if close_panel.empty:
        log.warning("No OHLCV loaded; rule engine returning empty")
        return pd.DataFrame()

    # Restrict to the most recent session present in the panel — we score
    # signals AS OF the last bar.
    latest_session = close_panel.index.max().date()
    log.info("Tier 1 engine: horizon=%s, latest_session=%s, symbols=%d",
             horizon, latest_session, close_panel.shape[1])

    # ── 2. Liquidity gate ────────────────────────────────────────────
    adv_map = compute_adv_map()
    liquid_symbols = [
        s for s in close_panel.columns
        if adv_map.get(s, 0.0) >= LIQUIDITY_MIN_RUPEES
    ]
    if not liquid_symbols:
        log.warning("No liquid symbols after ADV gate (>= ₹%.0fcr); empty signals",
                    LIQUIDITY_MIN_RUPEES / 1e7)
        return pd.DataFrame()
    log.info("Liquid universe: %d of %d symbols", len(liquid_symbols), close_panel.shape[1])

    # ── 3. Benchmark for BAB ─────────────────────────────────────────
    benchmark = build_benchmark_series(close_panel, universe_df)

    # ── 4. Per-factor score panels (full universe, then we slice) ────
    factor_panels: dict[str, pd.DataFrame] = {}
    horizon_factors = HORIZON_FACTORS[horizon]

    if "jt" in horizon_factors:
        factor_panels["jt"] = compute_jt_panel(close_panel[liquid_symbols])
    if "mop" in horizon_factors:
        factor_panels["mop"] = compute_mop_panel(close_panel[liquid_symbols])
    if "bab" in horizon_factors and benchmark is not None:
        factor_panels["bab"] = compute_bab_panel(close_panel[liquid_symbols], benchmark)
    if "rsi2" in horizon_factors:
        factor_panels["rsi2"] = compute_rsi2_panel(close_panel[liquid_symbols])

    if not factor_panels:
        log.warning("No factor panels computed; empty signals")
        return pd.DataFrame()

    # ── 5. Cross-sectional rank per factor, latest session only ──────
    ranked_panels: dict[str, pd.DataFrame] = {
        name: cross_sectional_rank(panel) for name, panel in factor_panels.items()
    }

    # Get latest-session row per ranked factor (Series indexed by symbol).
    latest_idx = close_panel.index.max()
    latest_ranks_per_factor: dict[str, pd.Series] = {}
    for name, panel in ranked_panels.items():
        if latest_idx in panel.index:
            row = panel.loc[latest_idx].dropna()
            if not row.empty:
                latest_ranks_per_factor[name] = row

    if not latest_ranks_per_factor:
        log.warning("No per-factor scores on latest session %s; empty signals", latest_idx)
        return pd.DataFrame()

    # ── 6. Composite via IC-weighted blend ───────────────────────────
    ic_weights = load_ic_weights(horizon)
    log.info("Composite IC weights (horizon=%s): %s", horizon,
             {k: round(v, 3) for k, v in ic_weights.items()})

    # Guard the MOMENTUM_PRIOR floor AFTER the NaN-drop. composite_rank_ic_weighted
    # renormalizes over only the factors that scored on the latest session, so if
    # neither jt nor mop survives (e.g. a universe with < ~253 sessions of history
    # where momentum is still NaN but rsi2 is alive), the composite would collapse
    # to ~100% rsi2/bab — silently defeating the 70% momentum floor the shrinkage
    # exists to enforce. A "momentum composite" with zero momentum is meaningless,
    # so we skip it entirely in that case. (Unreachable on the current 708+ session
    # universe, but a real latent trap flagged by the red-team.)
    composite_series = pd.Series(dtype=float)
    if any(f in latest_ranks_per_factor for f in ("jt", "mop")):
        one_row_panels = {
            name: pd.DataFrame([series]) for name, series in latest_ranks_per_factor.items()
        }
        composite_df = composite_rank_ic_weighted(one_row_panels, ic_weights)
        composite_series = composite_df.iloc[0].dropna()
    else:
        log.warning(
            "Neither jt nor mop scored on %s; SKIPPING tier1_composite so it cannot "
            "collapse to rsi2/bab and defeat the momentum floor.", latest_idx)

    # ── 7. Assemble per-strategy frames ──────────────────────────────
    horizon_str = horizon  # for clarity
    frames: list[pd.DataFrame] = []

    # signals.date is the CALENDAR date the signal is observable, not the
    # OHLCV bar date. This matches signal_generator.py (ML) so the downstream
    # paper_journal taker queries `date = today` and finds both ML and Tier 1
    # rows. On weekends/holidays latest_session lags today by 1-3 days; the
    # signal still "applies" today even though it was computed off Friday's
    # close.
    signal_calendar_date = as_of_date

    def _assemble(name: str, scores: pd.Series, strategy: str) -> pd.DataFrame:
        """Build a per-strategy DataFrame with direction + confidence."""
        if scores.empty:
            return pd.DataFrame()
        df = pd.DataFrame({
            "symbol": scores.index,
            "score": scores.values,
        })
        # rank_pct in [0, 1] (1 = best). We re-rank within liquid universe.
        df["rank_pct"] = df["score"].rank(pct=True, method="average")
        # Direction by decile.
        df["direction"] = 0
        df.loc[df["rank_pct"] >= (1.0 - LONG_DECILE), "direction"] = 1
        df.loc[df["rank_pct"] <= SHORT_DECILE, "direction"] = -1
        # Confidence: distance from neutral.
        df["confidence"] = (df["rank_pct"] - 0.5).abs() * 2.0  # [0, 1]
        df["strategy"] = strategy
        df["date"] = signal_calendar_date
        df["horizon"] = horizon_str
        return df[["strategy", "symbol", "date", "horizon",
                   "score", "rank_pct", "direction", "confidence"]]

    # Per-factor streams: tier1_jt, tier1_mop, tier1_bab, tier1_rsi2
    for name, series in latest_ranks_per_factor.items():
        frames.append(_assemble(name, series, f"tier1_{name}"))
    # IC-shrunk composite (all surviving factors, momentum-anchored weights).
    frames.append(_assemble("composite", composite_series, "tier1_composite"))

    # tier1_mom — the PROVEN, ACTIONABLE momentum composite: equal-weight blend of
    # jt + mop only (no BAB/RSI2 at all). scripts/backtest_rules.py measured this as
    # the durable net-of-cost edge (swing excess +0.48 Sharpe @30bps, regime-stable,
    # red-team-verified). The RuleMomentumEngine trades THIS by default; the other
    # tier1_* streams are written for research/Scorecard comparison but are not
    # actionable (BAB/RSI2 proved to be beta, the equal-weight composite dilutes).
    mom_members = [latest_ranks_per_factor[f] for f in ("jt", "mop")
                   if f in latest_ranks_per_factor]
    if mom_members:
        mom_series = pd.concat(mom_members, axis=1).mean(axis=1).dropna()
        frames.append(_assemble("mom", mom_series, "tier1_mom"))

    out = pd.concat([f for f in frames if not f.empty], ignore_index=True)
    log.info("Tier 1 engine produced %d signal rows across %d strategies",
             len(out), out["strategy"].nunique() if not out.empty else 0)
    return out


__all__ = [
    "LIQUIDITY_MIN_RUPEES",
    "OHLCV_LOOKBACK_DAYS",
    "LONG_DECILE",
    "SHORT_DECILE",
    "BENCHMARK_INDEX",
    "MOMENTUM_PRIOR",
    "PRIOR_SHRINK",
    "shrink_toward_prior",
    "load_active_universe",
    "load_close_panel",
    "compute_adv_map",
    "load_latest_atr_map",
    "load_ic_weights",
    "build_benchmark_series",
    "run_for_horizon",
]
