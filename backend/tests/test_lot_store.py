"""Broker snapshot import against the real database with a throwaway user: layout approval, per-security triage that sticks, pots regenerated from the snapshot, stops carried, trades deduplicated,
tax lots rebuilt and reconciled. Synthetic files; real NSE symbols only so that symbol resolution is the real one."""
import uuid
from datetime import date

import pytest

from qsde.db.connection import get_sync_conn, read_sql
from qsde.portfolio import lot_store as S
from qsde.trader import legacy as LG

BANNER = "Demo Back Office,,,\nClient Name: TEST PERSON,,,\nPAN: AAAAA0000A,,,\n,,,"
ACC = "demochoice"
TODAY = date(2026, 10, 25)


@pytest.fixture()
def uid():
    u = "t-" + uuid.uuid4().hex[:12]
    yield u
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute("DELETE FROM portfolio_holdings WHERE import_id IN (SELECT id FROM portfolio_imports WHERE user_id = %s)", (u,))
        for t in ("legacy_triage", "portfolio_imports", "security_pot", "lot_runs", "lot_loads", "lot_layouts_approved", "mode_audit"):
            cur.execute(f"DELETE FROM {t} WHERE user_id = %s", (u,))


@pytest.fixture(scope="module")
def isins():
    d = read_sql("SELECT symbol, isin FROM nse_equity_master WHERE symbol IN ('RELIANCE', 'TCS', 'HDFCBANK')")
    return dict(zip(d["symbol"], d["isin"]))


def holdings_file(rows):
    return (BANNER + "\nScrip Name,ISIN,Holding Quantity,Average Price\n" + "\n".join(f"{s},{i},{q},{p}" for s, i, q, p in rows) + "\nTotal,,0,\n").encode()


def load_holdings(uid, rows, **kw):
    prep = S.prepare("holdings", holdings_file(rows), "holdings.csv")
    return S.apply_import(prep, user_id=uid, account=ACC, role="cli", filename="holdings.csv", today=TODAY, **kw)


def pot_imports(uid):
    d = read_sql("SELECT i.id, i.pot, i.broker_account, (SELECT count(*) FROM portfolio_holdings h WHERE h.import_id = i.id) AS n FROM portfolio_imports i WHERE i.user_id = :u ORDER BY i.id", params={"u": uid})
    return [(r.pot if isinstance(r.pot, str) else None, int(r.n)) for r in d.itertuples()]


@pytest.mark.real_data
def test_the_first_file_of_a_layout_needs_a_reviewed_mapping_and_writes_nothing(uid, isins):
    prep = S.prepare("holdings", holdings_file([("RELIANCE", isins["RELIANCE"], 10, 2500)]), "h.csv")
    out = S.apply_import(prep, user_id=uid, account=ACC, role="cli", filename="h.csv", mapping_ok=False, today=TODAY)
    assert out["status"].startswith("REFUSED") and out["column_mapping"]["skipped_banner_rows"] == 4
    assert read_sql("SELECT count(*) AS n FROM lot_loads WHERE user_id = :u", params={"u": uid})["n"].iloc[0] == 0
    ok = S.apply_import(prep, user_id=uid, account=ACC, role="cli", filename="h.csv", mapping_ok=True, today=TODAY)
    assert ok["status"] == "loaded"
    again = S.apply_import(prep, user_id=uid, account=ACC, role="cli", filename="h.csv", mapping_ok=True, today=TODAY)
    assert again["status"] == "already loaded"
    # a different kind of file has its own layout: the holdings approval does not carry over
    trades = ("Trade Date,Symbol,Buy/Sell,Qty,Rate\n10-04-2025,RELIANCE,Buy,5,2400\n").encode()
    tprep = S.prepare("tradebook", trades, "t.csv")
    assert S.apply_import(tprep, user_id=uid, account=ACC, role="cli", filename="t.csv", mapping_ok=False, today=TODAY)["status"].startswith("REFUSED")


