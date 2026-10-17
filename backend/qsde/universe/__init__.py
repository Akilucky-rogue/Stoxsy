"""Universe management — listing, liquidity classification, expansion.

The Tier 1 rule engine reads `universe.is_active AND liquidity_tier='tradeable'`
to decide what to score. This module keeps that table honest:

  - liquidity_filter: ADV-based tier classification (tradeable / marginal / illiquid)
  - expansion script (scripts/expand_universe_nifty500.py): adds new symbols
    from Kite's instrument list + backfills OHLCV + recomputes tiers
"""
