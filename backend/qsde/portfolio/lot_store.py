"""Database side of the broker / lot importer: prepare a file (read-only, used by dry runs), apply it, triage securities into pots, regenerate the per-pot imports, rebuild the tax lots.

The order of authority: facts are stored as they arrive (one `lot_loads` row per file, deduplicated trades), the owner's per-security pot triage is set once and carried forward, and everything the
rest of the product reads (`portfolio_imports` per pot, `portfolio_tax_lots`) is DERIVED from those and can be regenerated. Nothing here places an order, and nothing personal from a file is stored:
banner rows are skipped while reading, and only counts, the header layout and column titles are kept.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Optional

import psycopg2.extras

from qsde.db.connection import get_sync_conn
from qsde.modes import profile as prof
from qsde.portfolio import holdings as H, lot_parsers as P, lots as L, tabular as T

KINDS = ("holdings", "tradebook", "lots", "mf")
INBOX = {"holdings": "holdings", "tradebook": "tradebooks", "lots": "lots", "mf": "mf"}
POTS = ("investment", "trading")
J = psycopg2.extras.Json


def _q(cur, sql: str, params=()) -> list:
    cur.execute(sql, params)
    return list(cur.fetchall())


# ── resolution ────────────────────────────────────────────────────────────────────────────────────────────────

def build_resolver(cur) -> P.Resolver:
    m = _q(cur, "SELECT symbol, isin FROM nse_equity_master")
    known = {r["symbol"] for r in m} | {r["symbol"] for r in _q(cur, "SELECT DISTINCT symbol FROM ohlcv WHERE source LIKE 'nse_adj_%%'")}
    by_isin = {r["isin"]: r["symbol"] for r in m}
    for r in _q(cur, "SELECT DISTINCT isin, symbol FROM nse_corporate_actions WHERE isin IS NOT NULL"):
        by_isin.setdefault(r["isin"], r["symbol"])                       # an ISIN the security had BEFORE a split / bonus still resolves to it
    renamed = {r["old_symbol"]: r["new_symbol"] for r in _q(cur, "SELECT old_symbol, new_symbol FROM symbol_changes")}
    return P.Resolver(known, by_isin, renamed)


# ── prepare (no writes) ───────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Prepared:
    kind: str
    table: T.Table
    rows: list = field(default_factory=list)             # kind-specific validated rows
    issues: list = field(default_factory=list)
    n_rows: int = 0
    n_rejected: int = 0
    sha256: str = ""
    layout: str = ""
    closing: dict = field(default_factory=dict)
    skipped_other_segment: int = 0
    parsed_holdings: Optional[H.Parsed] = None

    @property
    def mapping(self) -> dict:
        t = self.table
        return {"fields": {f: t.header[i] for f, i in t.fields.items()}, "unmatched_titles": t.unmatched, "header_row": t.header_row + 1, "skipped_banner_rows": t.skipped_banner,
                "skipped_footer_rows": t.skipped_footer, "sheet": t.sheet}


def prepare(kind: str, payload: bytes, filename: str, cur=None) -> Prepared:
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {', '.join(KINDS)}")
    t = T.read_table(payload, filename, P.VOCAB[kind], min_fields=3)
    miss = P.missing_fields(kind, t)
    if miss:
        raise ValueError("could not find column(s): " + ", ".join(miss) + f". Titles seen: {', '.join(c for c in t.header if c)}")
    own = cur is None
    ctx = get_sync_conn() if own else None
    try:
        if own:
            cur = ctx.__enter__().cursor()
        if kind == "mf":
            r = P.parse_mf(t)
            return Prepared(kind, t, r.rows, r.issues, r.n_rows, r.n_rejected, t.sha256, t.layout, closing=r.closing)
        res = build_resolver(cur)
        if kind == "holdings":
            parsed = P.parse_holdings_table(t, res.known, res.by_isin, res.renamed)
            return Prepared(kind, t, parsed.holdings, parsed.issues, parsed.n_rows, parsed.n_rejected, t.sha256, t.layout, parsed_holdings=parsed)
        r = P.parse_trades(t, res) if kind == "tradebook" else P.parse_reported_lots(t, res)
        return Prepared(kind, t, r.rows, r.issues, r.n_rows, r.n_rejected, t.sha256, t.layout, skipped_other_segment=r.skipped_other_segment)
    finally:
        if own:
            ctx.__exit__(None, None, None)


def preview(prep: Prepared, known_pots: Optional[dict] = None) -> dict:
    """What a dry run shows: the column mapping to review, counts, a few parsed rows. No banner text, no identifiers beyond securities."""
    def s(v):
        return v.isoformat() if isinstance(v, date) else v
    sample = [{k: s(v) for k, v in r.items() if k in ("symbol", "isin", "trade_date", "buy_date", "side", "quantity", "price", "unit_cost", "avg_cost", "folio")} for r in prep.rows[:3]]
    out = {"kind": prep.kind, "rows_in_file": prep.n_rows, "accepted": len(prep.rows), "rejected": prep.n_rejected, "layout": prep.layout[:16], "column_mapping": prep.mapping, "sample": sample,
           "issues": prep.issues[:15], "skipped_other_segment": prep.skipped_other_segment}
    if prep.kind == "holdings":
        pots = known_pots or {}
        out["triage"] = {"investment": sorted(h["symbol"] for h in prep.rows if pots.get(h["symbol"]) == "investment"), "trading": sorted(h["symbol"] for h in prep.rows if pots.get(h["symbol"]) == "trading"),
                         "needs_triage": sorted(h["symbol"] for h in prep.rows if h["symbol"] not in pots)}
    if prep.kind == "tradebook" and prep.rows:
        out["dates"] = {"from": s(min(r["trade_date"] for r in prep.rows)), "to": s(max(r["trade_date"] for r in prep.rows))}
        out["by_side"] = {side: sum(1 for r in prep.rows if r["side"] == side) for side in ("buy", "sell")}
    if prep.kind == "mf":
        out["schemes"] = len({(r["folio"], r["isin"]) for r in prep.rows})
    return out


# ── triage ────────────────────────────────────────────────────────────────────────────────────────────────────

def active_pots(cur, user_id: str, account: str, asset: str = "equity") -> dict:
    return {r["security"]: r["pot"] for r in _q(cur, "SELECT security, pot FROM security_pot WHERE user_id = %s AND broker_account = %s AND asset = %s AND released_at IS NULL", (user_id, account, asset))}


def set_triage(cur, user_id: str, role: str, account: str, assignments: dict, asset: str = "equity") -> dict:
    """assignments: {security: pot}. A security's pot is set once (database trigger); re-stating the same pot is a no-op, a different pot is refused before anything is written."""
    bad = {s: p for s, p in assignments.items() if p not in POTS}
    if bad:
        raise ValueError(f"pot must be 'investment' or 'trading': {sorted(bad)}")
    have = active_pots(cur, user_id, account, asset)
    clash = sorted(s for s, p in assignments.items() if s in have and have[s] != p)
    if clash:
        raise ValueError("already assigned to a different pot and immutable: " + ", ".join(f"{s} ({have[s]})" for s in clash)
                         + ". A position cannot migrate between pots; if it is fully sold you can release the assignment first.")
    new = {s: p for s, p in assignments.items() if s not in have}
    for s, p in sorted(new.items()):
        cur.execute("INSERT INTO security_pot (user_id, broker_account, asset, security, pot, set_by_role) VALUES (%s,%s,%s,%s,%s,%s)", (user_id, account, asset, s, p, role))
    if new:
        prof.record(cur.connection, user_id, role, "portfolio.triage", {"broker_account": account, "asset": asset, "assigned": new})
    return {"assigned": new, "unchanged": sorted(set(assignments) - set(new))}


