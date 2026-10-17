"""Tests for the F&O chain source (increment 4) — the data->snapshot plumbing.

All PURE (no DB / no network): build_snapshots + expiry selection + spot/lot
derivation + lookahead-safe iv_rank, the Kite assembler, and the end-to-end
BhavcopyChainSource (csv_text + injected history) feeding FnoIncomeEngine.
"""
from __future__ import annotations

import math
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.engines.base import Structure
from qsde.engines.fno_income import FnoIncomeEngine
from qsde.fno.black_scholes import bs_price
from qsde.fno.chain_source import (
    BhavcopyChainSource,
    build_snapshots,
    kite_rows_to_options,
)
from qsde.fno.vol import iv_rank as _iv_rank
from qsde.scorecard.store import idea_to_row

ASOF = date(2026, 7, 24)
EXP = date(2026, 7, 31)        # 7 DTE — inside the default [2, 60] window
NEAR = date(2026, 7, 25)       # 1 DTE — excluded by min_dte
FAR = date(2026, 11, 30)       # >60 DTE — excluded by max_dte

# Premiums consistent with spot 25000, iv 0.15, 7d (so add_iv/atm_iv ~ 0.15).
_CE_ATM = bs_price(25000, 25000, 7 / 365, 0.065, 0.15, "CE")
_PE_ATM = bs_price(25000, 25000, 7 / 365, 0.065, 0.15, "PE")


def _row(strike, ot, settle, expiry=EXP, sym="NIFTY", upx=25000.0, lot=75):
    # An already-IV'd row: build_snapshots needs symbol/expiry/strike/opt_type/
    # settle/iv (+ underlying_px, lot_size). iv only matters at the ATM strike.
    iv = 0.15 if strike == 25000 else 0.16
    return {"symbol": sym, "expiry": expiry, "strike": float(strike), "opt_type": ot,
            "settle": float(settle), "iv": iv, "underlying_px": upx, "lot_size": lot}


def _nifty_condor_frame(**over) -> pd.DataFrame:
    rows = [
        _row(25000, "CE", _CE_ATM, **over), _row(25000, "PE", _PE_ATM, **over),
        _row(24500, "PE", 120.0, **over), _row(24300, "PE", 70.0, **over),
        _row(25500, "CE", 110.0, **over), _row(25700, "CE", 65.0, **over),
    ]
    return pd.DataFrame(rows)


def _hist(values):
    """An iv_history_lookup that records its calls and returns a fixed window."""
    calls = []
    def lookup(underlying, asof):
        calls.append((underlying, asof))
        return list(values)
    lookup.calls = calls
    return lookup


# ── build_snapshots ───────────────────────────────────────────────────

def test_build_snapshots_happy_path_feeds_engine():
    lookup = _hist([0.10, 0.14])              # current ~0.15 > max -> iv_rank 1.0
    snaps = build_snapshots(_nifty_condor_frame(), indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=lookup)
    assert len(snaps) == 1
    s = snaps[0]
    assert s["underlying"] == "NIFTY"
    assert s["spot"] == pytest.approx(25000.0)
    assert s["expiry"] == EXP
    assert s["lot_size"] == 75 and s["strike_step"] == 50.0 and s["wing_points"] == 200.0
    assert s["atm_strike"] == pytest.approx(25000.0)
    assert s["atm_iv"] == pytest.approx(0.15, abs=1e-2)
    assert s["iv_rank"] == pytest.approx(1.0)
    assert lookup.calls == [("NIFTY", ASOF)]   # lookahead-safe: asked for asof's window

    ideas = FnoIncomeEngine(lot_size=50, strike_step=50.0, source=lambda: snaps).scan()
    assert len(ideas) == 1
    idea = ideas[0]
    assert idea.structure == Structure.IRON_CONDOR
    # snapshot lot_size (75) overrides the engine's 50: credit 95, width 200.
    assert idea.max_loss_rupees == pytest.approx(105 * 75)
    assert idea.net_credit == pytest.approx(95 * 75)
    idea.validate()


def test_iv_rank_is_lookahead_safe_and_drives_gate():
    # current ATM IV ~0.15 sits BELOW this window -> iv_rank 0.0 -> engine skips.
    snaps = build_snapshots(_nifty_condor_frame(), indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=_hist([0.20, 0.30]))
    assert snaps[0]["iv_rank"] == pytest.approx(0.0)
    assert FnoIncomeEngine(lot_size=75, strike_step=50.0, source=lambda: snaps).scan() == []


