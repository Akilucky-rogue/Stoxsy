"""Trader risk engine: every refusal names its rule, sizing never exceeds a limit (property tests), and the streak and weekly reductions work as written."""
from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings, strategies as st

from qsde.modes import policy as pol
from qsde.trader import risk as R

L = R.Limits.from_effective(pol.defaults())                     # 0.25% per trade, 3% open risk, 1.5% daily, 4% weekly, 8 positions, 0.55 correlation, 1% liquidity
T0 = datetime(2026, 10, 26, 10, 0)
CAP = 1_000_000.0


def idea(**kw):
    base = dict(symbol="AAA", side="long", entry=100.0, stop=95.0, cost_per_unit=0.30, gap_allowance=0.50, adv_value=5e8, correlations={}, lot_size=1)
    base.update(kw)
    return base


def pos(sym="BBB", qty=100, entry=100.0, stop=95.0, last=100.0, prev=100.0, risk=600.0):
    return R.OpenPosition(sym, qty, entry, stop, last, prev, risk)


def state(**kw):
    return R.State(capital=kw.pop("capital", CAP), **kw)


def trade(i, reason, closed_hours_after=0, opened_hours_after=None):
    base = T0 + timedelta(days=i)
    return R.TradeRecord(f"S{i}", base + timedelta(hours=(opened_hours_after or 0)), base + timedelta(hours=closed_hours_after + 1), reason, -1000.0 if reason == "stop" else 1000.0)


# ── sizing ───────────────────────────────────────────────────────────

def test_size_is_the_risk_budget_over_the_all_in_loss_per_unit():
    d = R.evaluate(state(), L, **idea())
    assert d.allowed
    z = d.sizing
    assert z.loss_per_unit == pytest.approx(5.0 + 0.30 + 0.50)                    # stop distance + cost + gap allowance
    assert z.qty == int(CAP * 0.0025 / 5.8) == 431 and z.binding == "risk_per_trade"
    assert z.risk_rupees == pytest.approx(431 * 5.8) and z.risk_rupees <= CAP * 0.0025
    assert z.risk_pct <= 0.0025 + 1e-12


