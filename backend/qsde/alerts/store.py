"""Alert storage. `plan_sync` is pure (what to insert / refresh / resolve); the functions below execute it.

Lifecycle: a new condition opens an alert; a condition that keeps holding refreshes the SAME alert (last_seen, occurrences) and keeps an acknowledgement unless the severity
got worse; a condition that clears resolves the alert. Nothing is ever deleted.
"""
from __future__ import annotations

from typing import Optional

import pandas as pd
import psycopg2.extras

SEV_RANK = {"info": 0, "warning": 1, "critical": 2}


def plan_sync(live: list, conditions: list) -> dict:
    """live: existing non-resolved alerts of one scope (dicts with id, dedup_key, severity, state). conditions: alert dicts from the rules."""
    by_key = {a["dedup_key"]: a for a in live}
    seen: set = set()
    insert, update = [], []
    for c in conditions:
        if c["dedup_key"] in seen:
            continue
        seen.add(c["dedup_key"])
        cur = by_key.get(c["dedup_key"])
        if cur is None:
            insert.append(c)
        else:
            worse = SEV_RANK[c["severity"]] > SEV_RANK[cur["severity"]]
            update.append({**c, "id": cur["id"], "reopen": bool(worse and cur["state"] == "acknowledged")})
    resolve = [a["id"] for a in live if a["dedup_key"] not in seen]
    return {"insert": insert, "update": update, "resolve": resolve}


def sync(conn, user_id: Optional[str], conditions: list) -> dict:
    """Apply plan_sync for one scope inside the caller's transaction."""
    cur = conn.cursor()
    cur.execute("SELECT id, dedup_key, severity, state FROM alert_events WHERE state <> 'resolved' AND COALESCE(user_id, '') = %s FOR UPDATE", (user_id or "",))
    live = [dict(r) for r in cur.fetchall()]
    plan = plan_sync(live, conditions)
    J = psycopg2.extras.Json
    for c in plan["insert"]:
        cur.execute("INSERT INTO alert_events (user_id, rule_id, severity, title, detail, entity_type, entity_id, dedup_key, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (user_id, c["rule_id"], c["severity"], c["title"], c["detail"], c["entity_type"], c["entity_id"], c["dedup_key"], J(c["payload"])))
    for c in plan["update"]:
        cur.execute("UPDATE alert_events SET severity=%s, title=%s, detail=%s, payload=%s, last_seen=NOW(), occurrences=occurrences+1, "
                    "state = CASE WHEN %s THEN 'open' ELSE state END, acked_at = CASE WHEN %s THEN NULL ELSE acked_at END WHERE id=%s",
                    (c["severity"], c["title"], c["detail"], J(c["payload"]), c["reopen"], c["reopen"], c["id"]))
    if plan["resolve"]:
        cur.execute("UPDATE alert_events SET state='resolved', resolved_at=NOW(), resolved_why='condition cleared' WHERE id = ANY(%s)", (plan["resolve"],))
    return {"opened": len(plan["insert"]), "refreshed": len(plan["update"]), "resolved": len(plan["resolve"])}


def acknowledge(conn, alert_id: int, user_id: str, allowed_user_scope: str) -> bool:
    """Acknowledge an open alert the caller may see (system-wide, or their own). Returns False if there is nothing to acknowledge."""
    cur = conn.cursor()
    cur.execute("UPDATE alert_events SET state='acknowledged', acked_by=%s, acked_at=NOW() WHERE id=%s AND state='open' AND (user_id IS NULL OR user_id=%s)", (user_id, alert_id, allowed_user_scope))
    return cur.rowcount == 1


def list_alerts(read_sql, user_id: str, state: str = "live", severity: Optional[str] = None, limit: int = 100, offset: int = 0) -> tuple[int, pd.DataFrame]:
    where, params = ["(user_id IS NULL OR user_id = :u)"], {"u": user_id}
    if state == "live":
        where.append("state <> 'resolved'")
    elif state in ("open", "acknowledged", "resolved"):
        where.append("state = :s")
        params["s"] = state
    if severity:
        where.append("severity = :sev")
        params["sev"] = severity
    w = " AND ".join(where)
    n = int(read_sql(f"SELECT count(*) AS n FROM alert_events WHERE {w}", params=params)["n"].iloc[0])
    d = read_sql(f"SELECT * FROM alert_events WHERE {w} ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, last_seen DESC, id DESC LIMIT :lim OFFSET :off",
                 params={**params, "lim": limit, "off": offset})
    return n, d
