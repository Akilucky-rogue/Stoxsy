"""Read-only endpoints for the new UI: defensive signal, system status, research reports.

Everything here is a reader of artifacts the research pipeline already produced (nothing trains, promotes or places anything), with typed
responses so the frontend can be generated against the OpenAPI document.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from qsde.config.settings import settings
from qsde.db.connection import read_sql

router = APIRouter()
DATA = Path(os.environ.get("STOXSY_DATA_DIR", r"D:\Stoxsy\data"))
SIGNAL = DATA / "signals" / "defensive" / "latest.parquet"
DEF_EVAL = DATA / "reports" / "defensive" / "defensive_eval.json"
REPORT_DIRS = {"model_cards": DATA / "reports" / "model_cards", "research": DATA / "reports" / "research",
               "adjustment": DATA / "reports" / "adjustment", "corp_actions": DATA / "reports" / "corp_actions",
               "data_quality": DATA / "reports" / "data_quality", "defensive": DATA / "reports" / "defensive"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+\.md$")
DISCLAIMER = ("Research and decision support, not investment advice. This is a risk-aware portfolio-construction signal, not a return "
              "forecast; the models in this system have not demonstrated a tradable edge and promotion is disabled.")


# ── defensive signal ─────────────────────────────────────────────────

class DefensiveRow(BaseModel):
    symbol: str
    defensive_score: float
    risk_bucket: Optional[int] = None
    atr_pct: Optional[float] = None
    vol60: Optional[float] = None
    downside60: Optional[float] = None
    dd120: Optional[float] = None
    liq: Optional[float] = None
    warnings: str = ""


class DefensiveLatest(BaseModel):
    as_of: Optional[str]
    regime: Optional[str]
    n: int
    bucket_counts: dict
    bucket_profile: dict
    rows: list[DefensiveRow]
    evaluation: Optional[dict] = None
    disclaimer: str = DISCLAIMER


@router.get("/defensive/latest", response_model=DefensiveLatest)
def defensive_latest(bucket: Optional[int] = Query(None, ge=1, le=5), q: Optional[str] = None, limit: int = Query(100, ge=1, le=600),
                     sort: str = Query("score_desc", pattern="^(score_desc|score_asc|symbol)$")):
    if not SIGNAL.exists():
        raise HTTPException(status_code=404, detail="no defensive signal has been computed yet (scripts/defensive_eval.py --latest)")
    d = pd.read_parquet(SIGNAL)
    counts = {int(k): int(v) for k, v in d["risk_bucket"].value_counts().sort_index().items()}
    if bucket is not None:
        d = d[d["risk_bucket"] == bucket]
    if q:
        d = d[d["symbol"].str.contains(q.strip().upper(), regex=False)]
    d = d.sort_values({"score_desc": "defensive_score", "score_asc": "defensive_score", "symbol": "symbol"}[sort],
                      ascending=(sort != "score_desc"))
    cols = ["symbol", "defensive_score", "risk_bucket", "atr_pct", "vol60", "downside60", "dd120", "liq", "warnings"]
    rows = d.head(limit)[cols].astype(object).where(d.head(limit)[cols].notna(), None).to_dict("records")
    prof = (pd.read_parquet(SIGNAL).groupby("risk_bucket")
            .agg(n=("symbol", "size"), median_atr_pct=("atr_pct", "median"), median_vol60=("vol60", "median"), median_dd120=("dd120", "median"),
                 median_liq=("liq", "median")))
    profile = {str(int(k)): {c: float(v) for c, v in row.items()} for k, row in prof.iterrows()}
    ev = json.loads(DEF_EVAL.read_text(encoding="utf-8")) if DEF_EVAL.exists() else None
    full = pd.read_parquet(SIGNAL, columns=["date", "regime"])
    return DefensiveLatest(as_of=str(pd.Timestamp(full["date"].iloc[0]).date()), regime=str(full["regime"].iloc[0]), n=int(len(d)),
                           bucket_counts=counts, bucket_profile=profile, rows=rows, evaluation=ev)


# ── system status ────────────────────────────────────────────────────

class SystemStatus(BaseModel):
    database: str
    ohlcv_latest: Optional[str]
    eod_raw_latest: Optional[str]
    current_snapshots: list[dict]
    adjustment_series: list[dict]
    auth_enforced: bool
    promotion_enabled: bool
    scheduled_downloads_enabled: bool = False
    exchanges: dict = {}
    notes: list[str]


@router.get("/system/status", response_model=SystemStatus)
def system_status():
    notes = []
    try:
        o = read_sql("SELECT max(date) AS d FROM ohlcv")["d"].iloc[0]
        e = read_sql("SELECT max(date) AS d FROM eod_raw")["d"].iloc[0]
        snaps = read_sql("SELECT snapshot_id, factor_version, universe_version, n_rows, n_symbols, date_max, built_at FROM feature_snapshots "
                         "WHERE kind = 'full' ORDER BY built_at DESC LIMIT 4")
        adj = read_sql("SELECT version, kind, validation_status, n_events, rows, built_at FROM adjustment_runs ORDER BY built_at DESC LIMIT 6")
        bse = read_sql("SELECT max(date) AS d, count(DISTINCT date) AS n FROM bse_eod_raw")
        xr = read_sql("SELECT link_status, count(*) AS n FROM listing_xref GROUP BY 1")
        exchanges = {"NSE": {"prices": "official, corporate-action adjusted", "in_universe_signals_cards": True},
                     "BSE": {"prices": "raw_prices_only", "in_universe_signals_cards": False, "last_session": str(pd.Timestamp(bse["d"].iloc[0]).date()) if pd.notna(bse["d"].iloc[0]) else None,
                             "sessions": int(bse["n"].iloc[0]), "cross_listing": {r.link_status: int(r.n) for r in xr.itertuples()},
                             "reason": "no corporate-action source for BSE prices yet; BSE-only names are excluded from the universe, the defensive signal and decision cards"}}
        db = "ok"
    except Exception as ex:  # noqa: BLE001
        return SystemStatus(database=f"error: {type(ex).__name__}", ohlcv_latest=None, eod_raw_latest=None, current_snapshots=[],
                            adjustment_series=[], auth_enforced=settings.auth_enforced, promotion_enabled=False, notes=["database unreachable"])
    promo = os.environ.get("QSDE_ML_PROMOTION_ENABLED", "false").strip().lower() == "true"
    if promo:
        notes.append("model promotion is ENABLED in the environment")
    if not settings.auth_enforced:
        notes.append("authentication is not enforced (local development mode)")
    rec = lambda df: json.loads(df.to_json(orient="records", date_format="iso"))      # noqa: E731
    return SystemStatus(database=db, ohlcv_latest=str(pd.Timestamp(o).date()) if pd.notna(o) else None,
                        eod_raw_latest=str(pd.Timestamp(e).date()) if pd.notna(e) else None, current_snapshots=rec(snaps),
                        adjustment_series=rec(adj), auth_enforced=settings.auth_enforced, promotion_enabled=promo, exchanges=exchanges, notes=notes)


# ── research reports ─────────────────────────────────────────────────

class ReportItem(BaseModel):
    group: str
    name: str
    title: str
    bytes: int
    modified: str
    superseded: bool = False


class ReportBody(BaseModel):
    group: str
    name: str
    markdown: str


def _title(p: Path) -> str:
    """First markdown heading; falls back to the first line that is not a blockquote banner."""
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for ln in lines:
        if ln.startswith("# "):
            return ln[2:].strip()[:160]
    return next((ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith(">")), p.stem)[:160]


@router.get("/research/reports", response_model=list[ReportItem])
def research_reports(group: Optional[str] = None):
    out = []
    for g, d in REPORT_DIRS.items():
        if group and g != group or not d.exists():
            continue
        for p in sorted(d.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
            out.append(ReportItem(group=g, name=p.name, title=_title(p), bytes=p.stat().st_size,
                                  modified=pd.Timestamp(p.stat().st_mtime, unit="s").isoformat(), superseded=p.name.startswith("SUPERSEDED")))
    return out


@router.get("/research/reports/{group}/{name}", response_model=ReportBody)
def research_report(group: str, name: str):
    if group not in REPORT_DIRS or not NAME_RE.match(name):
        raise HTTPException(status_code=404, detail="report not found")
    p = (REPORT_DIRS[group] / name).resolve()
    if REPORT_DIRS[group].resolve() not in p.parents or not p.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    return ReportBody(group=group, name=name, markdown=p.read_text(encoding="utf-8", errors="replace"))


# ── decision cards: what / where / how much / till when ─────────────

class DecisionBook(BaseModel):
    cards: list[dict]
    summary: dict
    transparency: dict = {}
    disclaimer: str = DISCLAIMER


@router.get("/decision/cards", response_model=DecisionBook)
def decision_cards(capital: float = Query(1_500_000, ge=50_000, le=1_000_000_000), risk_per_position: float = Query(0.005, ge=0.001, le=0.03),
                   max_position: float = Query(0.08, ge=0.01, le=0.25), max_positions: int = Query(20, ge=1, le=60),
                   horizon: str = Query("position_20d", pattern="^(swing_5d|position_20d)$"), min_bucket: int = Query(4, ge=3, le=5)):
    from qsde.decision.cards import PortfolioParams, build_cards
    from qsde.decision.transparency import explain_exclusions, gather, risk_inputs
    from qsde.ml.registry import get_horizon
    from qsde.ml import data as mldata

    if not SIGNAL.exists():
        raise HTTPException(status_code=404, detail="no defensive signal has been computed yet (scripts/defensive_eval.py --latest)")
    sc = pd.read_parquet(SIGNAL)
    sc["date"] = pd.to_datetime(sc["date"])
    p = PortfolioParams(capital=capital, risk_per_position=risk_per_position, max_position=max_position, max_positions=max_positions,
                        horizon=horizon, min_bucket=min_bucket)
    cand = sc[(sc["risk_bucket"] >= min_bucket) & (sc["liq"] >= p.min_traded_value)]["symbol"].tolist()
    start = str((sc["date"].iloc[0] - pd.Timedelta(days=700)).date())
    px = mldata.load_prices(cand, start=start)[["symbol", "date", "close"]]
    book = build_cards(sc, px, p)
    book["transparency"] = {"risk_inputs": risk_inputs(p, book["summary"], get_horizon(horizon)),
                            "exclusions": explain_exclusions(sc, p, book["summary"].get("skipped_reasons", {})),
                            **gather(sc, str(px["date"].max().date()) if len(px) else None)}
    return DecisionBook(**book)


# ── mutual funds / ETFs (AMFI): a separate asset-class layer ─────────

class FundRow(BaseModel):
    scheme_code: int
    name: str
    amc: Optional[str] = None
    category: Optional[str] = None
    asset_class: Optional[str] = None
    plan: Optional[str] = None
    option_type: Optional[str] = None
    is_etf: bool = False
    is_index: bool = False
    is_liquid: bool = False
    is_direct: Optional[bool] = None
    is_growth: Optional[bool] = None
    last_nav: Optional[float] = None
    last_nav_date: Optional[str] = None
    returns: dict = {}
    return_basis: str = ""


class FundList(BaseModel):
    n_matching: int
    rows: list[FundRow]
    methodology: str
    disclaimer: str = DISCLAIMER


METHOD = ("Cumulative return = NAV(latest) / NAV(on or before latest - horizon) - 1. An annualised figure (CAGR) is shown only from 1Y and is never mixed into the cumulative one. "
          "A horizon is omitted when the scheme has no NAV that far back, or when a single-day NAV move above 50% falls inside it (unit consolidation or bad print: not a return). Growth options: a total return after expenses. IDCW / payout options: a price return only (payouts are not in AMFI's NAV file). "
          "Past NAV performance is not a forecast. Expense ratio, holdings/overlap and AUM are not loaded yet.")


@router.get("/funds/schemes", response_model=FundList)
def fund_schemes(q: Optional[str] = Query(None, max_length=60), asset_class: Optional[str] = Query(None, pattern="^(equity|debt|hybrid|commodity|international|solution|other)$"),
                 plan: Optional[str] = Query(None, pattern="^(direct|regular)$"), growth_only: bool = True, etf: Optional[bool] = None,
                 index_only: bool = False, liquid_only: bool = False, active_only: bool = True, limit: int = Query(40, ge=1, le=100)):
    from qsde.funds.returns import scheme_returns

    where, params = ["TRUE"], {}
    if q:
        where.append("(lower(name) LIKE :q OR lower(amc) LIKE :q)")
        params["q"] = f"%{q.strip().lower()}%"
    if asset_class:
        where.append("asset_class = :ac")
        params["ac"] = asset_class
    if plan:
        where.append("is_direct IS " + ("TRUE" if plan == "direct" else "FALSE"))
    if growth_only:
        where.append("(is_growth IS TRUE OR is_etf)")            # an ETF has no growth/IDCW option: it must not vanish from the default view
    if etf is not None:
        where.append("is_etf IS " + ("TRUE" if etf else "FALSE"))
    if index_only:
        where.append("is_index")
    if liquid_only:
        where.append("is_liquid")
    if active_only:
        where.append("last_nav_date >= (SELECT max(last_nav_date) FROM mf_scheme_master) - 7")
    w = " AND ".join(where)
    n = int(read_sql(f"SELECT count(*) AS n FROM mf_scheme_master WHERE {w}", params=params)["n"].iloc[0])
    sm = read_sql(f"SELECT * FROM mf_scheme_master WHERE {w} ORDER BY name LIMIT :lim", params={**params, "lim": limit})
    rows = []
    if len(sm):
        codes = [int(c) for c in sm["scheme_code"]]
        nav = read_sql("SELECT scheme_code, date, nav FROM mf_nav WHERE scheme_code = ANY(:c) AND date >= (SELECT max(date) FROM mf_nav) - 2200 ORDER BY scheme_code, date", params={"c": codes})
        nav["date"] = pd.to_datetime(nav["date"])
        series = {c: g.set_index("date")["nav"] for c, g in nav.groupby("scheme_code")}
        for r in sm.itertuples():
            s = series.get(int(r.scheme_code))
            ret = scheme_returns(s) if s is not None and len(s) else {}
            rows.append(FundRow(scheme_code=int(r.scheme_code), name=r.name, amc=r.amc, category=(r.sub_category or r.broad_category), asset_class=r.asset_class, plan=r.plan,
                                option_type=r.option_type, is_etf=bool(r.is_etf), is_index=bool(r.is_index), is_liquid=bool(r.is_liquid),
                                is_direct=None if pd.isna(r.is_direct) else bool(r.is_direct), is_growth=None if pd.isna(r.is_growth) else bool(r.is_growth),
                                last_nav=None if s is None or not len(s) else float(s.iloc[-1]), last_nav_date=None if s is None or not len(s) else str(s.index[-1].date()),
                                returns=ret, return_basis=("total return after expenses (growth option)" if r.is_growth else "price return only (payouts not included)" if r.is_growth is False
                                              else "ETF: price return (no growth option; any distributions are not included)" if r.is_etf else "option not identified: treat as a price return only")))
    return FundList(n_matching=n, rows=rows, methodology=METHOD)
