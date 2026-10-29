"""Mode policy and profile planning (pure): mixes sum to 100, ceilings hold, loosening needs an admin note, the trading-pot guard, and a mode change touches only the mode."""
import json

import pytest

from qsde.modes import policy as pol
from qsde.modes import profile as prof

P = pol.load_policy()


def cur(**kw):
    d = prof.blank(P["version"])
    d.update(kw)
    return d


# ── policy file ──────────────────────────────────────────────────────

def test_every_risk_profile_sums_to_100_and_debt_is_the_residual():
    for name, m in P["investor"]["risk_profiles"].items():
        assert m["equity"] + m["gold"] + m["debt"] == 100, name
        assert m["debt"] == 100 - m["equity"] - m["gold"]


def test_profiles_are_ordered_by_equity_and_match_the_owner_review():
    eq = [P["investor"]["risk_profiles"][k]["equity"] for k in ("conservative", "moderately_conservative", "balanced", "growth", "aggressive")]
    assert eq == sorted(eq) and eq == [25, 40, 55, 70, 82]
    assert P["investor"]["risk_profiles"]["conservative"] == {"equity": 25, "gold": 7.5, "debt": 67.5}


def test_a_broken_mix_is_refused_at_load():
    bad = json.loads(json.dumps(P))
    bad["investor"]["risk_profiles"]["growth"]["debt"] = 25
    with pytest.raises(pol.PolicyError, match="growth"):
        pol.validate_policy(bad)


def test_defaults_are_the_stricter_values_and_one_percent_is_never_enabled():
    d = pol.defaults()
    assert d["risk_per_trade_pct"] == 0.25 and d["open_risk_pct"] == 3.0 and d["daily_loss_pct"] == 1.5 and d["weekly_loss_pct"] == 4.0
    assert d["max_positions"] == 8 and d["max_pair_correlation"] == 0.55 and d["liquidity_cap_pct"] == 1.0
    assert pol.ceiling_for("risk_per_trade_pct", P, approved=True) == 0.5               # even with an approved engine
    assert pol.ceiling_for("open_risk_pct", P, approved=False) == 3.0 and pol.ceiling_for("open_risk_pct", P, approved=True) == 4.0
    assert P["trader_rules_fixed"]["averaging_down"] is False and P["trader_rules_fixed"]["live_order_placement"] is False


def test_no_engine_is_approved_today():
    assert pol.engine_approved() is False


def test_policy_file_holds_no_personal_amounts():
    txt = json.dumps(P)
    for forbidden in ("1,00,00,000", "5,00,000", "2028-12-31", "2041-12-31", "₹"):
        assert forbidden not in txt


# ── limits ───────────────────────────────────────────────────────────

def test_tightening_is_free_and_default_value_clears_the_deviation():
    r = pol.check_limit_changes({"risk_per_trade_pct": 0.1, "daily_loss_pct": 1.0}, None)
    assert not r.errors and not r.needs_approval and r.new == {"risk_per_trade_pct": 0.1, "daily_loss_pct": 1.0}
    r2 = pol.check_limit_changes({"risk_per_trade_pct": 0.25}, {"risk_per_trade_pct": 0.1})
    assert r2.new == {}


def test_loosening_up_to_the_ceiling_needs_approval_and_beyond_is_refused():
    r = pol.check_limit_changes({"risk_per_trade_pct": 0.5}, None)
    assert r.needs_approval == ["risk_per_trade_pct"] and not r.errors
    assert pol.check_limit_changes({"risk_per_trade_pct": 1.0}, None).errors                  # never enabled
    assert pol.check_limit_changes({"open_risk_pct": 4.0}, None).errors                      # ceiling 3 until an engine is approved
    assert pol.check_limit_changes({"max_positions": 9}, None).errors
    assert pol.check_limit_changes({"bogus": 1}, None).errors
    assert pol.check_limit_changes({"daily_loss_pct": -1}, None).errors


def test_plan_requires_an_admin_note_to_loosen_and_audits_it():
    u = prof.Update(limits={"risk_per_trade_pct": 0.5})
    assert prof.plan_update(cur(), u, is_admin=True).errors                                    # no note
    u.approval_note = "short"
    assert prof.plan_update(cur(), u, is_admin=True).errors                                    # too short
    u.approval_note = "owner reviewed paper results"
    assert prof.plan_update(cur(), u, is_admin=False).errors                                   # not an admin
    ok = prof.plan_update(cur(), u, is_admin=True)
    assert not ok.errors and ok.changes["limits"] == {"risk_per_trade_pct": 0.5}
    action, payload = ok.audits[0]
    assert action == "limits.change" and payload["approved"] == ["risk_per_trade_pct"] and "owner reviewed" in payload["approval_note"]


# ── pots ─────────────────────────────────────────────────────────────

def test_trading_pot_guard_and_override():
    ok = pol.check_pots(1_000_000, 50_000, 100_000, None, False)
    assert not ok["errors"] and ok["trading_share"] == pytest.approx(50_000 / 1_050_000)
    big = pol.check_pots(1_000_000, 100_000, 0, None, True)
    assert big["errors"]                                                                        # 9.1% > 5%, no note
    assert pol.check_pots(1_000_000, 100_000, 0, "owner accepts the higher trading share", True)["overridden"]
    assert pol.check_pots(1_000_000, 100_000, 0, "owner accepts the higher trading share", False)["errors"]   # a non-admin cannot override
    assert pol.check_pots(0, 0, None, None, False)["errors"] == []


def test_emergency_fund_is_not_investable():
    a = pol.check_pots(1_000_000, 40_000, 0, None, False)
    b = pol.check_pots(1_000_000, 40_000, 900_000, None, False)
    assert a["trading_share"] == b["trading_share"] and a["investable"] == b["investable"]


def test_an_unknown_emergency_fund_is_flagged_not_assumed():
    r = pol.check_pots(1_000_000, 10_000, None, None, False)
    assert any("emergency fund not recorded" in w for w in r["warnings"])


# ── mode change touches only the mode ────────────────────────────────

def test_mode_change_plan_contains_only_the_mode():
    c = cur(mode="investor", investment_capital=900_000.0, trading_capital=30_000.0, emergency_fund=100_000.0, limits={"daily_loss_pct": 1.0})
    plan = prof.plan_update(c, prof.Update(mode="trader"), is_admin=True)
    assert plan.changes == {"mode": "trader"} and [a for a, _ in plan.audits] == ["mode.change"]
    assert prof.plan_update(c, prof.Update(mode="investor"), is_admin=True).changes == {}      # no-op is not a change
    assert prof.plan_update(c, prof.Update(mode="scalper"), is_admin=True).errors


def test_capital_plan_merges_with_existing_amounts_and_validates_the_whole():
    c = cur(investment_capital=1_000_000.0, trading_capital=20_000.0)
    # raising only the trading pot is judged against the EXISTING investment pot
    assert prof.plan_update(c, prof.Update(trading_capital=200_000.0, set_fields={"trading_capital"}), True).errors
    ok = prof.plan_update(c, prof.Update(trading_capital=30_000.0, set_fields={"trading_capital"}), True)
    assert ok.changes == {"trading_capital": 30_000.0} and ok.audits[0][0] == "capital.change"
    assert ok.audits[0][1]["before"] == {"trading_capital": 20_000.0}


def test_pot_for_mode_lens():
    assert pol.pot_for_mode("trader") == "trading" and pol.pot_for_mode("investor") == "investment"
    with pytest.raises(pol.PolicyError):
        pol.pot_for_mode("x")