def test_selects_nearest_two_sided_expiry_in_window():
    df = _nifty_condor_frame()
    # add a 1-DTE (excluded) and a far (excluded) ATM pair, plus a one-sided
    # in-window expiry (CE only) that must NOT be chosen.
    extra = pd.DataFrame([
        _row(25000, "CE", _CE_ATM, expiry=NEAR), _row(25000, "PE", _PE_ATM, expiry=NEAR),
        _row(25000, "CE", _CE_ATM, expiry=FAR), _row(25000, "PE", _PE_ATM, expiry=FAR),
    ])
    snaps = build_snapshots(pd.concat([extra, df], ignore_index=True),
                            indices=["NIFTY"], asof=ASOF, iv_history_lookup=_hist([0.1]))
    assert len(snaps) == 1 and snaps[0]["expiry"] == EXP


def test_skips_one_sided_expiry():
    calls_only = pd.DataFrame([_row(25000, "CE", _CE_ATM), _row(25500, "CE", 110.0)])
    assert build_snapshots(calls_only, indices=["NIFTY"], asof=ASOF,
                           iv_history_lookup=_hist([0.1])) == []


def test_lot_size_falls_back_to_registry_when_absent():
    # strip the lot_size column -> snapshot should use NIFTY's registry default (75).
    df = _nifty_condor_frame()
    df["lot_size"] = float("nan")
    snaps = build_snapshots(df, indices=["NIFTY"], asof=ASOF, iv_history_lookup=_hist([0.1]))
    assert snaps[0]["lot_size"] == 75


def test_no_spot_skips_unless_lookup_supplied():
    df = _nifty_condor_frame()
    df["underlying_px"] = float("nan")
    # no spot in the data, no fallback -> skipped
    assert build_snapshots(df, indices=["NIFTY"], asof=ASOF,
                           iv_history_lookup=_hist([0.1])) == []
    # with a spot_lookup, it recovers
    snaps = build_snapshots(df, indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=_hist([0.1]),
                            spot_lookup=lambda u: 25000.0)
    assert len(snaps) == 1 and snaps[0]["spot"] == pytest.approx(25000.0)


def test_unregistered_index_is_skipped():
    df = _nifty_condor_frame(sym="WONKY")
    assert build_snapshots(df, indices=["WONKY"], asof=ASOF,
                           iv_history_lookup=_hist([0.1])) == []


# ── kite_rows_to_options (pure Kite assembler) ────────────────────────

def test_kite_rows_to_options_builds_clean_frame():
    insts = [
        {"tradingsymbol": "NIFTY26JUL25000CE", "strike": 25000, "instrument_type": "CE",
         "expiry": EXP, "lot_size": 75},
        {"tradingsymbol": "NIFTY26JUL25000PE", "strike": 25000, "instrument_type": "PE",
         "expiry": EXP, "lot_size": 75},
        {"tradingsymbol": "NIFTY26JULFUT", "strike": 0, "instrument_type": "FUT",
         "expiry": EXP, "lot_size": 75},                       # not an option -> dropped
        {"tradingsymbol": "NIFTY26JUL25500CE", "strike": 25500, "instrument_type": "CE",
         "expiry": EXP, "lot_size": 75},                       # no quote -> dropped
    ]
    quotes = {
        "NIFTY26JUL25000CE": {"last_price": 150.0},
        "NIFTY26JUL25000PE": {"last_price": 140.0},
        "NIFTY26JUL25500CE": {"last_price": 0.0},              # zero -> dropped
    }
    df = kite_rows_to_options(insts, quotes, spot=25010.0, underlying="NIFTY")
    assert set(df["opt_type"]) == {"CE", "PE"} and len(df) == 2
    assert df.iloc[0]["underlying_px"] == pytest.approx(25010.0)
    assert df.iloc[0]["expiry"] == EXP


# ── BhavcopyChainSource end-to-end (csv_text + injected history, no DB) ─

def _udiff_csv() -> str:
    head = ("FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,SttlmPric,OpnIntrst,"
            "UndrlygPric,NewBrdLotQty")
    rows = [
        f"IDO,NIFTY,2026-07-31,25000,CE,{_CE_ATM:.2f},1000,25000,75",
        f"IDO,NIFTY,2026-07-31,25000,PE,{_PE_ATM:.2f},1000,25000,75",
        "IDO,NIFTY,2026-07-31,24500,PE,120.0,1000,25000,75",
        "IDO,NIFTY,2026-07-31,24300,PE,70.0,1000,25000,75",
        "IDO,NIFTY,2026-07-31,25500,CE,110.0,1000,25000,75",
        "IDO,NIFTY,2026-07-31,25700,CE,65.0,1000,25000,75",
        "IDF,NIFTY,2026-07-31,0,,25010,500,25000,75",          # future row -> parser drops
    ]
    return head + "\n" + "\n".join(rows) + "\n"