def release_triage(cur, user_id: str, role: str, account: str, security: str, asset: str = "equity") -> None:
    """Free a security for a fresh decision. Only allowed once the account no longer holds it, so a live position can never change pot."""
    held = latest_holdings(cur, user_id, account)
    if held and security in held[1]:
        raise ValueError(f"{security} is still in your latest holdings snapshot for {account}: its pot cannot change while you hold it")
    cur.execute("UPDATE security_pot SET released_at = NOW() WHERE user_id = %s AND broker_account = %s AND asset = %s AND security = %s AND released_at IS NULL", (user_id, account, asset, security))
    if cur.rowcount == 0:
        raise ValueError(f"{security} has no active pot assignment in {account}")
    prof.record(cur.connection, user_id, role, "portfolio.triage.release", {"broker_account": account, "asset": asset, "security": security})


def latest_holdings(cur, user_id: str, account: str) -> Optional[tuple]:
    """(load_id, {symbol: {quantity, avg_cost, isin}}) of the newest holdings snapshot for the account, or None."""
    l = _q(cur, "SELECT id FROM lot_loads WHERE user_id = %s AND broker_account = %s AND kind = 'holdings' ORDER BY imported_at DESC, id DESC LIMIT 1", (user_id, account))
    if not l:
        return None
    rows = _q(cur, "SELECT symbol, isin, quantity::float8 AS quantity, avg_cost::float8 AS avg_cost FROM broker_holdings WHERE load_id = %s", (l[0]["id"],))
    return l[0]["id"], {r["symbol"]: {"quantity": r["quantity"], "avg_cost": r["avg_cost"], "isin": r["isin"]} for r in rows}


