"""
Index-option metadata registry for the F&O-income engine (F&O increment 4).

These are FALLBACK defaults for strike spacing / wing width / lot size. Index
F&O market structure (lot sizes, which indices have weeklies) changes often by
NSE circular, so the engine treats the DATA as authoritative wherever it can:

  * lot size  → UDiFF bhavcopy `NewBrdLotQty` / Kite instrument `lot_size`
  * spot      → UDiFF bhavcopy `UndrlygPric` / Kite index LTP
  * expiries  → whatever the chain actually quotes (we never assume weekly vs
                monthly; we pick the nearest valid expiry that is present)

`strike_step` and `wing_points` are structural choices for the condor and are
NOT in the data, so they live here. Override per-call when needed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexSpec:
    underlying: str          # canonical symbol as it appears in the F&O bhavcopy TckrSymb
    strike_step: float       # gap between adjacent listed strikes
    wing_points: float       # protective-wing distance for the condor
    lot_size_default: int    # FALLBACK only — data (NewBrdLotQty/Kite) wins
    spot_symbol: str         # Kite NSE index symbol for live LTP (KiteChainSource)


# Current best-known structure (2026). Defaults are fallbacks; live/EOD data
# overrides lot_size and spot. NIFTY is the most liquid and reliably has the
# shortest-dated expiries; BANKNIFTY structure has shifted to longer-dated only
# post-2024, which is fine — we pick whatever expiry the chain quotes.
INDEX_REGISTRY: dict[str, IndexSpec] = {
    "NIFTY": IndexSpec("NIFTY", strike_step=50.0, wing_points=200.0,
                       lot_size_default=75, spot_symbol="NIFTY 50"),
    "BANKNIFTY": IndexSpec("BANKNIFTY", strike_step=100.0, wing_points=500.0,
                           lot_size_default=30, spot_symbol="NIFTY BANK"),
    "FINNIFTY": IndexSpec("FINNIFTY", strike_step=50.0, wing_points=300.0,
                          lot_size_default=65, spot_symbol="NIFTY FIN SERVICE"),
}

# What the daily snapshotter runs by default — NIFTY only (most liquid, tightest
# spreads). Add others explicitly once their data/lot handling is verified.
DEFAULT_INDICES: tuple[str, ...] = ("NIFTY",)


def get_index_spec(underlying: str) -> IndexSpec:
    """Look up an index's structural spec; KeyError if not registered."""
    return INDEX_REGISTRY[underlying.upper()]


__all__ = ["IndexSpec", "INDEX_REGISTRY", "DEFAULT_INDICES", "get_index_spec"]
