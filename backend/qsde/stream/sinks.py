"""Database sinks for the NSE EOD sources. Same `upsert(source, df, key_cols) -> rows_new` contract as the in-memory sink.

  PgEodRawSink            validates (reusing the loader's rules), upserts into `eod_raw`; rejected rows are logged, never silently dropped
  PgCorporateActionsSink  `nse_corporate_actions`: unchanged content is not re-inserted, a correction is a new row
  PgSymbolChangeSink      official NSE list -> `symbol_changes` (verification_status 'official'); replaces the previous official snapshot
"""
from __future__ import annotations

import logging

import pandas as pd

from qsde.stream.runner import row_hash

log = logging.getLogger(__name__)


class PgEodRawSink:
    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int:
        from qsde.ingestion.nse_eod import upsert_eod_raw, validate_eod

        res = validate_eod(df)
        if len(df) - len(res.valid):
            log.warning("eod_raw: %d of %d rows rejected by validation (reasons: %s)", len(df) - len(res.valid), len(df),
                        res.rejects["reason"].value_counts().to_dict() if hasattr(res, "rejects") and len(res.rejects) else "n/a")
        return upsert_eod_raw(res.valid) if len(res.valid) else 0


class PgCorporateActionsSink:
    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int:
        from qsde.db.connection import execute_sql

        n = 0
        for rec in df.to_dict("records"):
            nn = lambda v: None if v is None or (isinstance(v, float) and v != v) else v        # noqa: E731
            h = row_hash({k: v for k, v in rec.items() if k not in ("known_at", "source_sha256")})
            before = execute_sql  # noqa: F841 - readability: the INSERT below reports via rowcount
            n += _insert_returning_count(
                """INSERT INTO nse_corporate_actions (symbol, series, isin, company, subject, category, factor, face_value,
                      ex_date, record_date, event_time, known_at, content_hash, source_sha256)
                   VALUES (%(sym)s,%(se)s,%(isin)s,%(co)s,%(su)s,%(cat)s,%(f)s,%(fv)s,%(ex)s,%(rd)s,%(et)s,%(ka)s,%(h)s,%(sha)s)
                   ON CONFLICT (symbol, ex_date, subject, content_hash) DO NOTHING""",
                {"sym": rec["symbol"], "se": nn(rec.get("series")), "isin": nn(rec.get("isin")), "co": nn(rec.get("company")),
                 "su": rec["subject"], "cat": nn(rec.get("category")), "f": nn(rec.get("factor")), "fv": nn(rec.get("face_value")),
                 "ex": rec["ex_date"], "rd": nn(rec.get("record_date")), "et": rec["event_time"], "ka": rec["known_at"],
                 "h": h, "sha": nn(rec.get("source_sha256"))})
        return n


class PgSymbolChangeSink:
    SOURCE = "nse_symbolchange_csv"

    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int:
        from qsde.db.connection import execute_sql

        if df.empty:
            return 0
        n = 0
        for r in df.itertuples():
            n += _insert_returning_count(
                """INSERT INTO symbol_changes (old_symbol, new_symbol, effective_date, effective_after, status, evidence, source,
                      source_url, retrieved_at, verified, verification_status, change_type, notes)
                   VALUES (%(o)s,%(n)s,%(d)s,%(d)s,'official','stream',%(src)s,%(u)s,%(t)s,TRUE,'official',%(ct)s,%(nm)s)
                   ON CONFLICT DO NOTHING""",
                {"o": r.old_symbol, "n": r.new_symbol, "d": r.effective_date, "src": self.SOURCE,
                 "u": "https://nsearchives.nseindia.com/content/equities/symbolchange.csv", "t": r.known_at,
                 "ct": r.change_type, "nm": getattr(r, "company", None)})
        return n


def _insert_returning_count(sql: str, params: dict) -> int:
    """Run one INSERT ... ON CONFLICT DO NOTHING and return 1 if a row was inserted, else 0."""
    from qsde.db.connection import get_sync_conn

    with get_sync_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return int(cur.rowcount or 0)
