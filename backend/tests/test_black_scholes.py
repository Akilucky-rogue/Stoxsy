"""Tests for the Black-Scholes options math (F&O increment 1).

Pure math — no DB. Validates pricing identities, IV round-trip recovery, and
Greek signs so the F&O-income engine can trust IV + Greeks built on this.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qsde.fno.black_scholes import bs_greeks, bs_price, implied_vol

S, K, T, R, SIG = 100.0, 100.0, 0.10, 0.065, 0.20   # ATM, ~25 sessions, 20% vol


def test_put_call_parity():
    c = bs_price(S, K, T, R, SIG, "CE")
    p = bs_price(S, K, T, R, SIG, "PE")
    assert (c - p) == pytest.approx(S - K * math.exp(-R * T), abs=1e-9)


def test_iv_roundtrip_call_and_put():
    for ot in ("CE", "PE"):
        price = bs_price(S, K, T, R, SIG, ot)
        iv = implied_vol(price, S, K, T, ot, r=R)
        assert iv == pytest.approx(SIG, abs=1e-4)


def test_iv_roundtrip_otm_call():
    price = bs_price(S, 110.0, T, R, 0.35, "CE")
    iv = implied_vol(price, S, 110.0, T, "CE", r=R)
    assert iv == pytest.approx(0.35, abs=1e-4)


def test_atm_call_delta_near_half():
    d = bs_greeks(S, K, T, R, SIG, "CE")["delta"]
    assert 0.5 < d < 0.65


def test_greek_signs():
    cg = bs_greeks(S, K, T, R, SIG, "CE")
    pg = bs_greeks(S, K, T, R, SIG, "PE")
    assert cg["delta"] > 0 and pg["delta"] < 0          # call long / put short delta
    assert cg["gamma"] > 0 and cg["vega"] > 0           # gamma & vega positive (both legs)
    assert pg["gamma"] > 0 and pg["vega"] > 0
    assert cg["theta"] < 0                              # long option bleeds time value
    # put-call delta relationship: delta_call - delta_put ~= 1
    assert (cg["delta"] - pg["delta"]) == pytest.approx(1.0, abs=1e-6)


def test_expiry_returns_intrinsic():
    assert bs_price(110, 100, 0.0, R, SIG, "CE") == pytest.approx(10.0)   # ITM call
    assert bs_price(90, 100, 0.0, R, SIG, "PE") == pytest.approx(10.0)    # ITM put
    assert bs_price(90, 100, 0.0, R, SIG, "CE") == pytest.approx(0.0)     # OTM call


def test_iv_nan_when_price_below_intrinsic():
    # call intrinsic is 15 (S=115,K=100); a price of 5 is impossible -> NaN
    assert math.isnan(implied_vol(5.0, 115.0, 100.0, T, "CE", r=R))
    assert math.isnan(implied_vol(0.0, S, K, T, "CE", r=R))
