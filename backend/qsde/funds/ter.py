"""Expense ratio (AMFI TER) per fund scheme, as published and broken into its parts. Read-only; nothing is computed that AMFI did not publish.

The day used is the latest COMPLETE TER day (a day AMFI is still filing is never shown). A scheme's figure may be carried forward from an earlier day for up to CARRY_DAYS; older than that it is
returned with status "stale" and its own date. The current value of a day is the newest revision (mf_ter_revisions) if AMFI ever changed it, else the first-seen row. Values are never corrected:
quality flags are returned with plain-language text. Statuses: ok | not_offered (the plan has no TER) | stale | not_covered (no match in AMFI's lists, or no row)."""
from __future__ import annotations

from datetime import date
from typing import Optional

from qsde.ingestion.amfi_ter_db import CARRY_DAYS, latest_complete_date

PARTS = {"base": "ber", "brokerage": "brokerage", "transaction": "transaction", "statutory": "statutory", "total": "total"}
FLAG_TEXT = {
    "total_gt_3_regular": "The Regular plan's total expense is above 3% as published by AMFI. Check it before relying on it.",
    "total_gt_3_direct": "The Direct plan's total expense is above 3% as published by AMFI. Check it before relying on it.",
    "direct_not_below_regular": "The Direct plan is not cheaper than the Regular plan, which is unusual. Shown as published.",
    "sum_mismatch": "The parts AMFI lists do not add up to the total it lists. Shown as published.",
}
_COLS = [f"{p}_{c}" for p in "rd" for c in PARTS.values()]


def _empty(status: str, reason: str, plan: Optional[str] = None, when: Optional[date] = None) -> dict:
    return {"status": status, "reason": reason, "as_of": str(when) if when else None, "plan": plan, **{k: None for k in PARTS}, "flags": [], "flag_text": []}


def _with_conn(conn, fn):
    if conn is not None:
        return fn(conn)
    from qsde.db.connection import get_sync_conn

    with get_sync_conn() as c:
        return fn(c)


def expense_as_of(conn=None) -> Optional[date]:
    """The latest complete TER day, or None when nothing is loaded."""
    return _with_conn(conn, lambda c: latest_complete_date(c.cursor()))


def _num(v):
    return None if v is None else float(v)


def _parts(row: dict, plan: str) -> dict:
    """The five parts for this plan: from the newest revision when there is one (it holds all ten values), else from the first-seen row."""
    p = plan[0]
    rv = row.get("new_values")
    return {k: _num(rv.get(f"{p}_{c}") if rv is not None else row[f"{p}_{c}"]) for k, c in PARTS.items()}


def expense_for(codes: list, as_of: Optional[date] = None, conn=None) -> dict:
    """{scheme_code: expense entry} for every requested code, from ONE query (see the module docstring for the entry's statuses). `as_of` defaults to the latest complete day."""
    def run(c):
        cur = c.cursor()
        day = as_of or latest_complete_date(cur)
        if day is None or not codes:
            return {int(k): _empty("not_covered", "no AMFI TER data is loaded") for k in codes}
        cur.execute(f"""WITH m AS (SELECT DISTINCT ON (mm.scheme_code) mm.scheme_code, mm.plan, mm.nsdl_code
                                   FROM mf_ter_map mm LEFT JOIN mf_scheme_master ms ON ms.scheme_code = mm.scheme_code WHERE mm.scheme_code = ANY(%s)
                                   ORDER BY mm.scheme_code, (mm.plan = CASE WHEN ms.is_direct THEN 'direct' ELSE 'regular' END) DESC)
                        SELECT m.scheme_code, m.plan, t.ter_date, t.flags, {', '.join('t.' + k for k in _COLS)}, rv.new_values, rv.flags AS rv_flags
                        FROM m LEFT JOIN LATERAL (SELECT * FROM mf_ter x WHERE x.nsdl_code = m.nsdl_code AND x.ter_date <= %s ORDER BY x.ter_date DESC LIMIT 1) t ON TRUE
                        LEFT JOIN LATERAL (SELECT r.new_values, r.flags FROM mf_ter_revisions r WHERE r.nsdl_code = m.nsdl_code AND r.ter_date = t.ter_date ORDER BY r.id DESC LIMIT 1) rv ON TRUE""",
                    ([int(k) for k in codes], day))
        out = {int(k): _empty("not_covered", "AMFI's TER lists have no match for this scheme (closed, not listed, or its name could not be matched)") for k in codes}
        for r in cur.fetchall():
            if r["ter_date"] is None:
                out[r["scheme_code"]] = _empty("not_covered", f"matched to an AMFI scheme, but AMFI has no row up to {day}", r["plan"])
                continue
            parts = _parts(r, r["plan"])
            flags = list((r["rv_flags"] if r["new_values"] is not None else r["flags"]) or [])             # a revision brings the flags of its own values
            age = (day - r["ter_date"]).days
            if parts["total"] is None:
                status, reason = "not_offered", "AMFI lists no expense ratio for this plan (not offered)"
            elif age > CARRY_DAYS:
                status, reason = "stale", f"older figure: AMFI's latest row for this scheme is from {r['ter_date']}"
            else:
                status, reason = "ok", None
            out[r["scheme_code"]] = {"status": status, "reason": reason, "as_of": str(r["ter_date"]), "plan": r["plan"], **parts, "flags": flags, "flag_text": [FLAG_TEXT.get(f, f) for f in flags]}
        return out

    return _with_conn(conn, run)


def expense_history(code: int, months: int = 12, as_of: Optional[date] = None, conn=None) -> list:
    """One point per calendar month (that month's last complete day), oldest first: month 'YYYY-MM', as_of, and the five parts. Brokerage and transaction cost reset every month, so the points differ."""
    def run(c):
        cur = c.cursor()
        day = as_of or latest_complete_date(cur)
        cur.execute("SELECT plan, nsdl_code FROM mf_ter_map WHERE scheme_code = %s ORDER BY (plan = 'direct') DESC LIMIT 2", (int(code),))
        maps = cur.fetchall()
        if day is None or not maps:
            return []
        cur.execute("SELECT is_direct FROM mf_scheme_master WHERE scheme_code = %s", (int(code),))
        mr = cur.fetchone()
        want = "direct" if mr and mr["is_direct"] else "regular"
        m = next((x for x in maps if x["plan"] == want), maps[0])
        cur.execute(f"""SELECT DISTINCT ON (date_trunc('month', t.ter_date)) t.ter_date, {', '.join('t.' + k for k in _COLS)}, rv.new_values
                        FROM mf_ter t LEFT JOIN LATERAL (SELECT r.new_values FROM mf_ter_revisions r WHERE r.nsdl_code = t.nsdl_code AND r.ter_date = t.ter_date ORDER BY r.id DESC LIMIT 1) rv ON TRUE
                        WHERE t.nsdl_code = %s AND t.ter_date <= %s ORDER BY date_trunc('month', t.ter_date) DESC, t.ter_date DESC LIMIT %s""", (m["nsdl_code"], day, int(months)))
        rows = cur.fetchall()[::-1]
        return [{"month": r["ter_date"].strftime("%Y-%m"), "as_of": str(r["ter_date"]), **_parts(r, m["plan"])} for r in rows]

    return _with_conn(conn, run)
