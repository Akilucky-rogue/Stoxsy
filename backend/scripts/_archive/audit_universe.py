"""
Tradeable-universe audit — what CAN this system actually trade, and where are
the gaps? Honest, evidence-based, exchange by exchange (NSE / BSE / MCX).

It answers four questions from the live DB:
  1. INSTRUMENTS   — what does Kite's master expose per exchange/segment/type?
  2. UNIVERSE      — how many of those are in our `universe` (active, sectored)?
  3. DATA          — how many have OHLCV + factors (the ML/engine prerequisite)?
  4. TRADEABLE     — how many clear the retail liquidity bar (ADV tiers)?
Then it maps each ENGINE to the slice it can actually cover, and states the gaps
plainly (no pretending an equity ML covers commodity futures).

Usage:  python scripts/audit_universe.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.db.connection import read_sql


def _scalar(sql: str, params=None):
    df = read_sql(sql, params=params)
    return df.iloc[0, 0] if not df.empty else 0


def _hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def main() -> None:
    print("TRADEABLE-UNIVERSE AUDIT — Stoxsy")
    print("(what the system can actually trade today, and the data gaps)")

    # 1. Kite instrument master — the tradeable universe Kite exposes.
    _hr("1. INSTRUMENT MASTER (kite_instruments) — what Kite exposes")
    inst = read_sql(
        "SELECT exchange, segment, instrument_type, count(*) n FROM kite_instruments "
        "GROUP BY exchange, segment, instrument_type ORDER BY n DESC")
    if inst.empty:
        print("  (kite_instruments empty — run refresh_instruments)")
    else:
        for _, r in inst.iterrows():
            print(f"  {r['exchange']:<6} {str(r['segment']):<10} {str(r['instrument_type']):<8} {int(r['n']):>7}")
    for exch in ("BSE", "MCX", "NFO", "MCX-FUT"):
        if inst.empty or exch not in set(inst["exchange"].astype(str)):
            print(f"  [!] {exch}: NOT LOADED — needs client.refresh_instruments('{exch}') (Kite token)")

    # 2. Our universe.
    _hr("2. UNIVERSE COVERAGE (universe table)")
    uni = read_sql(
        "SELECT COALESCE(exchange,'?') exchange, count(*) total, "
        "sum(CASE WHEN is_active THEN 1 ELSE 0 END) active, "
        "sum(CASE WHEN sector IS NOT NULL AND btrim(sector)<>'' THEN 1 ELSE 0 END) sectored "
        "FROM universe GROUP BY exchange ORDER BY total DESC")
    print(f"  {'exchange':<8} {'total':>6} {'active':>7} {'sectored':>9}")
    for _, r in uni.iterrows():
        print(f"  {r['exchange']:<8} {int(r['total']):>6} {int(r['active']):>7} {int(r['sectored']):>9}")

    # 3. Data coverage (the ML/engine prerequisite).
    _hr("3. DATA COVERAGE — OHLCV + factors (prerequisite for ML/engines)")
    ohlcv_syms = int(_scalar("SELECT count(DISTINCT symbol) FROM ohlcv"))
    factor_syms = int(_scalar("SELECT count(DISTINCT symbol) FROM factor_pit"))
    nse_eq = int(_scalar("SELECT count(*) FROM kite_instruments WHERE exchange='NSE' AND instrument_type='EQ'"))
    depth = read_sql(
        "SELECT round(avg(n)) avg_days, min(n) min_days, max(n) max_days FROM "
        "(SELECT symbol, count(*) n FROM ohlcv GROUP BY symbol) s")
    print(f"  symbols with OHLCV:   {ohlcv_syms}")
    print(f"  symbols with factors: {factor_syms}")
    if not depth.empty and depth.iloc[0]['avg_days'] is not None:
        d = depth.iloc[0]
        print(f"  OHLCV history depth:  avg {int(d['avg_days'])}d, min {int(d['min_days'])}d, max {int(d['max_days'])}d")
    if nse_eq:
        print(f"  NSE-EQ coverage:      {ohlcv_syms}/{nse_eq} = {ohlcv_syms/nse_eq*100:.1f}% of tradeable NSE equities have data")

    # 4. Retail tradeability (ADV tiers).
    _hr("4. TRADEABLE AT RETAIL SIZE (ADV liquidity tiers)")
    tiers = read_sql(
        "SELECT COALESCE(liquidity_tier,'(untiered)') tier, count(*) n FROM universe "
        "WHERE is_active GROUP BY liquidity_tier ORDER BY n DESC")
    for _, r in tiers.iterrows():
        print(f"  {r['tier']:<14} {int(r['n']):>5}")
    tradeable = int(_scalar("SELECT count(*) FROM universe WHERE is_active AND liquidity_tier='tradeable'"))
    print(f"  -> {tradeable} names clear the Rs10cr/day ADV bar (what the engines actually trade).")

    # 5. Per-engine reach.
    _hr("5. PER-ENGINE COVERAGE (given today's data)")
    sectored_tradeable = int(_scalar(
        "SELECT count(*) FROM universe WHERE is_active AND liquidity_tier='tradeable' "
        "AND sector IS NOT NULL AND btrim(sector)<>''"))
    print(f"  rule_momentum / ML  : {factor_syms} names (needs factor_pit). Cross-sectional ML")
    print(f"                        wants HUNDREDS of names for dispersion — {factor_syms} is far too few.")
    print(f"  pairs_statarb       : {sectored_tradeable} sectored+tradeable names -> same-sector pairs only.")
    print(f"  fno_income          : NIFTY/BANKNIFTY INDEX OPTIONS — needs NFO segment "
          f"({'LOADED' if not inst.empty and 'NFO' in set(inst['exchange'].astype(str)) else 'NOT loaded'}).")

    # 6. Honest gaps + verdict per exchange.
    _hr("6. GAPS & HONEST VERDICT (NSE / BSE / MCX)")
    print("  NSE (equities): the tradeable list is ~%d EQ names, but only %d have data (%.1f%%)."
          % (nse_eq, ohlcv_syms, (ohlcv_syms/nse_eq*100 if nse_eq else 0)))
    print("     -> Backfill OHLCV+factors for the liquid set (Nifty-200/500) to give the ML a real")
    print("        cross-section. This is the single biggest unlock. Run: scripts/expand_universe_*.py")
    bse_uni = int(_scalar("SELECT count(*) FROM universe WHERE exchange='BSE' AND is_active"))
    print(f"  BSE (equities): {bse_uni} in universe; NOT in kite_instruments (need refresh_instruments('BSE')).")
    print("     -> Most liquid BSE names are DUAL-LISTED on NSE (same symbol). Marginal incremental")
    print("        universe; needs a (symbol, exchange) composite key for true multi-exchange. Low priority.")
    print("  MCX (commodities): NOT loaded, and it is a DIFFERENT ASSET CLASS — commodity FUTURES")
    print("        (gold/silver/crude/natgas/base metals, ~15 underlyings). The cross-sectional equity")
    print("        ML and the pairs engine DO NOT apply. MCX needs a separate trend/term-structure")
    print("        futures engine (roll yield, backwardation/contango) — a distinct build, not this pipeline.")
    print()
    print("  BOTTOM LINE: today the system genuinely covers ~%d liquid NSE equities end-to-end." % tradeable)
    print("  Expanding NSE-equity DATA coverage is the real lever; BSE is overlap; MCX is a new engine.")


if __name__ == "__main__":
    main()