@pytest.mark.real_data
def test_a_demat_file_with_mixed_positions_is_split_by_the_owners_triage_and_untriaged_holdings_lock_ideas(uid, isins):
    rows = [("RELIANCE", isins["RELIANCE"], 10, 2500), ("TCS", isins["TCS"], 20, 3500), ("HDFCBANK", isins["HDFCBANK"], 30, 1600)]
    out = load_holdings(uid, rows, mapping_ok=True, triage={"RELIANCE": "trading", "TCS": "investment"})
    assert out["status"] == "loaded" and set(out["pots"]) == {"trading", "investment", "unassigned"}
    assert sorted(pot_imports(uid), key=str) == sorted([("trading", 1), ("investment", 1), (None, 1)], key=str)
    assert LG.untriaged(read_sql, uid) == ["HDFCBANK"]                                                    # held, but in neither pot: new ideas are locked
    assert [p["symbol"] for p in LG.positions(read_sql, uid, lambda rs, s, d: __import__("qsde.trader.store", fromlist=["x"]).load_prices(rs, s, d), date(2026, 9, 1))] == ["RELIANCE"]
    # the owner triages the last one; the unassigned import is superseded by an empty one
    with get_sync_conn() as c:
        cur = c.cursor()
        S.set_triage(cur, uid, "cli", ACC, {"HDFCBANK": "investment"})
        made = S.materialize_pots(cur, uid, "cli", ACC)
    assert set(made) == {"investment", "unassigned"} and LG.untriaged(read_sql, uid) == []


@pytest.mark.real_data
def test_a_security_pot_is_set_once_and_a_reimport_does_not_reset_it_or_the_recorded_stop(uid, isins):
    rows = [("RELIANCE", isins["RELIANCE"], 10, 2500), ("TCS", isins["TCS"], 20, 3500)]
    load_holdings(uid, rows, mapping_ok=True, triage={"RELIANCE": "trading", "TCS": "investment"})
    imp = LG.latest_trading_imports(read_sql, uid)[0]
    with get_sync_conn() as c:
        c.cursor().execute("INSERT INTO legacy_triage (import_id, symbol, user_id, stop_price, set_by_role) VALUES (%s,'RELIANCE',%s,2300,'trader')", (imp, uid))
    # next week's export: quantities changed, no triage arguments at all
    out = S.apply_import(S.prepare("holdings", holdings_file([("RELIANCE", isins["RELIANCE"], 12, 2510), ("TCS", isins["TCS"], 20, 3500)]), "wk2.csv"), user_id=uid, account=ACC, role="cli",
                         filename="wk2.csv", mapping_ok=False, today=TODAY)
    assert out["status"] == "loaded"                                                                      # same layout as before: already approved
    new = LG.latest_trading_imports(read_sql, uid)[0]
    assert new != imp and LG.untriaged(read_sql, uid) == []
    pos = LG.positions(read_sql, uid, lambda rs, s, d: __import__("qsde.trader.store", fromlist=["x"]).load_prices(rs, s, d), date(2026, 9, 1))
    assert pos[0]["qty"] == 12 and pos[0]["stop"] == 2300 and pos[0]["protected"]                       # the stop followed the symbol
    with get_sync_conn() as c, pytest.raises(Exception, match="immutable"):
        c.cursor().execute("UPDATE security_pot SET pot = 'investment' WHERE user_id = %s AND security = 'RELIANCE'", (uid,))
    with get_sync_conn() as c:
        cur = c.cursor()
        with pytest.raises(ValueError, match="already assigned to a different pot"):
            S.set_triage(cur, uid, "cli", ACC, {"RELIANCE": "investment"})
        with pytest.raises(ValueError, match="still in your latest holdings"):
            S.release_triage(cur, uid, "cli", ACC, "RELIANCE")


@pytest.mark.real_data
def test_a_position_that_is_sold_empties_its_pot_so_the_old_import_stops_counting(uid, isins):
    load_holdings(uid, [("RELIANCE", isins["RELIANCE"], 10, 2500), ("TCS", isins["TCS"], 20, 3500)], mapping_ok=True, triage={"RELIANCE": "trading", "TCS": "investment"})
    assert len(LG.latest_trading_imports(read_sql, uid)) == 1
    S.apply_import(S.prepare("holdings", holdings_file([("TCS", isins["TCS"], 20, 3500)]), "wk2.csv"), user_id=uid, account=ACC, role="cli", filename="wk2.csv", mapping_ok=False, today=TODAY)
    imp = LG.latest_trading_imports(read_sql, uid)
    assert len(imp) == 1 and read_sql("SELECT count(*) AS n FROM portfolio_holdings WHERE import_id = :i", params={"i": imp[0]})["n"].iloc[0] == 0
    with get_sync_conn() as c:
        S.release_triage(c.cursor(), uid, "cli", ACC, "RELIANCE")                                          # gone from the account: a later purchase is a new decision
        S.set_triage(c.cursor(), uid, "cli", ACC, {"RELIANCE": "investment"})


