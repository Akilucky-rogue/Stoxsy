"""Tax-lot engine: FIFO, split vs bonus, reconciliation against holdings, holding-period classes. Synthetic data only."""
from datetime import date

import pytest

from qsde.portfolio import lots as L

D = date
TODAY = D(2026, 10, 25)


def tx(d, side, q, p, **k):
    return {"trade_date": d, "side": side, "quantity": q, "price": p, "symbol": k.pop("symbol", "AAA"), "isin": k.pop("isin", "INE000A01010"), **k}


def test_fifo_sells_the_oldest_lots_first_and_partially():
    r = L.fifo([tx(D(2025, 1, 10), "buy", 10, 100), tx(D(2025, 2, 10), "buy", 10, 120), tx(D(2025, 3, 10), "sell", 15, 150)])
    assert [(l.date, l.qty, l.cost) for l in r.lots] == [(D(2025, 2, 10), 5, 120)] and r.unmatched_sell == 0


def test_inventory_never_goes_negative_a_sell_with_no_lot_is_counted_not_invented():
    r = L.fifo([tx(D(2025, 1, 10), "buy", 5, 100), tx(D(2025, 2, 10), "sell", 12, 150)])
    assert r.lots == [] and r.unmatched_sell == pytest.approx(7)


def test_same_day_buy_is_available_to_a_same_day_sell_when_no_time_is_known():
    r = L.fifo([tx(D(2025, 1, 10), "sell", 5, 101), tx(D(2025, 1, 10), "buy", 5, 100)])
    assert r.lots == [] and r.unmatched_sell == 0


