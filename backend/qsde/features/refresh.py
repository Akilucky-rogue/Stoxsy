"""Nightly feature refresh: extend the Parquet feature store with the sessions that arrived since its newest day, as an immutable *increment* snapshot (ADR 0001).

Why not rebuild: a full snapshot is about 500 MB and several minutes; a nightly rebuild would add roughly 180 GB a year. Why full history per symbol: the earlier daily job
(ffd_v1, retired) differenced only a short tail with a different kernel than the backfill, so tail rows disagreed with backfilled rows. Here the engine is given each symbol's whole
history (`start`, as in the full build) and only the new symbol-days are written, so a value is identical however it was written; `tests/test_feature_refresh.py` proves that on real data.

Refuses to run without a full snapshot to extend. Writes nothing when nothing is pending. A snapshot whose coverage of the expected symbol-days is below `min_coverage` is not promoted."""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from typing import Optional, Sequence

import pandas as pd

from qsde.factors.versions import FFD_STITCHED_VERSION
from qsde.features import store

log = logging.getLogger(__name__)
VERSIONS = ("ffd_v2s", "ffd_v2")
METHOD, MIN_ADV = "adv60_top500_v1", 10.0               # the universe the full builds use
TOLERANCE_SESSIONS = 2                                    # the routine runs after the close: one or two sessions behind is normal


def features_status(latest_feature: Optional[date], price_sessions: Sequence[date], tolerance: int = TOLERANCE_SESSIONS) -> tuple[str, Optional[int]]:
    """('ok' | 'warn', number of price sessions after the newest feature day). Nothing built yet is ('warn', None)."""
    if latest_feature is None:
        return "warn", None
    behind = sum(1 for d in price_sessions if d > latest_feature)
    return ("ok" if behind <= tolerance else "warn"), behind


def latest_feature_date(version: str, root=store.ROOT) -> Optional[date]:
    """Newest day held by the current snapshots of a version (read from their manifests, no data files opened)."""
    snaps = store.current_snapshots(version, root=root)
    return max((pd.Timestamp(s.date_max).date() for s in snaps), default=None)


def _db_dates(sql: str, params: Optional[dict] = None) -> list:
    from qsde.db.connection import read_sql

    return [pd.Timestamp(d).date() for d in read_sql(sql, params=params).iloc[:, 0].tolist()]


def price_sessions_after(latest: Optional[date]) -> list:
    """Price sessions (distinct `ohlcv` dates) after `latest`; all recent sessions when there is no feature day yet."""
    floor = latest or (date.today() - timedelta(days=60))
    return _db_dates("SELECT DISTINCT date FROM ohlcv WHERE date > :d ORDER BY date", {"d": floor})


def _stitching():
    """The security map and applied corporate-action events the stitched version (ffd_v2s) needs (same inputs as scripts/compute_factors_pit.py --stitched)."""
    from qsde.db.connection import read_sql
    from qsde.ingestion.security_master import SecurityMap

    iv = read_sql("SELECT security_id, symbol, valid_from, valid_to FROM security_master")
    iv["valid_from"] = pd.to_datetime(iv["valid_from"])
    iv["valid_to"] = pd.to_datetime(iv["valid_to"].map(lambda d: d if d.year < 2200 else pd.Timestamp("2200-01-01").date()))
    events = read_sql("SELECT symbol, ex_date, price_factor, applied FROM corp_action_events WHERE applied")
    return SecurityMap(iv), events


def compute_rows(version: str, uni: pd.DataFrame, start: str = "2016-01-01", chunk: int = 50) -> pd.DataFrame:
    """Factor rows for exactly the symbol-days in `uni` (columns date, symbol), computed from each symbol's full history since `start`."""
    from qsde.factors.engine import compute_ffd_panel

    symbols = sorted(uni["symbol"].unique())
    smap = events = None
    if version == FFD_STITCHED_VERSION:
        smap, events = _stitching()
        rep: dict = {}
        for s in symbols:                                        # one representative symbol per security, so old and new names are never computed in different chunks
            rep.setdefault(smap.security_of(s) or s, s)
        symbols = sorted(rep.values())
    parts, t0 = [], time.time()
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        part_all = set(part)
        if smap is not None:
            for s in part:
                sid = smap.security_of(s)
                if sid:
                    part_all |= set(smap.intervals.loc[smap.intervals["security_id"] == sid, "symbol"])
        _, to_write, _ = compute_ffd_panel(part, start=start, universe=uni[uni["symbol"].isin(part_all)], security_map=smap, events=events)
        if len(to_write):
            parts.append(to_write)
        log.info("features %s: computed %d/%d symbols (%.0fs)", version, min(i + chunk, len(symbols)), len(symbols), time.time() - t0)
    return pd.concat(parts) if parts else pd.DataFrame()


def refresh(version: str, since: Optional[date] = None, until: Optional[date] = None, symbols: Optional[Sequence[str]] = None, dry_run: bool = False,
            root=store.ROOT, base_root=store.ROOT, register: bool = True, start: str = "2016-01-01", min_coverage: float = 0.98) -> dict:
    """Add the point-in-time universe symbol-days from `since` (default: the day after the newest feature day) to `until` (default: the newest universe day) as one increment snapshot.

    `base_root` is where the full snapshot to extend is looked up; `root` is where the increment is written (they differ only in tests)."""
    from qsde.db.connection import read_sql

    base = store.current_snapshots(version, root=base_root)
    fulls = [s for s in base if s.kind == "full"]
    if not fulls:
        raise ValueError(f"no full snapshot of {version} to extend: build one first (scripts/compute_factors_pit.py)")
    latest = latest_feature_date(version, root=base_root)
    since = since or (latest + timedelta(days=1))
    until = until or _db_dates("SELECT max(date) FROM universe_pit WHERE method = :m AND adv_cr >= :a", {"m": METHOD, "a": MIN_ADV})[0]
    out = {"version": version, "latest_feature_date": str(latest), "since": str(since), "until": str(until)}
    if since > until:
        return {**out, "status": "up_to_date", "days": 0, "symbol_days": 0}
    uni = read_sql("SELECT date, symbol, rank, adv_cr FROM universe_pit WHERE method = :m AND adv_cr >= :a AND date BETWEEN :s AND :u",
                   params={"m": METHOD, "a": MIN_ADV, "s": since, "u": until})
    if symbols is not None:
        uni = uni[uni["symbol"].isin(list(symbols))]
    out.update(days=int(uni["date"].nunique()), symbol_days=int(len(uni)))
    if uni.empty:
        return {**out, "status": "up_to_date"}
    if dry_run:
        return {**out, "status": "pending"}
    t0 = time.time()
    rows = compute_rows(version, uni, start=start)
    if rows.empty:
        raise RuntimeError(f"{version}: no feature rows were computed for {len(uni)} expected symbol-days")
    from qsde.ml.data import OFFICIAL

    w = store.SnapshotWriter(version, fulls[-1].universe_version, kind="increment", root=root, expected_keys=uni[["symbol", "date"]], min_coverage=min_coverage, min_free_gb=5.0,
                             params={"start": start, "universe_method": METHOD, "min_adv_cr": MIN_ADV, "stitched": version == FFD_STITCHED_VERSION, "since": str(since), "until": str(until)},
                             extra_manifest={"price_policy": OFFICIAL, "extends_full_snapshot": fulls[-1].snapshot_id})
    w.add(rows)
    snap = w.close(register=register)
    return {**out, "status": "written", "snapshot_id": snap.snapshot_id, "rows": snap.n_rows, "symbols": snap.n_symbols, "seconds": round(time.time() - t0, 1)}
