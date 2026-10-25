"""Lots and triage through the API and the CLI, and the trader desk lock for holdings that are not assigned to a pot. Throwaway user; synthetic files; real NSE symbols."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from api.auth_deps import Principal, require_auth
from api.main import app
from qsde.cli import main as cli
from qsde.config import paths
from qsde.db.connection import get_sync_conn, read_sql
from qsde.portfolio import lot_store as S

BANNER = "Demo Back Office,,,\nClient Name: TEST PERSON,,,\n,,,"
ACC = "demochoice"
SYM = "RELIANCE"


@pytest.fixture()
def who():
    uid = "t-" + uuid.uuid4().hex[:12]
    role = {"v": "trader"}
    app.dependency_overrides[require_auth] = lambda: Principal(id=uid, email=f"{uid}@example.com", role=role["v"], authenticated=True)
    yield TestClient(app), uid, role
    app.dependency_overrides.pop(require_auth, None)
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SET LOCAL session_replication_role = replica")
        cur.execute("DELETE FROM portfolio_holdings WHERE import_id IN (SELECT id FROM portfolio_imports WHERE user_id = %s)", (uid,))
        for t in ("legacy_triage", "portfolio_imports", "security_pot", "lot_runs", "lot_loads", "lot_layouts_approved", "mode_audit", "trader_ideas", "user_profiles", "compliance_restricted_list"):
            cur.execute(f"DELETE FROM {t} WHERE user_id = %s", (uid,))


def isin(sym):
    return read_sql("SELECT isin FROM nse_equity_master WHERE symbol = :s", params={"s": sym})["isin"].iloc[0]


def holdings_csv(*syms):
    return (BANNER + "\nScrip Name,ISIN,Holding Quantity,Average Price\n" + "\n".join(f"{s},{isin(s)},10,{2500 + i}" for i, s in enumerate(syms)) + "\nTotal,,0,\n").encode()


def load(uid, *syms, triage=None):
    return S.apply_import(S.prepare("holdings", holdings_csv(*syms), "h.csv"), user_id=uid, account=ACC, role="cli", filename="h.csv", mapping_ok=True, triage=triage)


def test_untriaged_holdings_lock_new_ideas_until_each_has_a_pot(who):
    c, uid, _ = who
    assert c.put("/api/me/profile", json={"investment_capital": 40_000_000, "trading_capital": 1_000_000, "emergency_fund": 0}).status_code == 200
    load(uid, SYM, "TCS", triage={"TCS": "investment"})
    d = c.get("/api/trader/desk").json()
    assert d["untriaged"] == [SYM] and "not assigned to a pot yet (RELIANCE)" in d["headline"] and d["meters"]["untriaged"] == [SYM]
    r = c.post("/api/trader/ideas", json={"symbol": "HDFCBANK"})
    assert r.status_code == 422 and "not assigned to a pot yet (RELIANCE)" in r.json()["detail"][0]
    t = c.get("/api/portfolio/triage").json()
    assert t["unassigned"] == [SYM] and [h["pot"] for h in t["accounts"][0]["holdings"]] == [None, "investment"]
    ok = c.put("/api/portfolio/triage", json={"account": ACC, "assignments": {SYM: "trading"}})
    assert ok.status_code == 200 and ok.json()["assigned"] == {SYM: "trading"}
    d = c.get("/api/trader/desk").json()
    assert d["untriaged"] == [] and any(b["symbol"] == SYM and b["source"] == "imported" for b in d["book"])      # now part of the existing book (and unprotected until a stop is recorded)
    assert d["unprotected"] == [SYM]


def test_triage_writes_need_a_trader_and_are_validated(who):
    c, uid, role = who
    load(uid, SYM, "TCS", triage={"TCS": "investment"})
    role["v"] = "viewer"
    assert c.get("/api/portfolio/triage").status_code == 200
    assert c.put("/api/portfolio/triage", json={"account": ACC, "assignments": {SYM: "trading"}}).status_code == 403
    role["v"] = "trader"
    assert c.put("/api/portfolio/triage", json={"account": ACC, "assignments": {"NOSUCH": "trading"}}).status_code == 422
    assert c.put("/api/portfolio/triage", json={"account": ACC, "assignments": {"TCS": "trading"}}).status_code == 409          # already investment: immutable
    assert c.put("/api/portfolio/triage", json={"account": "other", "assignments": {SYM: "trading"}}).status_code == 404
    assert c.put("/api/portfolio/triage", json={"account": ACC, "assignments": {SYM: "gold"}}).status_code == 422


def test_lots_endpoint_reports_pot_class_status_and_what_is_not_computed(who):
    c, uid, _ = who
    assert c.get("/api/portfolio/lots").json()["lots"] == []
    load(uid, SYM, triage={SYM: "trading"})
    j = c.get("/api/portfolio/lots").json()
    assert len(j["lots"]) == 1 and j["lots"][0]["pot"] == "trading" and j["lots"][0]["status"] == "DATE_PENDING" and j["lots"][0]["tax_class"] == "UNKNOWN_DATE" and j["lots"][0]["buy_date"] is None
    assert "UNVERIFIED" in j["policy"]["status"] and any("exemption" in x for x in j["policy"]["not_computed"]) and any("places no orders" in n for n in j["notes"])


def test_cli_import_dry_run_writes_nothing_and_the_inbox_is_read_from_the_data_directory(who, tmp_path, monkeypatch, capsys):
    c, uid, _ = who
    monkeypatch.setattr(paths, "DATA", tmp_path)
    inbox = tmp_path / "inbox" / "holdings"
    inbox.mkdir(parents=True)
    (inbox / "choice.csv").write_bytes(holdings_csv(SYM))
    assert cli.main(["import", "holdings", "--user", uid, "--account", ACC, "--inbox", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "needs_triage" in out and "TEST PERSON" not in out
    assert read_sql("SELECT count(*) AS n FROM lot_loads WHERE user_id = :u", params={"u": uid})["n"].iloc[0] == 0
    assert cli.main(["import", "holdings", "--user", uid, "--account", ACC, "--inbox", "--trading", SYM]) == 1           # layout not approved yet
    assert "REFUSED" in capsys.readouterr().out
    assert cli.main(["import", "holdings", "--user", uid, "--account", ACC, "--inbox", "--mapping-ok", "--trading", SYM]) == 0
    assert read_sql("SELECT pot FROM security_pot WHERE user_id = :u", params={"u": uid})["pot"].tolist() == ["trading"]
    assert cli.main(["triage", "list", "--user", uid, "--account", ACC]) == 0
    assert cli.main(["lots", "--user", uid]) == 0


def test_cli_pot_all_is_an_explicit_choice_and_triage_file_is_validated(who, tmp_path, capsys):
    c, uid, _ = who
    f = tmp_path / "h.csv"
    f.write_bytes(holdings_csv(SYM, "TCS"))
    assert cli.main(["import", "holdings", "--user", uid, "--account", ACC, "--file", str(f), "--mapping-ok", "--pot-all", "investment", "--trading", SYM]) == 0
    pots = dict(read_sql("SELECT security, pot FROM security_pot WHERE user_id = :u", params={"u": uid}).itertuples(index=False))
    assert pots == {SYM: "trading", "TCS": "investment"}                                                                # an explicit per-symbol choice beats --pot-all
    bad = tmp_path / "t.csv"
    bad.write_text("symbol,pot\nTCS,gold\n")
    assert cli.main(["triage", "set", "--user", uid, "--account", ACC, "--triage-file", str(bad)]) == 2
    assert "symbol,pot" in capsys.readouterr().out


def test_cli_restricted_list_is_audited_and_blocks_the_desk(who, capsys):
    c, uid, _ = who
    assert c.put("/api/me/profile", json={"investment_capital": 40_000_000, "trading_capital": 1_000_000, "emergency_fund": 0}).status_code == 200
    assert cli.main(["restrict", "list", "--user", uid]) == 0 and "empty" in capsys.readouterr().out
    assert cli.main(["restrict", "add", "--user", uid, "--symbol", "hdfcbank", "--reason", "advised client this week", "--until", "2099-01-01"]) == 0
    assert cli.main(["restrict", "add", "--user", uid, "--symbol", "TCS"]) == 1                                         # a reason is required
    r = c.post("/api/trader/ideas", json={"symbol": "HDFCBANK"})
    assert r.status_code == 422 and "restricted list" in r.json()["detail"][0]
    assert cli.main(["restrict", "release", "--user", uid, "--symbol", "HDFCBANK"]) == 0
    assert cli.main(["restrict", "release", "--user", uid, "--symbol", "HDFCBANK"]) == 1
    acts = read_sql("SELECT action FROM mode_audit WHERE user_id = :u AND action LIKE 'trader.restricted.%' ORDER BY id", params={"u": uid})["action"].tolist()
    assert acts == ["trader.restricted.add", "trader.restricted.release"]
    assert c.get("/api/trader/restricted").json()["entries"][0]["released_at"] is not None


def test_cli_requires_the_user_and_account_explicitly():
    with pytest.raises(SystemExit):
        cli.main(["import", "holdings", "--account", ACC])
    with pytest.raises(SystemExit):
        cli.main(["import", "holdings", "--user", "x"])
    with pytest.raises(SystemExit):
        cli.main(["restrict", "list"])