@pytest.mark.real_data
def test_triage_naming_a_symbol_that_is_not_in_the_file_is_refused_before_anything_is_written(uid, isins):
    prep = S.prepare("holdings", holdings_file([("RELIANCE", isins["RELIANCE"], 10, 2500)]), "h.csv")
    with pytest.raises(ValueError, match="not in this file"):
        S.apply_import(prep, user_id=uid, account=ACC, role="cli", filename="h.csv", mapping_ok=True, triage={"TCSS": "trading"}, today=TODAY)
    assert read_sql("SELECT count(*) AS n FROM lot_loads WHERE user_id = :u", params={"u": uid})["n"].iloc[0] == 0


TRADES = ("Trade Date,Symbol,ISIN,Buy/Sell,Qty,Rate,Trade No\n"
          "10-04-2025,RELIANCE,{i},Buy,6,2400,A1\n20-05-2025,RELIANCE,{i},Buy,4,2450,A2\n25-06-2025,RELIANCE,{i},Sell,3,2600,A3\n")


@pytest.mark.real_data
def test_tradebook_lots_are_fifo_reconciled_to_the_holdings_and_overlapping_files_do_not_double_count(uid, isins):
    load_holdings(uid, [("RELIANCE", isins["RELIANCE"], 7, 2420)], mapping_ok=True, triage={"RELIANCE": "trading"})
    a = S.apply_import(S.prepare("tradebook", TRADES.format(i=isins["RELIANCE"]).encode(), "fy25.csv"), user_id=uid, account=ACC, role="cli", filename="fy25.csv", mapping_ok=True, today=TODAY)
    assert a["status"] == "loaded" and a["duplicates"] == 0 and a["lots"]["by_status"] == {"RECONCILED": 1}
    overlap = TRADES.format(i=isins["RELIANCE"]) + "01-08-2025,RELIANCE," + isins["RELIANCE"] + ",Buy,2,2500,A4\n"
    b = S.apply_import(S.prepare("tradebook", overlap.encode(), "fy25b.csv"), user_id=uid, account=ACC, role="cli", filename="fy25b.csv", mapping_ok=False, today=TODAY)
    assert b["duplicates"] == 3 and b["accepted"] == 4                                                     # the three repeated trades were recognised, one new trade added
    lots = read_sql("SELECT l.buy_date, l.quantity::float8 AS q, l.status FROM portfolio_tax_lots l WHERE l.run_id = (SELECT max(id) FROM lot_runs WHERE user_id = :u) ORDER BY l.buy_date", params={"u": uid})
    assert [(str(r.buy_date), r.q) for r in lots.itertuples()] == [("2025-04-10", 3.0), ("2025-05-20", 4.0), ("2025-08-01", 2.0)]
    assert set(lots["status"]) == {"UNRECONCILED_SURPLUS"}                                                 # holdings snapshot says 7, files imply 9: flagged, not silently trimmed


@pytest.mark.real_data
def test_holdings_with_no_trades_get_date_pending_lots_and_a_gap_is_reported_exactly(uid, isins):
    load_holdings(uid, [("RELIANCE", isins["RELIANCE"], 10, 2420), ("TCS", isins["TCS"], 5, 3500)], mapping_ok=True, triage={"RELIANCE": "trading", "TCS": "investment"})
    d = read_sql("SELECT symbol, status, buy_date, quantity::float8 AS q FROM portfolio_tax_lots WHERE run_id = (SELECT max(id) FROM lot_runs WHERE user_id = :u) ORDER BY symbol", params={"u": uid})
    assert list(d["status"]) == ["DATE_PENDING", "DATE_PENDING"] and d["buy_date"].isna().all()
    S.apply_import(S.prepare("tradebook", TRADES.format(i=isins["RELIANCE"]).encode(), "t.csv"), user_id=uid, account=ACC, role="cli", filename="t.csv", mapping_ok=True, today=TODAY)
    d = read_sql("SELECT status, buy_date, quantity::float8 AS q, origin FROM portfolio_tax_lots WHERE symbol = 'RELIANCE' AND run_id = (SELECT max(id) FROM lot_runs WHERE user_id = :u) ORDER BY buy_date NULLS LAST", params={"u": uid})
    assert list(zip(d["origin"], d["q"])) == [("buy", 3.0), ("buy", 4.0), ("gap", 3.0)] and d["buy_date"].isna().tolist() == [False, False, True]
    assert d["status"].tolist() == ["UNRECONCILED_LOT_GAP", "UNRECONCILED_LOT_GAP", "DATE_PENDING"]       # 10 held, 7 explained: the 3 missing shares are named, not dated