def materialize_pots(cur, user_id: str, role: str, account: str) -> dict:
    """Regenerate the per-pot imports (`portfolio_imports` + `portfolio_holdings`) from the latest snapshot and the triage. A pot whose content did not change is left alone; a pot that emptied gets an
    empty import so the older one stops counting. Stops recorded on the previous trading import are carried over for symbols that are still held."""
    got = latest_holdings(cur, user_id, account)
    if got is None:
        return {}
    load_id, snap = got
    meta = _q(cur, "SELECT filename, sha256 FROM lot_loads WHERE id = %s", (load_id,))[0]
    pots = active_pots(cur, user_id, account)
    groups: dict = {"investment": [], "trading": [], None: []}
    for sym, h in sorted(snap.items()):
        groups[pots.get(sym)].append((sym, h))
    made = {}
    for pot, items in groups.items():
        prev = _q(cur, "SELECT id FROM portfolio_imports WHERE user_id = %s AND broker_account = %s AND pot IS NOT DISTINCT FROM %s ORDER BY imported_at DESC, id DESC LIMIT 1", (user_id, account, pot))
        prev_rows = []
        if prev:
            prev_rows = [(r["symbol"], round(r["quantity"], 4), round(r["avg_cost"], 4)) for r in
                         _q(cur, "SELECT symbol, quantity::float8 AS quantity, avg_cost::float8 AS avg_cost FROM portfolio_holdings WHERE import_id = %s ORDER BY symbol", (prev[0]["id"],))]
        want = [(s, round(h["quantity"], 4), round(h["avg_cost"], 4)) for s, h in items]
        if (not prev and not items) or (prev and prev_rows == want):
            continue
        label = f"{account} snapshot" + ("" if pot else " (not yet assigned to a pot)")
        cur.execute("INSERT INTO portfolio_imports (user_id, pot, label, filename, sha256, n_rows_file, n_accepted, n_rejected, issues, broker_account, source_load_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,0,'[]'::jsonb,%s,%s) RETURNING id", (user_id, pot, label, meta["filename"], meta["sha256"], len(items), len(items), account, load_id))
        iid = cur.fetchone()["id"]
        if items:
            psycopg2.extras.execute_values(cur, "INSERT INTO portfolio_holdings (import_id, symbol, isin, quantity, avg_cost) VALUES %s", [(iid, s, h["isin"], h["quantity"], h["avg_cost"]) for s, h in items])
        if prev and items:
            cur.execute("INSERT INTO legacy_triage (import_id, symbol, user_id, stop_price, horizon_id, set_by_role, set_at) SELECT %s, t.symbol, t.user_id, t.stop_price, t.horizon_id, 'carried', t.set_at "
                        "FROM legacy_triage t WHERE t.import_id = %s AND t.symbol = ANY(%s) ON CONFLICT DO NOTHING", (iid, prev[0]["id"], [s for s, _ in items]))
        made[pot or "unassigned"] = {"import_id": iid, "holdings": len(items)}
    if made:
        prof.record(cur.connection, user_id, role, "portfolio.pots_regenerated", {"broker_account": account, "source_load_id": load_id, "imports": made})
    return made


