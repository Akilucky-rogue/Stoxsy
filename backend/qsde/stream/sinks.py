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


class PgBseEodSink:
    """BSE bhavcopy -> bse_eod_raw (its own table; NSE's eod_raw is never touched). Reloading a session replaces its rows."""

    COLS = ("scrip_code", "date", "ticker", "name", "grp", "isin", "open", "high", "low", "close", "last", "prev_close", "volume",
            "turnover_value", "trades", "source_format", "source_file", "known_at")

    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int:
        import psycopg2.extras

        from qsde.db.connection import get_sync_conn

        if df.empty:
            return 0
        rows = []
        for r in df[list(self.COLS)].itertuples(index=False):
            rows.append(tuple(None if (v is None or (isinstance(v, float) and v != v) or v is pd.NaT) else
                              (v.date() if isinstance(v, pd.Timestamp) and c == "date" else v.to_pydatetime() if isinstance(v, pd.Timestamp) else
                               int(v) if c in ("scrip_code", "volume", "trades") else v)
                              for c, v in zip(self.COLS, r)))
        sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in self.COLS if c not in ("scrip_code", "date"))
        sql = (f"INSERT INTO bse_eod_raw ({', '.join(self.COLS)}) VALUES %s ON CONFLICT (scrip_code, date) DO UPDATE SET {sets}, loaded_at = NOW()")
        with get_sync_conn() as conn:
            psycopg2.extras.execute_values(conn.cursor(), sql, rows, page_size=1000)
        return len(rows)


class PgMfNavSink:
    """AMFI NAV rows -> mf_nav (+ the load registry with the source hash) and the scheme master (names, categories, ISINs, classification)."""

    def upsert(self, source: str, df: pd.DataFrame, key_cols: tuple) -> int:
        import psycopg2.extras

        from qsde.db.connection import get_sync_conn
        from qsde.funds.classify import classify

        if df.empty:
            return 0
        sha, kind = df["source_sha256"].iloc[0], df["kind"].iloc[0]
        wf, wt = df["window_from"].iloc[0], df["window_to"].iloc[0]
        known = df["known_at"].max().to_pydatetime() if "known_at" in df else None
        with get_sync_conn() as conn:
            cur = conn.cursor()
            cur.execute("""INSERT INTO mf_nav_loads (sha256, source, window_from, window_to, known_at, n_rows) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (sha256) DO UPDATE SET n_rows = EXCLUDED.n_rows RETURNING id""",
                        (sha, "amfi_navall" if kind == "navall" else "amfi_nav_history", None if wf is None else wf, None if wt is None else wt, known, int(len(df))))
            load_id = cur.fetchone()["id"]
            nav = [(int(r.scheme_code), r.date.date(), float(r.nav), load_id) for r in df[["scheme_code", "date", "nav"]].itertuples(index=False)]
            psycopg2.extras.execute_values(cur, """INSERT INTO mf_nav (scheme_code, date, nav, load_id) VALUES %s
                ON CONFLICT (scheme_code, date) DO UPDATE SET nav = EXCLUDED.nav, load_id = EXCLUDED.load_id WHERE mf_nav.nav IS DISTINCT FROM EXCLUDED.nav""",
                                           nav, page_size=20000)
            last = df.sort_values("date").groupby("scheme_code").tail(1)
            rng = df.groupby("scheme_code")["date"].agg(["min", "max"])
            sch = []
            for r in last.itertuples(index=False):
                c = classify(r.category_raw, r.name, r.plan, r.option_type)
                sch.append((int(r.scheme_code), r.name, r.amc, r.category_raw, c["scheme_type"], c["broad_category"], c["sub_category"], r.plan, r.option_type, r.isin_a, r.isin_b,
                            c["asset_class"], c["is_etf"], c["is_index"], c["is_liquid"], c["is_fof"], c["is_direct"], c["is_growth"], c["classification_evidence"], c["fund_group"], c["unclassified_reason"],
                            rng.loc[r.scheme_code, "min"].date(), rng.loc[r.scheme_code, "max"].date()))
            psycopg2.extras.execute_values(cur, """INSERT INTO mf_scheme_master (scheme_code, name, amc, category_raw, scheme_type, broad_category, sub_category, plan, option_type,
                isin_growth_or_payout, isin_reinvest, asset_class, is_etf, is_index, is_liquid, is_fof, is_direct, is_growth, classification_evidence, fund_group, unclassified_reason, first_nav_date, last_nav_date) VALUES %s
                ON CONFLICT (scheme_code) DO UPDATE SET
                  name = CASE WHEN EXCLUDED.last_nav_date >= mf_scheme_master.last_nav_date THEN EXCLUDED.name ELSE mf_scheme_master.name END,
                  amc = COALESCE(EXCLUDED.amc, mf_scheme_master.amc), category_raw = COALESCE(EXCLUDED.category_raw, mf_scheme_master.category_raw),
                  scheme_type = COALESCE(EXCLUDED.scheme_type, mf_scheme_master.scheme_type), broad_category = COALESCE(EXCLUDED.broad_category, mf_scheme_master.broad_category),
                  sub_category = COALESCE(EXCLUDED.sub_category, mf_scheme_master.sub_category), plan = COALESCE(EXCLUDED.plan, mf_scheme_master.plan),
                  option_type = COALESCE(EXCLUDED.option_type, mf_scheme_master.option_type),
                  isin_growth_or_payout = COALESCE(EXCLUDED.isin_growth_or_payout, mf_scheme_master.isin_growth_or_payout),
                  isin_reinvest = COALESCE(EXCLUDED.isin_reinvest, mf_scheme_master.isin_reinvest),
                  asset_class = EXCLUDED.asset_class, is_etf = EXCLUDED.is_etf, is_index = EXCLUDED.is_index, is_liquid = EXCLUDED.is_liquid, is_fof = EXCLUDED.is_fof,
                  is_direct = COALESCE(EXCLUDED.is_direct, mf_scheme_master.is_direct), is_growth = COALESCE(EXCLUDED.is_growth, mf_scheme_master.is_growth),
                  classification_evidence = EXCLUDED.classification_evidence, fund_group = EXCLUDED.fund_group, unclassified_reason = EXCLUDED.unclassified_reason,
                  first_nav_date = LEAST(mf_scheme_master.first_nav_date, EXCLUDED.first_nav_date), last_nav_date = GREATEST(mf_scheme_master.last_nav_date, EXCLUDED.last_nav_date),
                  updated_at = NOW()""", sch, page_size=2000)
        return len(nav)