def test_mf_lots_are_tagged_direct_or_regular_from_the_scheme_master_and_stay_unverified_without_a_closing_balance(uid):
    m = read_sql("SELECT isin_growth_or_payout AS isin, is_direct, is_growth FROM mf_scheme_master WHERE isin_growth_or_payout IS NOT NULL AND is_direct IS NOT NULL AND is_growth IS NOT NULL LIMIT 1")
    if m.empty:
        pytest.skip("no scheme master loaded")
    isin = m["isin"].iloc[0]
    raw = f"Folio No,ISIN,Scheme Name,Date,Transaction Type,Amount,Units,NAV\n77,{isin},Demo Fund,05-01-2025,Purchase,10000,500,20\n77,{isin},Demo Fund,05-03-2025,Redemption,2200,100,22\n".encode()
    out = S.apply_import(S.prepare("mf", raw, "mf.csv"), user_id=uid, account="democams", role="cli", filename="mf.csv", mapping_ok=True, triage={isin: "investment"}, today=TODAY)
    assert out["status"] == "loaded"
    d = read_sql("SELECT plan_type, option_type, status, quantity::float8 AS q, folio FROM portfolio_tax_lots WHERE user_id = :u", params={"u": uid})
    assert d["plan_type"].iloc[0] == ("DIRECT" if m["is_direct"].iloc[0] else "REGULAR") and d["option_type"].iloc[0] == ("GROWTH" if m["is_growth"].iloc[0] else "IDCW")
    assert d["status"].iloc[0] == "UNVERIFIED" and d["q"].iloc[0] == 400 and d["folio"].iloc[0] == "77"
    assert read_sql("SELECT pot FROM security_pot WHERE user_id = :u", params={"u": uid})["pot"].tolist() == ["investment"]


@pytest.mark.real_data
def test_nothing_personal_from_the_banner_reaches_the_database(uid, isins):
    load_holdings(uid, [("RELIANCE", isins["RELIANCE"], 10, 2500)], mapping_ok=True, triage={"RELIANCE": "trading"})
    d = read_sql("SELECT issues::text AS i, mapping::text AS m, filename FROM lot_loads WHERE user_id = :u", params={"u": uid}).iloc[0]
    audit = read_sql("SELECT payload::text AS p FROM mode_audit WHERE user_id = :u", params={"u": uid})["p"].str.cat()
    for needle in ("TEST PERSON", "AAAAA0000A", "Client Name"):
        assert needle not in d["i"] and needle not in d["m"] and needle not in audit


def test_the_confirmed_corporate_actions_loader_marks_unconfirmed_ones_as_not_applied():
    row = read_sql("SELECT a.symbol, a.ex_date FROM nse_corporate_actions a JOIN corp_action_reconciliation r ON r.action_id = a.id AND r.classification = 'confirmed' "
                   "WHERE a.category = 'SPLIT_BONUS' AND a.ex_date > '2022-01-01' ORDER BY a.ex_date DESC LIMIT 1")
    if row.empty:
        pytest.skip("no confirmed split / bonus in the database")
    sym, ex = row["symbol"].iloc[0], row["ex_date"].iloc[0]
    with get_sync_conn() as c:
        ev = S.load_events(c.cursor(), [sym], date(2020, 1, 1), date(2030, 1, 1))[sym]
    mine = [e for e in ev if e.ex_date == ex]
    assert mine and all(isinstance(e.ok, bool) for e in mine)
    assert any(e.ok for e in mine) or any("terms" in e.reason or "implies" in e.reason or "recognisable" in e.reason or "consolidation" in e.reason for e in mine)