# ── corporate actions for the lots ────────────────────────────────────────────────────────────────────────────

def load_events(cur, symbols: list, since: date, today: date) -> dict:
    """{resolved symbol: [Event]}: NSE split / bonus / consolidation rows keyed under the symbol or its earlier names, between `since` and `today`. Only actions confirmed against the price archive
    (or applied in a validated adjustment build) are usable; the rest come back as events that are NOT applied and say why, so the lots flag them instead of ignoring them."""
    if not symbols:
        return {}
    ren = _q(cur, "SELECT old_symbol, new_symbol FROM symbol_changes")
    olds: dict = {}
    for r in ren:
        olds.setdefault(r["new_symbol"], set()).add(r["old_symbol"])
    for _ in range(3):                                                      # chains of renames
        for new, o in list(olds.items()):
            for x in list(o):
                o |= olds.get(x, set())
    lineage = {s: {s} | olds.get(s, set()) for s in symbols}
    every = sorted({x for v in lineage.values() for x in v})
    rows = _q(cur, """SELECT a.id, a.symbol, a.ex_date, a.subject, a.factor,
                             (SELECT r.classification FROM corp_action_reconciliation r WHERE r.action_id = a.id ORDER BY r.id DESC LIMIT 1) AS cls,
                             EXISTS (SELECT 1 FROM adjustment_events_applied e JOIN adjustment_runs ru ON ru.version = e.version AND ru.validation_status = 'validated'
                                     WHERE e.source_record_id = a.id) AS applied
                      FROM nse_corporate_actions a
                      WHERE a.category IN ('SPLIT_BONUS', 'CONSOLIDATION') AND a.symbol = ANY(%s) AND a.ex_date > %s AND a.ex_date <= %s ORDER BY a.ex_date, a.id""", (every, since, today))
    out: dict = {s: [] for s in symbols}
    seen: set = set()
    for r in rows:
        for s, names in lineage.items():
            if r["symbol"] not in names or (s, r["ex_date"], r["subject"]) in seen:
                continue
            seen.add((s, r["ex_date"], r["subject"]))
            if r["cls"] == "confirmed" or r["applied"]:
                out[s].append(L.parse_event(r["ex_date"], r["subject"], r["factor"]))
            else:
                out[s].append(L.Event(r["ex_date"], subject=r["subject"], ok=False, reason=f"listed by NSE but not confirmed against the price archive ({r['cls'] or 'not reconciled'})"))
    return out


# ── rebuild ───────────────────────────────────────────────────────────────────────────────────────────────────