def test_bhavcopy_chain_source_end_to_end_no_db():
    src = BhavcopyChainSource(ASOF, indices=["NIFTY"], csv_text=_udiff_csv(), persist=False)
    src._history_lookup = lambda u, asof: [0.10, 0.14]   # inject (no DB read)

    snaps = src()
    assert len(snaps) == 1
    s = snaps[0]
    assert s["underlying"] == "NIFTY" and s["expiry"] == EXP
    assert s["spot"] == pytest.approx(25000.0)
    assert s["atm_iv"] == pytest.approx(0.15, abs=1e-2)
    assert s["iv_rank"] == pytest.approx(1.0)
    assert s["lot_size"] == 75

    ideas = FnoIncomeEngine(lot_size=75, strike_step=50.0, source=src).scan()
    assert len(ideas) == 1
    ideas[0].validate()
    assert ideas[0].max_loss_rupees == pytest.approx(105 * 75)


def test_bhavcopy_source_degrades_when_fetch_fails(monkeypatch):
    # NSE blocks bots / file not published yet -> the source must return [] (no
    # chain this run), NEVER propagate and crash the runner / daily EOD.
    import qsde.fno.chain_source as cs

    def boom(_d):
        raise RuntimeError("403 Forbidden")
    monkeypatch.setattr(cs, "fetch_fo_bhavcopy", boom)
    src = BhavcopyChainSource(ASOF, indices=["NIFTY"], persist=False)   # csv_text=None -> fetch
    assert src() == []
    assert FnoIncomeEngine(lot_size=75, strike_step=50.0, source=src).scan() == []


# ── cold-start / warm-up gate (review fix: NaN/degenerate must NOT sell) ──

def _engine(snaps, **kw):
    return FnoIncomeEngine(lot_size=75, strike_step=50.0, source=lambda: snaps, **kw)


def test_iv_rank_degenerate_window_is_nan_not_neutral():
    # A flat/single-point/empty window is UNKNOWN, not "median" — must be NaN so
    # it can't sneak past a 0.5 selling threshold.
    assert math.isnan(_iv_rank(0.15, [0.20]))          # one prior session
    assert math.isnan(_iv_rank(0.15, [0.18, 0.18]))    # flat window
    assert math.isnan(_iv_rank(0.15, []))              # empty


def test_cold_start_empty_history_emits_nothing():
    snaps = build_snapshots(_nifty_condor_frame(), indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=_hist([]))
    assert math.isnan(snaps[0]["iv_rank"]) and snaps[0]["iv_history_n"] == 0
    assert _engine(snaps).scan() == []          # NaN must NOT pass `< iv_rank_min`


def test_single_session_degenerate_history_emits_nothing():
    snaps = build_snapshots(_nifty_condor_frame(), indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=_hist([0.20]))
    assert math.isnan(snaps[0]["iv_rank"])
    assert _engine(snaps).scan() == []


def test_warmup_floor_blocks_until_enough_history():
    # Rich rank, but on a thin (n=2) window: a min_iv_history floor must wait.
    snaps = build_snapshots(_nifty_condor_frame(), indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=_hist([0.10, 0.14]))
    assert snaps[0]["iv_history_n"] == 2 and snaps[0]["iv_rank"] == pytest.approx(1.0)
    assert _engine(snaps, min_iv_history=20).scan() == []      # too few sessions -> wait
    assert len(_engine(snaps, min_iv_history=2).scan()) == 1   # enough -> emit


def test_idea_stamped_with_session_asof_not_wallclock():
    snaps = build_snapshots(_nifty_condor_frame(), indices=["NIFTY"], asof=ASOF,
                            iv_history_lookup=_hist([0.10, 0.14]))
    idea = _engine(snaps).scan()[0]
    assert idea.created_at.startswith(ASOF.isoformat())
    # idea-log per-day dedup key is the SESSION date, not the run date.
    assert idea_to_row(idea)["idea_date"] == ASOF.isoformat()


def test_extractors_tolerate_absent_columns():
    # build_snapshots-style frame missing underlying_px/lot_size entirely must not
    # raise (degrade to NaN -> registry/spot fallbacks).
    from qsde.fno.bhavcopy import extract_lot_size, extract_underlying_spot
    bare = pd.DataFrame([{"symbol": "NIFTY", "expiry": EXP, "strike": 25000.0,
                          "opt_type": "CE", "settle": 100.0, "iv": 0.15}])
    assert math.isnan(extract_underlying_spot(bare, "NIFTY"))
    assert math.isnan(extract_lot_size(bare, "NIFTY"))