def test_a_split_scales_quantity_and_cost_and_keeps_the_purchase_date():
    ev = L.parse_event(D(2025, 6, 1), "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 0.2)
    assert ev.ok and ev.split == pytest.approx(5) and ev.bonus == 0
    r = L.fifo([tx(D(2024, 1, 10), "buy", 10, 1000)], [ev], TODAY)
    assert [(l.date, l.qty, l.cost, l.origin) for l in r.lots] == [(D(2024, 1, 10), 50, 200, "buy")]                   # date preserved


def test_a_bonus_adds_a_new_zero_cost_lot_and_leaves_the_original_untouched():
    ev = L.parse_event(D(2025, 6, 1), "Bonus 1:2", 2 / 3)
    assert ev.ok and ev.bonus == pytest.approx(0.5)
    r = L.fifo([tx(D(2024, 1, 10), "buy", 15, 300)], [ev], TODAY)
    assert (r.lots[0].date, r.lots[0].qty, r.lots[0].cost) == (D(2024, 1, 10), 15, 300)                              # original: same date, quantity and cost
    b = r.lots[1]
    assert (b.date, b.qty, b.cost, b.origin, b.date_basis) == (D(2025, 6, 1), 7, 0.0, "bonus", "bonus_ex_date_proxy")   # floor(7.5): fractional entitlement is not a share
    assert "allotment" in b.note


def test_split_and_bonus_together_and_sells_take_original_lots_before_bonus_lots():
    ev = L.parse_event(D(2025, 6, 1), "Bonus 1:1/Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", 0.25)
    assert ev.ok and ev.split == pytest.approx(2) and ev.bonus == pytest.approx(1)
    r = L.fifo([tx(D(2024, 1, 10), "buy", 10, 400), tx(D(2025, 7, 1), "sell", 20, 150)], [ev], TODAY)
    # 10 -> 20 after the split, +20 bonus lot; selling 20 takes the original lot
    assert [(l.origin, l.qty) for l in r.lots] == [("bonus", 20)]


def test_a_trade_on_the_ex_date_is_already_in_post_event_terms():
    ev = L.parse_event(D(2025, 6, 1), "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 5/- Per Share", 0.5)
    r = L.fifo([tx(D(2025, 5, 1), "buy", 10, 100), tx(D(2025, 6, 1), "buy", 4, 52)], [ev], TODAY)
    assert [(l.qty, l.cost) for l in r.lots] == [(20, 50), (4, 52)]


def test_events_that_cannot_be_trusted_are_not_applied_and_say_why():
    assert not L.parse_event(D(2025, 6, 1), "Dividend - Rs 5 Per Share", None).ok
    assert "NSE lists 0.9000" in L.parse_event(D(2025, 6, 1), "Bonus 1:1", 0.9).reason
    assert not L.parse_event(D(2025, 6, 1), "Face Value Consolidation - From Rs 1/- Per Share To Rs 10/- Per Share", 10).ok
    bad = L.parse_event(D(2025, 6, 1), "Bonus 1:1", 0.9)
    r = L.fifo([tx(D(2024, 1, 10), "buy", 10, 100)], [bad], TODAY)
    assert [l.qty for l in r.lots] == [10] and r.skipped == [bad]


def test_an_event_before_anything_was_held_is_ignored_silently():
    ev = L.parse_event(D(2024, 1, 1), "Bonus 1:1", 0.5)
    r = L.fifo([tx(D(2025, 1, 10), "buy", 10, 100)], [ev], TODAY)
    assert [l.qty for l in r.lots] == [10] and r.skipped == [] and r.applied == []


def rows_by_status(rows):
    return sorted((r["status"], r["quantity"], r["buy_date"]) for r in rows)


def test_reconciled_when_the_lots_equal_the_holdings():
    rows, summ = L.build_equity(txns=[tx(D(2025, 1, 10), "buy", 10, 100)], reported=[], holdings={"AAA": {"quantity": 10, "avg_cost": 100, "isin": "INE000A01010"}}, events={}, today=TODAY)
    assert summ[0]["status"] == "RECONCILED" and rows[0]["status"] == "RECONCILED" and rows[0]["buy_date"] == D(2025, 1, 10)


def test_holdings_above_the_lots_become_an_undated_gap_lot_with_the_exact_missing_quantity():
    rows, summ = L.build_equity(txns=[tx(D(2025, 1, 10), "buy", 10, 100)], reported=[], holdings={"AAA": {"quantity": 25, "avg_cost": 90, "isin": None}}, events={}, today=TODAY)
    assert summ[0]["status"] == "UNRECONCILED_LOT_GAP" and summ[0]["gap_qty"] == pytest.approx(15)
    gap = [r for r in rows if r["origin"] == "gap"][0]
    assert gap["buy_date"] is None and gap["quantity"] == 15 and gap["unit_cost"] is None and gap["status"] == "DATE_PENDING"          # no guessed date, no guessed cost
    assert any(r["buy_date"] == D(2025, 1, 10) and r["status"] == "UNRECONCILED_LOT_GAP" for r in rows)


def test_lots_above_the_holdings_are_a_surplus_and_nothing_is_reduced_silently():
    rows, summ = L.build_equity(txns=[tx(D(2025, 1, 10), "buy", 10, 100)], reported=[], holdings={"AAA": {"quantity": 4, "avg_cost": 100, "isin": None}}, events={}, today=TODAY)
    assert summ[0]["status"] == "UNRECONCILED_SURPLUS" and summ[0]["gap_qty"] == pytest.approx(-6) and rows[0]["quantity"] == 10


def test_holdings_with_no_trades_keep_a_date_pending_placeholder():
    rows, summ = L.build_equity(txns=[], reported=[], holdings={"BBB": {"quantity": 30, "avg_cost": 55.5, "isin": "INE000B01010"}}, events={}, today=TODAY)
    assert summ[0]["status"] == "DATE_PENDING" and rows[0]["buy_date"] is None and rows[0]["unit_cost"] == 55.5 and rows[0]["origin"] == "holding_snapshot"


def test_without_a_holdings_snapshot_lots_are_unverified_not_reconciled():
    rows, summ = L.build_equity(txns=[tx(D(2025, 1, 10), "buy", 10, 100)], reported=[], holdings=None, events={}, today=TODAY)
    assert summ[0]["status"] == "UNVERIFIED" and rows[0]["status"] == "UNVERIFIED"


def test_a_fully_sold_position_leaves_no_lots():
    rows, summ = L.build_equity(txns=[tx(D(2025, 1, 10), "buy", 10, 100), tx(D(2025, 2, 10), "sell", 10, 110)], reported=[], holdings={}, events={}, today=TODAY)
    assert rows == [] and summ == []


def test_the_brokers_own_lot_report_is_used_as_stated_and_never_adjusted_by_us():
    ev = L.parse_event(D(2025, 6, 1), "Bonus 1:1", 0.5)
    rep = [{"symbol": "AAA", "isin": None, "buy_date": D(2024, 3, 1), "quantity": 20, "unit_cost": 50.0}]            # already post-bonus in the broker's report
    rows, summ = L.build_equity(txns=[], reported=rep, holdings={"AAA": {"quantity": 20, "avg_cost": 50, "isin": None}}, events={"AAA": [ev]}, today=TODAY)
    assert summ[0]["status"] == "RECONCILED" and summ[0]["source"] == "broker_lot_report" and rows[0]["quantity"] == 20 and rows[0]["unit_cost"] == 50


def test_the_source_that_reconciles_wins_and_a_skipped_corporate_action_asks_for_review():
    bad = L.parse_event(D(2025, 6, 1), "Bonus 1:1", 0.9)
    rows, summ = L.build_equity(txns=[tx(D(2024, 1, 10), "buy", 10, 100)], reported=[], holdings={"AAA": {"quantity": 20, "avg_cost": 50, "isin": None}}, events={"AAA": [bad]}, today=TODAY)
    assert summ[0]["status"] == "NEEDS_REVIEW" and any("corporate action on 2025-06-01 not applied" in n for n in summ[0]["notes"])


def test_long_term_boundary_is_calendar_months_plus_one_day_not_365_days():
    assert L.long_term_from(D(2025, 1, 15)) == D(2026, 1, 16)
    assert L.long_term_from(D(2024, 2, 29)) == D(2025, 3, 1)                       # 12 months after a leap day is 28 Feb; more than 12 months starts 1 Mar
    assert L.long_term_from(D(2023, 3, 1)) == D(2024, 3, 2)                        # a leap year in between: 366 days later, not 365
    on = L.classify_lot(D(2025, 1, 15), D(2026, 1, 15))
    assert on["tax_class"] == "NEAR_LONG_TERM" and on["days_to_long_term"] == 1    # exactly 12 months: still short-term
    assert L.classify_lot(D(2025, 1, 15), D(2026, 1, 16))["tax_class"] == "LONG_TERM"


def test_holding_period_classes_and_what_is_not_classified():
    assert L.classify_lot(D(2026, 9, 1), TODAY)["tax_class"] == "SHORT_TERM"
    near = L.classify_lot(D(2025, 11, 20), TODAY)
    assert near["tax_class"] == "NEAR_LONG_TERM" and near["days_to_long_term"] == (D(2026, 11, 21) - TODAY).days
    assert L.classify_lot(None, TODAY)["tax_class"] == "UNKNOWN_DATE"
    nc = L.classify_lot(D(2024, 1, 1), TODAY, classifiable=False, why_not="debt fund")
    assert nc["tax_class"] == "NOT_CLASSIFIED" and nc["reason"] == "debt fund" and nc["long_term_from"] is None


def test_mf_units_fifo_and_closing_balance_check():
    t = [{"isin": "INF000A01010", "folio": "123", "scheme_name": "Demo Fund", "trade_date": D(2025, 1, 5), "side": "buy", "quantity": 100.5, "price": 20.0},
         {"isin": "INF000A01010", "folio": "123", "scheme_name": "Demo Fund", "trade_date": D(2025, 3, 5), "side": "buy", "quantity": 50.25, "price": 22.0},
         {"isin": "INF000A01010", "folio": "123", "scheme_name": "Demo Fund", "trade_date": D(2025, 6, 5), "side": "sell", "quantity": 120.0, "price": 25.0}]
    rows, summ = L.build_mf(txns=t, closing={("123", "INF000A01010"): 30.75})
    assert summ[0]["status"] == "RECONCILED" and [(r["buy_date"], r["quantity"]) for r in rows] == [(D(2025, 3, 5), 30.75)]
    rows, summ = L.build_mf(txns=t, closing={})
    assert summ[0]["status"] == "UNVERIFIED" and "closing unit balance" in summ[0]["notes"][0]
    rows, summ = L.build_mf(txns=t, closing={("123", "INF000A01010"): 40.0})
    assert summ[0]["status"] == "UNRECONCILED_LOT_GAP" and summ[0]["gap_qty"] == pytest.approx(9.25)


def test_two_folios_of_the_same_scheme_are_separate_inventories():
    mk = lambda f, s, q: {"isin": "INF000A01010", "folio": f, "scheme_name": "X", "trade_date": D(2025, 1, 5), "side": s, "quantity": q, "price": 10.0}      # noqa: E731
    rows, summ = L.build_mf(txns=[mk("A", "buy", 10), mk("B", "buy", 5), mk("A", "sell", 10)], closing={})
    assert [(r["folio"], r["quantity"]) for r in rows] == [("B", 5)]
