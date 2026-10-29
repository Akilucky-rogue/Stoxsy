"""File reading and row parsing for broker reports. Synthetic files only: every name, code and number below is made up (no realistic PAN, client code or DP id)."""
import io
from datetime import date

import openpyxl
import pytest

from qsde.portfolio import lot_parsers as P, tabular as T

BANNER = ["Demo Back Office Report,,,,,,", "Client Name: TEST PERSON,,,,,,", "Client Code: ZZ0000,,,,,,", "PAN: AAAAA0000A DP ID: IN000000,,,,,,", "From 01-04-2025 To 31-03-2026,,,,,,", ",,,,,,"]
TRADE_HEADER = "Trade Date,Scrip Name,ISIN,Buy/Sell,Qty,Rate,Trade No,Segment"
RES = P.Resolver({"AAA", "BBB", "NEWCO"}, {"INE000A01010": "AAA", "INE000N01010": "NEWCO"}, {"OLDCO": "NEWCO"})


def csv_bytes(*lines):
    return "\n".join(lines).encode("utf-8")


def test_the_header_is_found_under_a_banner_and_the_banner_is_only_counted_never_kept():
    raw = csv_bytes(*BANNER, TRADE_HEADER, "10-04-2025,AAA,INE000A01010,Buy,10,100.50,T1,EQ", "11-04-2025,BBB,,Sell,5,50,T2,EQ", "Total,,,,15,,,")
    t = T.read_table(raw, "tradebook.csv", P.TRADE_VOCAB)
    assert t.skipped_banner == 6 and t.skipped_footer == 1 and len(t.rows) == 2 and t.header[0] == "Trade Date"
    dump = repr(t.__dict__)
    assert "TEST PERSON" not in dump and "AAAAA0000A" not in dump and "ZZ0000" not in dump and "IN000000" not in dump


def test_an_xlsx_workbook_with_a_banner_reads_the_same_way():
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in [["Demo Report"], ["Client Name", "TEST PERSON"], [], ["Trade Date", "Scrip Name", "ISIN", "Buy/Sell", "Qty", "Rate", "Trade No", "Segment"],
              [date(2025, 4, 10), "AAA", "INE000A01010", "B", 10, 100.5, "T1", "EQ"], ["Grand Total", None, None, None, 10]]:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    t = T.read_table(buf.getvalue(), "tradebook.xlsx", P.TRADE_VOCAB)
    assert t.skipped_banner == 3 and t.skipped_footer == 1 and t.rows[0][0] == "2025-04-10" and t.rows[0][4] == "10"
    out = P.parse_trades(t, RES)
    assert out.rows[0]["trade_date"] == date(2025, 4, 10) and out.rows[0]["side"] == "buy" and out.rows[0]["price"] == 100.5


def test_old_xls_and_pdf_are_refused_with_a_clear_instruction():
    with pytest.raises(ValueError, match="save it as .xlsx or .csv"):
        T.read_table(b"\xd0\xcf\x11\xe0", "report.xls", P.TRADE_VOCAB)
    with pytest.raises(ValueError, match="PDF statements are not read"):
        T.read_table(b"%PDF-1.4 ...", "cas.pdf", P.MF_VOCAB)


def test_a_file_that_is_not_the_expected_kind_is_refused_and_lists_the_fields_it_wanted():
    with pytest.raises(ValueError, match="no header row found"):
        T.read_table(csv_bytes("a,b,c", "1,2,3"), "x.csv", P.TRADE_VOCAB)


def test_trades_are_parsed_resolved_by_isin_or_rename_and_derivatives_are_skipped_and_counted():
    raw = csv_bytes(TRADE_HEADER, "10-04-2025,AAA,INE000A01010,Buy,10,100.50,T1,EQ", "12-04-2025,OLDCO,,Sell,5,50,T2,EQ", "13-04-2025,NIFTY25APRFUT,,Buy,50,22000,T3,FO",
                    "14-04-2025,ZZZ,,Buy,5,10,T4,EQ", "bad-date,AAA,,Buy,5,10,T5,EQ", "15-04-2025,AAA,,Hold,5,10,T6,EQ", "16-04-2025,AAA,,Buy,5,,T7,EQ")
    out = P.parse_trades(T.read_table(raw, "t.csv", P.TRADE_VOCAB), RES)
    assert [(r["symbol"], r["side"], r["trade_date"]) for r in out.rows] == [("AAA", "buy", date(2025, 4, 10)), ("NEWCO", "sell", date(2025, 4, 12))]
    assert out.skipped_other_segment == 1 and out.n_rejected == 4
    reasons = " | ".join(i["reason"] for i in out.issues)
    assert "no NSE listing" in reasons and "trade date" in reasons and "buy from sell" in reasons and "price" in reasons


def test_issues_never_echo_free_text_from_the_file():
    raw = csv_bytes(TRADE_HEADER, "10-04-2025,Client Name: TEST PERSON PAN AAAAA0000A,,Buy,10,100,T1,EQ")
    out = P.parse_trades(T.read_table(raw, "t.csv", P.TRADE_VOCAB), RES)
    assert out.n_rejected == 1 and "TEST" not in repr(out.issues) and "AAAAA" not in repr(out.issues) and out.issues[0]["symbol"] == "<unreadable>"