def rebuild_lots(cur, user_id: str, account: str, today: Optional[date] = None) -> dict:
    today = today or date.today()
    tx = _q(cur, "SELECT symbol, isin, trade_date, trade_time, side, quantity::float8 AS quantity, price::float8 AS price FROM broker_transactions WHERE user_id = %s AND broker_account = %s AND asset = 'equity'", (user_id, account))
    rl = _q(cur, "SELECT id FROM lot_loads WHERE user_id = %s AND broker_account = %s AND kind = 'lots' ORDER BY imported_at DESC, id DESC LIMIT 1", (user_id, account))
    reported = _q(cur, "SELECT symbol, isin, buy_date, quantity::float8 AS quantity, unit_cost::float8 AS unit_cost FROM broker_reported_lots WHERE load_id = %s", (rl[0]["id"],)) if rl else []
    got = latest_holdings(cur, user_id, account)
    holdings = None if got is None else got[1]
    syms = sorted({t["symbol"] for t in tx})
    since = min((t["trade_date"] for t in tx), default=today)
    events = load_events(cur, syms, since, today)
    eq_rows, eq_sum = L.build_equity(txns=tx, reported=reported, holdings=holdings, events=events, today=today)
    mtx = _q(cur, "SELECT isin, folio, scheme_name, trade_date, side, quantity::float8 AS quantity, price::float8 AS price FROM broker_transactions WHERE user_id = %s AND broker_account = %s AND asset = 'mf'", (user_id, account))
    closing: dict = {}
    for m in _q(cur, "SELECT mapping FROM lot_loads WHERE user_id = %s AND broker_account = %s AND kind = 'mf' ORDER BY imported_at, id", (user_id, account)):
        for k, v in (m["mapping"].get("closing") or {}).items():
            folio, isin = k.split("|", 1)
            closing[(folio, isin)] = v
    mf_rows, mf_sum = L.build_mf(txns=mtx, closing=closing)
    isins = sorted({x["isin"] for x in mf_rows}) or ["-"]
    meta: dict = {}                                                         # Direct / Regular and Growth / IDCW from the AMFI scheme master, matched on either of the scheme's ISINs
    for r in _q(cur, "SELECT scheme_code, isin_growth_or_payout AS a, isin_reinvest AS b, is_direct, is_growth FROM mf_scheme_master WHERE isin_growth_or_payout = ANY(%s) OR isin_reinvest = ANY(%s)",
                (isins, isins)):
        for k in (r["a"], r["b"]):
            if k:
                meta.setdefault(k, r)
    cur.execute("INSERT INTO lot_runs (user_id, broker_account, inputs, summary) VALUES (%s,%s,%s,%s) RETURNING id",
                (user_id, account, J({"equity_trades": len(tx), "reported_lots": len(reported), "holdings_load": None if got is None else got[0], "mf_trades": len(mtx), "as_of": str(today)}),
                 J(json.loads(json.dumps(eq_sum + mf_sum, default=str)))))
    run = cur.fetchone()["id"]
    vals = []
    for r in eq_rows + mf_rows:
        m = meta.get(r["isin"]) if r["asset"] == "mf" else None
        plan = None if not m or m["is_direct"] is None else ("DIRECT" if m["is_direct"] else "REGULAR")
        opt = None if not m or m["is_growth"] is None else ("GROWTH" if m["is_growth"] else "IDCW")
        vals.append((run, user_id, account, r["asset"], r.get("symbol"), r.get("isin"), r.get("folio"), None if not m else m["scheme_code"], r.get("scheme_name"), plan, opt, r["buy_date"],
                     r["quantity"], r["unit_cost"], r["origin"], r["date_basis"], r["status"], r["note"]))
    if vals:
        psycopg2.extras.execute_values(cur, "INSERT INTO portfolio_tax_lots (run_id, user_id, broker_account, asset, symbol, isin, folio, scheme_code, scheme_name, plan_type, option_type, buy_date, quantity, "
                                            "unit_cost, origin, date_basis, status, note) VALUES %s", vals)
    counts: dict = {}
    for s in eq_sum + mf_sum:
        counts[s["status"]] = counts.get(s["status"], 0) + 1
    return {"run_id": run, "lots": len(vals), "securities": len(eq_sum) + len(mf_sum), "by_status": counts}


# ── apply ─────────────────────────────────────────────────────────────────────────────────────────────────────

def _dedupe_keys(rows: list) -> list:
    seen: dict = {}
    out = []
    for r in rows:
        base = "|".join(str(r.get(k)) for k in ("asset", "symbol", "isin", "folio", "trade_date", "trade_time", "side", "quantity", "price", "trade_ref"))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(hashlib.sha1(f"{base}|{n}".encode()).hexdigest())
    return out


def layout_approved(cur, user_id: str, kind: str, layout: str) -> bool:
    return bool(_q(cur, "SELECT 1 FROM lot_layouts_approved WHERE user_id = %s AND kind = %s AND layout_hash = %s", (user_id, kind, layout)))


