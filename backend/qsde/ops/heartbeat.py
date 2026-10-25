"""Liveness heartbeats. A component calls `beat()` regularly; `read_all()` feeds the alert engine and `stoxsy doctor`. Writing a heartbeat must never break the component:
a database error is logged and swallowed."""
from __future__ import annotations

import logging
import socket
from typing import Optional

import psycopg2.extras

log = logging.getLogger(__name__)
STATUSES = ("ok", "degraded", "failed", "stopped")


def beat(component: str, status: str = "ok", detail: Optional[dict] = None) -> bool:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    try:
        from qsde.db.connection import get_sync_conn

        with get_sync_conn() as c:
            c.cursor().execute(
                "INSERT INTO ops_heartbeat (component, last_beat, status, host, detail) VALUES (%s, NOW(), %s, %s, %s) "
                "ON CONFLICT (component) DO UPDATE SET last_beat = NOW(), status = EXCLUDED.status, host = EXCLUDED.host, detail = EXCLUDED.detail",
                (component, status, socket.gethostname(), psycopg2.extras.Json(detail or {})))
        return True
    except Exception as e:                                         # noqa: BLE001
        log.warning("heartbeat %s not written: %s", component, e)
        return False


def read_all() -> list:
    from qsde.db.connection import read_sql

    d = read_sql("SELECT component, last_beat, status, host, detail FROM ops_heartbeat ORDER BY component")
    return d.to_dict("records")