def test_buy_and_sell_quantity_columns_work_when_there_is_no_side_column():
    raw = csv_bytes("Trade Date,Symbol,Buy Qty,Sell Qty,Price", "10-04-2025,AAA,10,0,100", "11-04-2025,AAA,0,4,110", "12-04-2025,AAA,3,3,110")
    out = P.parse_trades(T.read_table(raw, "t.csv", P.TRADE_VOCAB), RES)
    assert [(r["side"], r["quantity"]) for r in out.rows] == [("buy", 10), ("sell", 4)] and out.n_rejected == 1


def test_reported_lots_take_a_price_or_derive_it_from_the_value():
    raw = csv_bytes("Scrip Name,ISIN,Buy Date,Qty,Buy Value", "AAA,INE000A01010,01-02-2024,10,1000", "BBB,,02-03-2024,0,5")
    out = P.parse_reported_lots(T.read_table(raw, "lots.csv", P.LOT_VOCAB), RES)
    assert out.rows == [{"symbol": "AAA", "isin": "INE000A01010", "buy_date": date(2024, 2, 1), "quantity": 10.0, "unit_cost": 100.0}] and out.n_rejected == 1


def test_holdings_with_a_banner_and_alternative_quantity_title():
    raw = csv_bytes(*BANNER, "Scrip Name,ISIN,Holding Quantity,Average Price", "AAA,INE000A01010,40,95.25", "BBB,,10,20", "Total,,50,")
    t = T.read_table(raw, "holdings.csv", P.HOLDINGS_VOCAB)
    out = P.parse_holdings_table(t, {"AAA", "BBB"}, {}, {})
    assert [(h["symbol"], h["quantity"], h["avg_cost"]) for h in out.holdings] == [("AAA", 40.0, 95.25), ("BBB", 10.0, 20.0)]


def test_mf_types_are_classified_and_unknown_ones_are_reported_not_guessed():
    c = P.classify_mf_type
    assert c("Purchase") == ("trade", "buy") and c("Systematic Investment - SIP") == ("trade", "buy") and c("Switch In") == ("trade", "buy") and c("Dividend Reinvestment") == ("trade", "buy")
    assert c("Redemption") == ("trade", "sell") and c("Switch Out") == ("trade", "sell") and c("STP Out") == ("trade", "sell")
    assert c("Purchase - Reversal") == ("reversal", "buy") and c("Redemption Rejected") == ("reversal", "sell")
    assert c("Stamp Duty")[0] == "ignore" and c("TDS on dividend payout")[0] == "ignore" and c("Something odd")[0] == "unknown"


def test_mf_rows_reversals_cancel_the_original_and_units_come_from_amount_over_nav():
    hdr = "Folio No,ISIN,Scheme Name,Date,Transaction Type,Amount,Units,NAV"
    raw = csv_bytes(hdr, "111,INF000A01010,Demo Fund,05-01-2025,Purchase,10000,500,20", "111,INF000A01010,Demo Fund,06-01-2025,Purchase,5000,250,20",
                    "111,INF000A01010,Demo Fund,07-01-2025,Purchase - Reversal,5000,-250,20", "111,INF000A01010,Demo Fund,08-01-2025,Stamp Duty,0.5,,", "111,INF000A01010,Demo Fund,09-01-2025,Redemption,4400,,22",
                    "111,INF000A01010,Demo Fund,10-01-2025,Redemption Reversal,9,-9,22")
    out = P.parse_mf(T.read_table(raw, "mf.csv", P.MF_VOCAB))
    assert [(r["side"], r["quantity"], r["trade_date"]) for r in out.rows] == [("buy", 500.0, date(2025, 1, 5)), ("sell", 200.0, date(2025, 1, 9))]                      # reversal removed the 250-unit buy
    assert out.n_rejected == 1 and any("no matching original" in i["reason"] for i in out.issues)                                                                       # the 9-unit reversal had no original


def test_mf_closing_unit_balance_is_carried_when_the_statement_has_it():
    raw = csv_bytes("Folio,ISIN,Date,Transaction Type,Units,NAV,Closing Unit Balance", "9,INF000A01010,05-01-2025,Purchase,100,10,100", "9,INF000A01010,06-01-2025,Redemption,40,11,60")
    out = P.parse_mf(T.read_table(raw, "mf.csv", P.MF_VOCAB))
    assert out.closing == {("9", "INF000A01010"): 60.0}


def test_dates_are_day_first_and_ambiguous_garbage_is_none():
    assert P.parse_date("03/04/2025") == date(2025, 4, 3) and P.parse_date("2025-04-03") == date(2025, 4, 3) and P.parse_date("3-Apr-2025") == date(2025, 4, 3)
    assert P.parse_date("") is None and P.parse_date("sometime in April") is None


def test_layout_hash_depends_on_header_names_only_not_on_banner_or_rows():
    a = T.read_table(csv_bytes(*BANNER, TRADE_HEADER, "10-04-2025,AAA,,Buy,10,100,T1,EQ"), "a.csv", P.TRADE_VOCAB)
    b = T.read_table(csv_bytes("Other banner,,,", TRADE_HEADER, "11-05-2025,BBB,,Sell,1,2,T9,EQ"), "b.csv", P.TRADE_VOCAB)
    c = T.read_table(csv_bytes("Trade Date,Symbol,Qty,Price,Side", "10-04-2025,AAA,10,100,Buy"), "c.csv", P.TRADE_VOCAB)
    assert a.layout == b.layout and a.layout != c.layout and P.missing_fields("tradebook", a) == []
    assert P.missing_fields("tradebook", T.read_table(csv_bytes("Trade Date,Symbol,Qty", "1,2,3"), "d.csv", P.TRADE_VOCAB)) == ["price"]