def apply_import(prep: Prepared, *, user_id: str, account: str, role: str, filename: str, mapping_ok: bool = False, triage: Optional[dict] = None, today: Optional[date] = None) -> dict:
    """Write one prepared file. Refuses (and writes nothing) when the file was already loaded, when its header layout has not been approved by the owner (dry run first, then --mapping-ok),
    or when a triage entry contradicts an existing one."""
    if not account or len(account.strip()) < 2:
        raise ValueError("a broker account label is required (nothing is assumed)")
    account = account.strip().lower()
    triage = {k.upper(): v for k, v in (triage or {}).items()}
    with get_sync_conn() as conn:
        cur = conn.cursor()
        dup = _q(cur, "SELECT id FROM lot_loads WHERE user_id = %s AND broker_account = %s AND kind = %s AND sha256 = %s", (user_id, account, prep.kind, prep.sha256))
        if dup:
            return {"status": "already loaded", "load_id": dup[0]["id"]}
        if not layout_approved(cur, user_id, prep.kind, prep.layout) and not mapping_ok:
            return {"status": f"REFUSED: this {prep.kind} column layout has not been approved. Run a dry run, check the column mapping and sample against the real file, then repeat with --mapping-ok", **preview(prep)}
        if prep.kind == "holdings":
            files = {h["symbol"] for h in prep.rows}
            unknown = sorted(set(triage) - files)
            if unknown:
                raise ValueError("triage names symbols that are not in this file: " + ", ".join(unknown))
        if prep.kind == "mf":
            files = {r["isin"] for r in prep.rows}
            unknown = sorted(set(triage) - files)
            if unknown:
                raise ValueError("triage names ISINs that are not in this file: " + ", ".join(unknown))
        mapping = dict(prep.mapping)
        if prep.closing:
            mapping["closing"] = {f"{f}|{i}": v for (f, i), v in prep.closing.items()}
        cur.execute("INSERT INTO lot_loads (user_id, broker_account, kind, filename, sha256, layout_hash, n_rows_file, n_accepted, n_rejected, issues, mapping) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (user_id, account, prep.kind, (filename or "")[:200], prep.sha256, prep.layout, prep.n_rows, len(prep.rows), prep.n_rejected, J(prep.issues), J(mapping)))
        load = cur.fetchone()["id"]
        if mapping_ok:
            cur.execute("INSERT INTO lot_layouts_approved (user_id, kind, layout_hash, mapping) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (user_id, prep.kind, prep.layout, J(prep.mapping)))
        dups = 0
        if prep.kind == "holdings":
            psycopg2.extras.execute_values(cur, "INSERT INTO broker_holdings (load_id, symbol, isin, quantity, avg_cost) VALUES %s", [(load, h["symbol"], h["isin"], h["quantity"], h["avg_cost"]) for h in prep.rows])
        elif prep.kind in ("tradebook", "mf"):
            keys = _dedupe_keys(prep.rows)
            vals = [(load, user_id, account, r["asset"], r["symbol"], r["isin"], r["folio"], r["scheme_name"], r["trade_date"], r["trade_time"], r["side"], r["quantity"], r["price"], r["trade_ref"], k)
                    for r, k in zip(prep.rows, keys)]
            ins = psycopg2.extras.execute_values(cur, "INSERT INTO broker_transactions (load_id, user_id, broker_account, asset, symbol, isin, folio, scheme_name, trade_date, trade_time, side, quantity, price, "
                                                      "trade_ref, dedupe_key) VALUES %s ON CONFLICT (user_id, broker_account, dedupe_key) DO NOTHING RETURNING 1", vals, fetch=True)
            dups = len(vals) - len(ins)
            cur.execute("UPDATE lot_loads SET n_duplicate = %s WHERE id = %s", (dups, load))
        else:
            psycopg2.extras.execute_values(cur, "INSERT INTO broker_reported_lots (load_id, symbol, isin, buy_date, quantity, unit_cost) VALUES %s",
                                           [(load, r["symbol"], r["isin"], r["buy_date"], r["quantity"], r["unit_cost"]) for r in prep.rows])
        asset = "mf" if prep.kind == "mf" else "equity"
        tri = set_triage(cur, user_id, role, account, triage, asset) if triage else {"assigned": {}, "unchanged": []}
        made = materialize_pots(cur, user_id, role, account) if prep.kind == "holdings" else {}
        lots = rebuild_lots(cur, user_id, account, today)
        prof.record(conn, user_id, role, "portfolio.lots_import", {"kind": prep.kind, "broker_account": account, "load_id": load, "sha256": prep.sha256, "accepted": len(prep.rows),
                                                                  "rejected": prep.n_rejected, "duplicates": dups, "layout": prep.layout[:16], "approved_now": bool(mapping_ok)})
    return {"status": "loaded", "load_id": load, "accepted": len(prep.rows), "rejected": prep.n_rejected, "duplicates": dups, "triage": tri, "pots": made, "lots": lots}