def test_liquidity_cap_binds_in_a_thin_stock_and_lot_size_rounds_down():
    d = R.evaluate(state(), L, **idea(adv_value=2e6))                                # 1% of Rs 20 lakh = Rs 20,000 = 200 shares
    assert d.sizing.qty == 200 and d.sizing.binding == "liquidity" and any("capped by 'liquidity'" in n for n in d.notes)
    lot = R.evaluate(state(), L, **idea(lot_size=75))
    assert lot.sizing.qty % 75 == 0 and lot.sizing.qty == (431 // 75) * 75


def test_free_cash_in_the_trading_pot_caps_size():
    s = state(capital=100_000.0)
    d = R.evaluate(s, L, **idea(entry=1000.0, stop=990.0))
    assert d.allowed and d.sizing.position_value <= 100_000.0
    big = state(capital=100_000.0, open_positions=[pos(qty=90, last=1000.0, risk=100.0)])          # Rs 90,000 already invested
    d2 = R.evaluate(big, L, **idea(entry=1000.0, stop=990.0))
    assert d2.allowed and d2.sizing.position_value <= 10_000.0 and d2.sizing.binding == "free_cash"


def test_the_remaining_open_risk_budget_shrinks_a_new_idea_and_a_full_cap_refuses_it():
    s = state(open_positions=[pos("B1", risk=14_000.0), pos("B2", risk=14_000.0)])                 # 2.8% of 3% used
    d = R.evaluate(s, L, **idea())
    assert d.allowed and d.sizing.binding == "open_risk_cap" and d.sizing.risk_rupees <= 0.03 * CAP - 28_000 + 1e-6
    full = state(open_positions=[pos("B1", risk=30_000.0)])
    r = R.evaluate(full, L, **idea())
    assert not r.allowed and "open risk" in r.reasons[0] and "3%" in r.reasons[0]


# ── refusals ─────────────────────────────────────────────────────────

def test_every_refusal_names_the_rule_value_and_limit():
    assert "trading capital is not set" in R.evaluate(R.State(capital=0.0), L, **idea()).reasons[0]
    assert "short selling" in R.evaluate(state(), L, **idea(side="short")).reasons[0]
    assert "stop 105" in R.evaluate(state(), L, **idea(stop=105.0)).reasons[0] and "below the entry" in R.evaluate(state(), L, **idea(stop=105.0)).reasons[0]
    assert "research and paper only" in R.evaluate(state(), L, **idea(), instrument="option").reasons[0]
    assert "no engine is approved" in R.evaluate(state(), L, **idea(), evidence="APPROVED").reasons[0]
    assert R.evaluate(state(), L, **idea(), evidence="APPROVED", engine_approved=True).allowed
    assert "sessions behind" in R.evaluate(state(price_sessions_behind=3), L, **idea()).reasons[0]
    nine = state(open_positions=[pos(f"P{i}", risk=100.0) for i in range(8)])
    assert "maximum is 8" in R.evaluate(nine, L, **idea()).reasons[0]
    same = state(open_positions=[pos("AAA", entry=100.0)])
    assert "already open" in R.evaluate(same, L, **idea(entry=96.0, stop=91.0)).reasons[0] and "averaging down" in R.evaluate(same, L, **idea(entry=96.0, stop=91.0)).reasons[0]
    cor = R.evaluate(state(open_positions=[pos("BBB")]), L, **idea(correlations={"BBB": 0.71}))
    assert not cor.allowed and "0.71" in cor.reasons[0] and "0.55" in cor.reasons[0]
    assert R.evaluate(state(open_positions=[pos("BBB")]), L, **idea(correlations={"BBB": 0.55})).allowed        # at the limit is allowed
    tiny = R.evaluate(state(capital=10_000.0), L, **idea(entry=5000.0, stop=4900.0))
    assert not tiny.allowed and "not even one share" in tiny.reasons[0]


def test_the_daily_loss_limit_counts_realised_and_unrealised_and_stops_new_ideas():
    realised = state(realised_today=-15_000.0)                                       # exactly 1.5%
    assert "daily loss limit" in R.evaluate(realised, L, **idea()).reasons[0]
    mixed = state(realised_today=-7_000.0, open_positions=[pos("B1", qty=1000, last=91.5, prev=100.0, risk=4000.0)])        # -7,000 realised, -8,500 unrealised
    assert R.daily_pnl(mixed) == pytest.approx(-15_500.0) and not R.evaluate(mixed, L, **idea()).allowed
    gain = state(realised_today=-7_000.0, open_positions=[pos("B1", qty=1000, last=105.0, prev=100.0, risk=4000.0)])
    assert R.evaluate(gain, L, **idea()).allowed                                      # an unrealised gain offsets: -2,000 net


def test_a_weekly_breach_is_noted_and_halves_next_weeks_size_not_this_weeks_permission():
    s = state(realised_week=-41_000.0)
    d = R.evaluate(s, L, **idea())
    assert d.allowed and any("weekly loss limit reached" in n for n in d.notes)
    nxt = state(previous_week_pnl=-41_000.0)
    full, half = R.evaluate(state(), L, **idea()), R.evaluate(nxt, L, **idea())
    assert half.sizing.multiplier == 0.5 and abs(half.sizing.qty - full.sizing.qty / 2) <= 1 and any("halved" in n for n in half.notes)


def test_three_stops_in_a_row_halve_the_next_five_trades_and_then_normal_size_returns():
    stops = [trade(i, "stop") for i in range(3)]
    assert R.streak_status(stops)["reduced_trades_left"] == 5
    assert R.size_multiplier(state(trades=stops), L)[0] == 0.5
    opened = stops + [R.TradeRecord(f"N{i}", T0 + timedelta(days=10 + i)) for i in range(5)]                 # five trades opened after the trigger
    assert R.streak_status(opened)["reduced_trades_left"] == 0 and R.size_multiplier(state(trades=opened), L)[0] == 1.0
    broken = [trade(0, "stop"), trade(1, "stop"), trade(2, "target"), trade(3, "stop")]
    assert R.streak_status(broken)["stops_in_row"] == 1 and R.streak_status(broken)["reduced_trades_left"] == 0      # a win resets the run before 3
    both = state(trades=stops, previous_week_pnl=-50_000.0)
    assert R.size_multiplier(both, L)[0] == 0.25                                       # both reductions apply


def test_meters_show_how_much_of_each_limit_is_used():
    s = state(realised_today=-6_000.0, open_positions=[pos(risk=9_000.0)])
    m = R.meters(s, L)
    assert m["open_risk_pct"] == pytest.approx(0.9) and m["daily_limit_used"] == pytest.approx(0.4) and m["positions"] == 1 and m["free_cash"] == pytest.approx(CAP - 100 * 100.0)


# ── properties: no input may make an allowed idea exceed a limit ─────

@settings(max_examples=400, deadline=None)
@given(capital=st.floats(50_000, 5e7), entry=st.floats(5, 20_000), stop_frac=st.floats(0.002, 0.4), cost=st.floats(0, 50), gap=st.floats(0, 200), adv=st.floats(1e5, 1e10),
       n_open=st.integers(0, 7), open_risk_each=st.floats(0, 0.004), realised=st.floats(-0.02, 0.02), prevw=st.floats(-0.1, 0.1), lot=st.sampled_from([1, 1, 25, 75, 500]),
       stops=st.integers(0, 6))
def test_an_allowed_idea_never_exceeds_any_limit(capital, entry, stop_frac, cost, gap, adv, n_open, open_risk_each, realised, prevw, lot, stops):
    stop = entry * (1 - stop_frac)
    opens = [R.OpenPosition(f"O{i}", 10, entry, entry * 0.95, entry, entry, open_risk_each * capital) for i in range(n_open)]
    tr = [trade(i, "stop") for i in range(stops)]
    s = R.State(capital=capital, open_positions=opens, realised_today=realised * capital, previous_week_pnl=prevw * capital, trades=tr)
    d = R.evaluate(s, L, symbol="ZZZ", side="long", entry=entry, stop=stop, cost_per_unit=cost, gap_allowance=gap, adv_value=adv, correlations={}, lot_size=lot)
    if not d.allowed:
        assert d.reasons and d.sizing is None
        return
    z = d.sizing
    mult, _ = R.size_multiplier(s, L)
    assert z.qty >= 1 and z.qty % lot == 0 if lot > 1 else z.qty >= 1
    assert z.risk_rupees <= capital * L.risk_per_trade_pct / 100 * mult * (1 + 1e-9) + 1e-9                  # never more than the (reduced) per-trade budget
    assert R.open_risk(s) + z.risk_rupees <= capital * L.open_risk_pct / 100 * (1 + 1e-9) + 1e-9               # never over the open-risk cap
    assert z.qty * entry <= L.liquidity_cap_pct / 100 * adv * (1 + 1e-9) + entry                              # liquidity cap (floor to whole units)
    assert z.position_value <= max(0.0, capital - R.invested(s)) + 1e-6                                       # never more than the free cash of the pot
    assert R.daily_pnl(s) > -L.daily_loss_pct / 100 * capital                                                 # not on a day the limit is hit
    assert len(opens) < L.max_positions
