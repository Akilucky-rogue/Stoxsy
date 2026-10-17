"""F&O-income domain: options math (Black-Scholes / IV / Greeks), chain ingest,
and the vol-regime filter. ARCHITECTURE.md §F&O — the flagship engine's toolkit.
"""
from qsde.fno.black_scholes import bs_greeks, bs_price, implied_vol
from qsde.fno.bhavcopy import (
    extract_lot_size,
    extract_underlying_spot,
    fetch_fo_bhavcopy,
    parse_fo_bhavcopy,
)
from qsde.fno.chain_source import (
    BhavcopyChainSource,
    KiteChainSource,
    build_snapshots,
    kite_rows_to_options,
    persist_atm_iv,
)
from qsde.fno.indices import DEFAULT_INDICES, INDEX_REGISTRY, get_index_spec
from qsde.fno.iv_store import load_atm_iv_history, record_atm_iv
from qsde.fno.vol import add_iv, atm_iv, build_chain_snapshot, iv_rank

__all__ = [
    # options math
    "bs_price", "bs_greeks", "implied_vol",
    # bhavcopy parse / fetch / extract
    "parse_fo_bhavcopy", "fetch_fo_bhavcopy",
    "extract_underlying_spot", "extract_lot_size",
    # IV analytics
    "add_iv", "atm_iv", "iv_rank", "build_chain_snapshot",
    # IV history store
    "record_atm_iv", "load_atm_iv_history",
    # index registry
    "INDEX_REGISTRY", "DEFAULT_INDICES", "get_index_spec",
    # chain sources
    "build_snapshots", "persist_atm_iv", "kite_rows_to_options",
    "BhavcopyChainSource", "KiteChainSource",
]
