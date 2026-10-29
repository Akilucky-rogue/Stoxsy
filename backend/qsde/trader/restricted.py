"""The owner's own restricted list. One implementation for the API and the CLI, so every change is audited the same way (mode_audit) whichever door it came through.

Nothing is preset: the list starts empty. Which securities are restricted, why, and until when is for the owner and their compliance officer to decide. Releasing keeps the row.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from qsde.modes import profile as prof


def list_entries(read_sql, user_id: str, today: Optional[date] = None) -> list:
    today = today or date.today()
    d = read_sql("SELECT id, symbol, reason, restricted_until, added_by_role, added_at, released_at FROM compliance_restricted_list WHERE user_id = :u ORDER BY added_at DESC", params={"u": user_id})
    out = []
    for r in d.to_dict("records"):
        until = None if pd.isna(r["restricted_until"]) else r["restricted_until"]
        released = not pd.isna(r["released_at"])
        out.append({"id": int(r["id"]), "symbol": r["symbol"], "reason": r["reason"], "restricted_until": None if until is None else str(until), "added_at": r["added_at"].isoformat(),
                    "released_at": r["released_at"].isoformat() if released else None, "active": (not released) and (until is None or until >= today)})
    return out


def add(conn, user_id: str, role: str, symbol: str, reason: str, restricted_until: Optional[date] = None) -> dict:
    sym, reason = symbol.strip().upper(), reason.strip()
    if not sym:
        raise ValueError("a symbol is required")
    if len(reason) < 3:
        raise ValueError("a reason is required (at least 3 characters)")
    cur = conn.cursor()
    cur.execute("INSERT INTO compliance_restricted_list (user_id, symbol, reason, restricted_until, added_by_role) VALUES (%s,%s,%s,%s,%s) RETURNING id", (user_id, sym, reason, restricted_until, role))
    rid = cur.fetchone()["id"]
    prof.record(conn, user_id, role, "trader.restricted.add", {"symbol": sym, "reason": reason, "until": None if restricted_until is None else str(restricted_until)})
    return {"id": rid, "symbol": sym}


def release(conn, user_id: str, role: str, symbol: str) -> dict:
    sym = symbol.strip().upper()
    cur = conn.cursor()
    cur.execute("UPDATE compliance_restricted_list SET released_at = NOW() WHERE user_id = %s AND symbol = %s AND released_at IS NULL", (user_id, sym))
    n = cur.rowcount
    if not n:
        raise LookupError(f"{sym} has no active restriction")
    prof.record(conn, user_id, role, "trader.restricted.release", {"symbol": sym, "entries": n})
    return {"symbol": sym, "released": n}
