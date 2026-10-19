"""Decision cards: sizing arithmetic, caps, regime effect, dates, determinism."""
import numpy as np
import pandas as pd
import pytest

from datetime import date

from qsde.decision.cards import EVIDENCE, PortfolioParams, add_sessions, build_cards
from qsde.ml.registry import get_horizon

AS_OF = pd.Timestamp("2026-10-16")           # a Friday
DATES = pd.bdate_range(end=AS_OF, periods=300)


def prices(vol=0.01, last=500.0, symbols=("AAA", "BBB", "CCC", "DDD")):
    rng = np.random.default_rng(3)
    rows = []
    for i, s in enumerate(symbols):
        r = rng.normal(0, vol * (1 + 0.3 * i), len(DATES))
        c = last * np.exp(np.cumsum(r))
        c = c * last / c[-1]
        rows.append(pd.DataFrame({"symbol": s, "date": DATES, "close": c}))
    return pd.concat(rows, ignore_index=True)


def scores(regime="neutral", symbols=("AAA", "BBB", "CCC", "DDD"), bucket=5, liq=5e9, dd=-0.1):
    return pd.DataFrame({"symbol": list(symbols), "date": AS_OF, "defensive_score": np.linspace(0.95, 0.85, len(symbols)),
                         "risk_bucket": bucket, "atr_pct": 0.02, "vol60": 0.012, "dd120": dd, "liq": liq, "warnings": "", "regime": regime})


def test_position_is_sized_so_a_stop_out_costs_the_risk_budget():
    p = PortfolioParams(capital=1_000_000, risk_per_position=0.005, max_position=0.25, horizon="position_20d")
    b = build_cards(scores(regime="risk_on"), prices(), p)
    c = b["cards"][0]
    sl = get_horizon("position_20d").barrier.sl_mult
    assert c["stop_distance"] == pytest.approx(sl * c["daily_vol"])
    assert c["stop"] == pytest.approx(c["last_close"] * (1 - c["stop_distance"]))
    assert c["sized_by"] == "risk budget"
    assert c["loss_pct_of_capital"] == pytest.approx(0.005, rel=0.02)          # floor() to whole shares can only reduce it
    assert c["loss_pct_of_capital"] <= 0.005 + 1e-9
    assert c["quantity"] == int(c["value"] // c["last_close"]) and c["evidence"] == EVIDENCE


def test_caps_bind_and_are_reported():
    base = dict(capital=1_000_000, risk_per_position=0.02, horizon="position_20d")
    cap = build_cards(scores(regime="risk_on"), prices(vol=0.003), PortfolioParams(max_position=0.03, **base))["cards"][0]
    assert cap["sized_by"] == "max position" and cap["weight"] <= 0.03 + 1e-9
    liq = build_cards(scores(regime="risk_on", liq=2e7), prices(vol=0.003), PortfolioParams(min_traded_value=1e6, max_position=0.25, **base))["cards"][0]
    assert liq["sized_by"] == "liquidity" and liq["value"] <= 0.01 * 2e7 + 1e-6


def test_regime_reduces_risk_and_total_exposure():
    p = PortfolioParams(capital=1_000_000, risk_per_position=0.005, max_position=0.25, max_positions=4)
    on = build_cards(scores(regime="risk_on"), prices(), p)
    off = build_cards(scores(regime="risk_off"), prices(), p)
    assert off["summary"]["exposure_cap"] == 0.5 and on["summary"]["exposure_cap"] == 0.95
    assert off["cards"][0]["value"] < on["cards"][0]["value"]
    assert off["summary"]["invested_pct"] <= 0.5 + 1e-9 and on["summary"]["invested_pct"] <= 0.95 + 1e-9


def test_total_invested_never_exceeds_capital_and_loss_summary_adds_up():
    p = PortfolioParams(capital=300_000, risk_per_position=0.01, max_position=0.25, max_positions=4)
    b = build_cards(scores(regime="risk_on"), prices(vol=0.004), p)
    s = b["summary"]
    assert s["invested"] <= 300_000 and s["cash"] == pytest.approx(300_000 - s["invested"])
    assert s["loss_if_all_stopped"] == pytest.approx(sum(c["loss_if_stopped"] for c in b["cards"]))
    assert s["positions"] == len(b["cards"]) <= 4 and s["dates_are_estimates"] is True


def test_filters_exclude_risky_illiquid_and_deep_drawdown_names():
    p = PortfolioParams()
    assert build_cards(scores(bucket=3), prices(), p)["cards"] == []
    assert build_cards(scores(liq=1e6), prices(), p)["cards"] == []
    assert build_cards(scores(dd=-0.5), prices(), p)["cards"] == []


def test_names_without_history_are_skipped_with_a_reason():
    b = build_cards(scores(symbols=("AAA", "ZZZ")), prices(symbols=("AAA",)), PortfolioParams(capital=1_000_000))
    assert [c["symbol"] for c in b["cards"]] == ["AAA"] and ("ZZZ", "not enough price history") in b["summary"]["skipped"]


def test_dates_count_sessions_and_skip_weekends_and_known_holidays():
    assert add_sessions(date(2026, 10, 16), 1) == date(2026, 10, 19)                       # Friday -> Monday
    assert add_sessions(date(2026, 10, 16), 5) == date(2026, 10, 23)
    assert add_sessions(date(2026, 10, 16), 1, frozenset({date(2026, 10, 19)})) == date(2026, 10, 20)
    b = build_cards(scores(), prices(), PortfolioParams(horizon="swing_5d"))
    assert b["summary"]["entry_date"] == "2026-10-19" and b["summary"]["exit_by"] == "2026-10-26"         # entry + 5 sessions
    assert b["cards"][0]["review_by"] <= b["cards"][0]["exit_by"]


def test_deterministic_and_parameter_validation():
    a, b = build_cards(scores(), prices(), PortfolioParams()), build_cards(scores(), prices(), PortfolioParams())
    assert a == b
    for bad in (PortfolioParams(capital=-1), PortfolioParams(risk_per_position=0.5), PortfolioParams(max_positions=0)):
        with pytest.raises(ValueError):
            build_cards(scores(), prices(), bad)


def test_a_name_that_is_a_near_copy_of_a_chosen_one_is_skipped_as_the_same_risk_twice():
    px = prices(symbols=("AAA", "BBB"))
    a = px[px["symbol"] == "AAA"].reset_index(drop=True)
    twin = a.assign(symbol="TWN", close=a["close"] * 1.0001)                 # moves exactly with AAA
    allp = pd.concat([px, twin], ignore_index=True)
    sc = scores(symbols=("AAA", "TWN", "BBB"))
    b = build_cards(sc, allp, PortfolioParams(capital=1_000_000, max_positions=3))
    assert [c["symbol"] for c in b["cards"]] == ["AAA", "BBB"]
    assert any(sym == "TWN" and "too correlated with AAA" in why for sym, why in b["summary"]["skipped"])
    off = build_cards(sc, allp, PortfolioParams(capital=1_000_000, max_positions=3, max_pair_corr=1.01))
    assert "TWN" in [c["symbol"] for c in off["cards"]]
