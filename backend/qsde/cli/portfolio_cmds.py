"""`stoxsy import | triage | restrict | lots`: the owner's tools for bringing broker files in, assigning each security to a pot, keeping a restricted list and reading the tax lots.

Everything writes through the same functions the API uses, so it is audited identically. Nothing is assumed: the user, the broker account and every pot are typed by the owner.
(`--user` is the account id the web app uses; with sign-in switched off it is `local`.) Nothing here places an order.
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from qsde.config import paths

ROLE = "cli"
EXTS = (".csv", ".txt", ".tsv", ".xlsx", ".xlsm", ".xls")


def _p(*a) -> None:
    print(*a, flush=True)


def _split(s: Optional[str]) -> list:
    return [x.strip().upper() for x in (s or "").split(",") if x.strip()]


def _triage_from_args(args) -> dict:
    out: dict = {}
    for pot, names in (("trading", _split(args.trading)), ("investment", _split(args.investment))):
        for n in names:
            if n in out and out[n] != pot:
                raise ValueError(f"{n} is listed for both pots")
            out[n] = pot
    if getattr(args, "triage_file", None):
        with open(args.triage_file, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                k = {kk.strip().lower(): (vv or "").strip() for kk, vv in r.items() if kk}
                sym, pot = k.get("symbol", "").upper(), k.get("pot", "").lower()
                if not sym or pot not in ("investment", "trading"):
                    raise ValueError(f"triage file row not understood (need columns symbol,pot with pot investment or trading): {r}")
                if out.get(sym, pot) != pot:
                    raise ValueError(f"{sym} is listed for both pots")
                out[sym] = pot
    return out


def _files(args, kind: str) -> list:
    from qsde.portfolio import lot_store as S

    if args.file:
        return [Path(args.file)]
    d = paths.data_path("inbox", S.INBOX[kind])
    d.mkdir(parents=True, exist_ok=True)
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith((".", "~")))


def cmd_import(args) -> int:
    from qsde.db.connection import get_sync_conn
    from qsde.portfolio import lot_store as S

    kind = args.kind
    files = _files(args, kind)
    if not files:
        _p(f"no files to import: pass --file, or put {kind} files in {paths.data_path('inbox', S.INBOX[kind])}")
        return 1
    triage = _triage_from_args(args)
    rc = 0
    for f in files:
        try:
            prep = S.prepare(kind, f.read_bytes(), f.name)
        except ValueError as e:
            _p(f"{f.name}: cannot read: {e}")
            rc = max(rc, 1)
            continue
        tri = dict(triage)
        if args.pot_all:
            key = "symbol" if kind == "holdings" else "isin"
            for r in prep.rows if kind in ("holdings", "mf") else []:
                tri.setdefault(r[key], args.pot_all)
        if args.dry_run:
            known = {}
            if kind in ("holdings", "mf"):
                with get_sync_conn() as c:
                    known = S.active_pots(c.cursor(), args.user, args.account.strip().lower(), "mf" if kind == "mf" else "equity")
                known = {**known, **tri}
            _p(f"--- dry run: {f.name} (nothing written)")
            _p(json.dumps(S.preview(prep, known), indent=2, default=str))
            continue
        out = S.apply_import(prep, user_id=args.user, account=args.account, role=ROLE, filename=f.name, mapping_ok=args.mapping_ok, triage=tri)
        _p(f"--- {f.name}: {out['status'].split(':')[0]}")
        if out["status"].startswith("REFUSED"):
            _p(out["status"])
            _p(json.dumps({k: out[k] for k in ("column_mapping", "sample", "issues", "rows_in_file", "accepted", "rejected") if k in out}, indent=2, default=str))
            rc = max(rc, 1)
        else:
            _p(json.dumps({k: v for k, v in out.items() if k != "status"}, indent=2, default=str))
            if out.get("rejected"):
                rc = max(rc, 1)
    return rc


def cmd_triage(args) -> int:
    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.portfolio import lot_store as S
    from qsde.trader import legacy

    account = args.account.strip().lower()
    if args.action == "list":
        got = None
        with get_sync_conn() as c:
            cur = c.cursor()
            pots = S.active_pots(cur, args.user, account)
            got = S.latest_holdings(cur, args.user, account)
        held = sorted(got[1]) if got else []
        _p(f"account {account}: {len(held)} holdings in the latest snapshot")
        for s in held:
            _p(f"  {s:<14}{pots.get(s, 'NOT ASSIGNED')}")
        left = legacy.untriaged(read_sql, args.user)
        _p("new trade ideas are locked until every holding is assigned" if left else "every holding is assigned")
        return 1 if left else 0
    if args.action == "set":
        tri = _triage_from_args(args)
        if not tri:
            _p("nothing to assign: pass --trading, --investment or --triage-file")
            return 1
        with get_sync_conn() as c:
            cur = c.cursor()
            got = S.latest_holdings(cur, args.user, account)
            if got is None:
                raise ValueError(f"no holdings snapshot for {account}: import a holdings file first")
            unknown = sorted(set(tri) - set(got[1]))
            if unknown:
                raise ValueError("not in the latest snapshot: " + ", ".join(unknown))
            r = S.set_triage(cur, args.user, ROLE, account, tri)
            made = S.materialize_pots(cur, args.user, ROLE, account)
            S.rebuild_lots(cur, args.user, account)
        _p(json.dumps({**r, "pots": made}, indent=2, default=str))
        return 0
    if args.action == "release":
        if not args.symbol:
            _p("--symbol is required")
            return 1
        with get_sync_conn() as c:
            S.release_triage(c.cursor(), args.user, ROLE, account, args.symbol.strip().upper())
        _p(f"{args.symbol.upper()} released: it can be assigned again when you next hold it")
        return 0
    return 1


def cmd_restrict(args) -> int:
    from qsde.db.connection import get_sync_conn, read_sql
    from qsde.trader import restricted

    if args.action == "list":
        rows = restricted.list_entries(read_sql, args.user)
        if not rows:
            _p("restricted list is empty (nothing is preset)")
        for r in rows:
            _p(f"{r['symbol']:<14}{'ACTIVE' if r['active'] else 'released' if r['released_at'] else 'expired':<10}until {r['restricted_until'] or 'released by you':<12} {r['reason']}")
        return 0
    if args.action == "add":
        if not args.symbol or not args.reason:
            _p("--symbol and --reason are required")
            return 1
        until = datetime.strptime(args.until, "%Y-%m-%d").date() if args.until else None
        with get_sync_conn() as c:
            r = restricted.add(c, args.user, ROLE, args.symbol, args.reason, until)
        _p(f"{r['symbol']} restricted (entry {r['id']}); the trader desk will not propose or log it while this stands")
        return 0
    if not args.symbol:
        _p("--symbol is required")
        return 1
    try:
        with get_sync_conn() as c:
            r = restricted.release(c, args.user, ROLE, args.symbol)
    except LookupError as e:
        _p(str(e))
        return 1
    _p(f"{r['symbol']} released ({r['released']} entr{'y' if r['released'] == 1 else 'ies'}); history is kept")
    return 0


def cmd_lots(args) -> int:
    from qsde.db.connection import read_sql
    from qsde.portfolio import lot_report

    rep = lot_report.build(read_sql, args.user, args.account.strip().lower() if args.account else None, date.today())
    _p(lot_report.render_text(rep))
    return 0


def register(sub) -> None:
    def common(p, account_required=True):
        p.add_argument("--user", required=True, help="your account id in Stoxsy ('local' when sign-in is off); never assumed")
        p.add_argument("--account", required=account_required, help="the broker account label you choose, e.g. choice or zerodha; never assumed")

    i = sub.add_parser("import", help="import a broker file: holdings | tradebook | lots | mf (dry run first)")
    i.add_argument("kind", choices=["holdings", "tradebook", "lots", "mf"])
    common(i)
    i.add_argument("--file")
    i.add_argument("--inbox", action="store_true", help="every file in <data>/inbox/<kind>")
    i.add_argument("--dry-run", action="store_true", help="read and show the column mapping and a sample; write nothing")
    i.add_argument("--mapping-ok", action="store_true", help="I checked this header layout against the real file in a dry run (needed once per layout)")
    i.add_argument("--trading", help="comma-separated symbols that belong to the trading pot")
    i.add_argument("--investment", help="comma-separated symbols that belong to the investment pot")
    i.add_argument("--triage-file", help="CSV with columns symbol,pot")
    i.add_argument("--pot-all", choices=["investment", "trading"], help="assign every security in this file that has no pot yet to this pot (your explicit choice)")
    i.set_defaults(fn=cmd_import)

    t = sub.add_parser("triage", help="which pot each holding belongs to: list | set | release")
    t.add_argument("action", choices=["list", "set", "release"])
    common(t)
    t.add_argument("--trading")
    t.add_argument("--investment")
    t.add_argument("--triage-file")
    t.add_argument("--symbol")
    t.set_defaults(fn=cmd_triage)

    r = sub.add_parser("restrict", help="your own restricted list: add | list | release")
    r.add_argument("action", choices=["add", "list", "release"])
    r.add_argument("--user", required=True, help="your account id in Stoxsy ('local' when sign-in is off); never assumed")
    r.add_argument("--symbol")
    r.add_argument("--reason")
    r.add_argument("--until", help="YYYY-MM-DD; omit for until you release it")
    r.set_defaults(fn=cmd_restrict)

    l = sub.add_parser("lots", help="tax lots with holding-period class and reconciliation status")
    common(l, account_required=False)
    l.set_defaults(fn=cmd_lots)
