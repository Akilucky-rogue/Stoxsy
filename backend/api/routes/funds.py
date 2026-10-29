"""Mutual funds & ETFs (AMFI): a separate asset-class layer. Nothing here feeds the equity universe, the defensive stock signal or the decision cards.

Reads the scheme master and the derived snapshot (returns with explicit statuses, risk, data-quality badge, peer context). Every return carries its basis label;
a missing figure says why (insufficient history, stale start NAV, NAV discontinuity) instead of being extrapolated or silently dropped.
Expense ratio, holdings / overlap and AUM are not loaded and are not shown.
"""
from __future__ import annotations

import json
from typing import Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from qsde.db.connection import read_sql
from qsde.funds.snapshot import BASIS_TEXT

router = APIRouter()
DISCLAIMER = ("Research and decision support, not investment advice. Past NAV performance is not a forecast. NAV is raw as published by AMFI; "
              "expense ratio, holdings / overlap and AUM are not loaded yet.")
METHOD = ("Cumulative return = NAV(latest) / NAV(on or before latest - horizon) - 1. An annualised figure (CAGR) is shown only from 1Y and is never mixed into the cumulative one. "
          "A horizon is not shown when the scheme has no NAV that far back, when its start NAV is more than 7 days stale, or when a single-day NAV move above 50% falls inside it "
          "(unit consolidation or bad print: not a return); the reason is given instead. Volatility, drawdown and downside deviation use ~3 years of daily NAV changes. "
          "Growth options: total return after expenses. IDCW / payout lines and ETFs: price return only (payouts are not in the NAV file). "
          "Peer context compares only like with like (same asset class, category and plan; growth lines, or ETFs of one asset class), needs 8+ peers, and excludes schemes with a poor data-quality badge.")
SORTS = {"name": "m.name", "ret_1y": "s.ret_1y", "ret_3y": "s.ret_3y", "ret_5y": "s.ret_5y", "vol_3y": "s.vol_3y", "maxdd_3y": "s.maxdd_3y"}


class FundRow(BaseModel):
    scheme_code: int
    name: str
    amc: Optional[str] = None
    category: Optional[str] = None
    asset_class: Optional[str] = None
    fund_group: Optional[str] = None
    plan: Optional[str] = None
    option_type: Optional[str] = None
    is_etf: bool = False
    is_index: bool = False
    is_direct: Optional[bool] = None
    is_growth: Optional[bool] = None
    lifecycle: Optional[str] = None
    last_nav: Optional[float] = None
    last_nav_date: Optional[str] = None
    basis: Optional[str] = None
    basis_text: Optional[str] = None
    returns: dict = {}
    risk: dict = {}
    quality: Optional[dict] = None
    peers: Optional[dict] = None
    snapshot_as_of: Optional[str] = None


class FundList(BaseModel):
    n_matching: int
    offset: int
    rows: list[FundRow]
    methodology: str = METHOD
    disclaimer: str = DISCLAIMER


class Facets(BaseModel):
    asset_classes: list[dict]
    fund_groups: list[dict]
    amcs: list[dict]
    categories: list[dict]


def _j(x):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return json.loads(x) if isinstance(x, str) else x


def _row(r, nav: Optional[dict] = None) -> FundRow:
    snap = pd.notna(getattr(r, "basis", None))
    code = int(r.scheme_code)
    n = (nav or {}).get(code)
    return FundRow(scheme_code=code, name=r.name, amc=r.amc, category=(r.sub_category or r.broad_category), asset_class=r.asset_class, fund_group=r.fund_group, plan=r.plan,
                   option_type=r.option_type, is_etf=bool(r.is_etf), is_index=bool(r.is_index), is_direct=None if pd.isna(r.is_direct) else bool(r.is_direct),
                   is_growth=None if pd.isna(r.is_growth) else bool(r.is_growth), lifecycle=r.lifecycle, last_nav=None if n is None else n[1],
                   last_nav_date=None if n is None else str(n[0]), basis=r.basis if snap else None, basis_text=BASIS_TEXT.get(r.basis) if snap else None,
                   returns=_j(r.returns) or {} if snap else {}, risk=_j(r.risk) or {} if snap else {}, quality=_j(r.quality) if snap else None,
                   peers=_j(r.peers) if snap else None, snapshot_as_of=str(pd.Timestamp(r.as_of).date()) if snap else None)


def _last_navs(codes: list) -> dict:
    if not codes:
        return {}
    d = read_sql("SELECT DISTINCT ON (scheme_code) scheme_code, date, nav FROM mf_nav WHERE scheme_code = ANY(:c) AND date >= (SELECT max(date) FROM mf_nav) - 400 "
                 "ORDER BY scheme_code, date DESC", params={"c": codes})
    return {int(r.scheme_code): (pd.Timestamp(r.date).date(), float(r.nav)) for r in d.itertuples()}


@router.get("/funds/facets", response_model=Facets)
def facets():
    base = "FROM mf_scheme_master m JOIN mf_fund_snapshot s USING (scheme_code)"
    ac = read_sql(f"SELECT m.asset_class AS name, count(*) AS n {base} GROUP BY 1 ORDER BY 2 DESC")
    fg = read_sql(f"SELECT m.fund_group AS name, count(*) AS n {base} GROUP BY 1 ORDER BY 2 DESC")
    am = read_sql(f"SELECT m.amc AS name, count(*) AS n {base} WHERE m.amc IS NOT NULL GROUP BY 1 ORDER BY 1")
    ca = read_sql(f"SELECT m.asset_class, COALESCE(m.sub_category, m.broad_category) AS name, count(*) AS n {base} WHERE COALESCE(m.sub_category, m.broad_category) IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 3 DESC")
    rec = lambda d: json.loads(d.to_json(orient="records"))      # noqa: E731
    return Facets(asset_classes=rec(ac), fund_groups=rec(fg), amcs=rec(am), categories=rec(ca))


@router.get("/funds/schemes", response_model=FundList)
def fund_schemes(q: Optional[str] = Query(None, max_length=60), asset_class: Optional[str] = Query(None, pattern="^(equity|debt|hybrid|commodity|international|solution|other)$"),
                 amc: Optional[str] = Query(None, max_length=120), fund_group: Optional[str] = Query(None, max_length=40), category: Optional[str] = Query(None, max_length=160),
                 plan: Optional[str] = Query(None, pattern="^(direct|regular)$"), option: str = Query("growth", pattern="^(growth|idcw|etf|any)$"),
                 status: str = Query("active", pattern="^(active|all)$"), quality: Optional[str] = Query(None, pattern="^(ok|caution|poor)$"),
                 sort: str = Query("name", pattern="^(name|ret_1y|ret_3y|ret_5y|vol_3y|maxdd_3y)$"), order: str = Query("asc", pattern="^(asc|desc)$"),
                 limit: int = Query(40, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)):
    where, params = ["TRUE"], {}
    if q:
        where.append("(lower(m.name) LIKE :q OR lower(m.amc) LIKE :q)")
        params["q"] = f"%{q.strip().lower()}%"
    for col, val, key in (("m.asset_class", asset_class, "ac"), ("m.amc", amc, "amc"), ("m.fund_group", fund_group, "fg"), ("COALESCE(m.sub_category, m.broad_category)", category, "cat")):
        if val:
            where.append(f"{col} = :{key}")
            params[key] = val
    if plan:
        where.append("m.is_direct IS " + ("TRUE" if plan == "direct" else "FALSE"))
    if option == "growth":
        where.append("m.is_growth IS TRUE")
    elif option == "idcw":
        where.append("m.is_growth IS FALSE")
    elif option == "etf":
        where.append("m.is_etf")
    if status == "active":
        where.append("m.lifecycle IN ('active', 'recent_launch')")
    if quality:
        where.append("s.quality_level = :ql")
        params["ql"] = quality
    w = " AND ".join(where)
    frm = "FROM mf_scheme_master m LEFT JOIN mf_fund_snapshot s USING (scheme_code)"
    n = int(read_sql(f"SELECT count(*) AS n {frm} WHERE {w}", params=params)["n"].iloc[0])
    nulls = "NULLS LAST"
    d = read_sql(f"SELECT m.*, s.as_of, s.basis, s.returns, s.risk, s.quality, s.peers FROM mf_scheme_master m LEFT JOIN mf_fund_snapshot s USING (scheme_code) "
                 f"WHERE {w} ORDER BY {SORTS[sort]} {order} {nulls}, m.name, m.scheme_code LIMIT :lim OFFSET :off", params={**params, "lim": limit, "off": offset})
    nav = _last_navs([int(c) for c in d["scheme_code"]])
    return FundList(n_matching=n, offset=offset, rows=[_row(r, nav) for r in d.itertuples()])


class Series(BaseModel):
    dates: list[str]
    value: list[float]
    drawdown: list[float]


class FundDetail(BaseModel):
    fund: FundRow
    window: str
    series: Optional[Series] = None
    benchmark: Optional[dict] = None
    events: dict
    methodology: str = METHOD
    disclaimer: str = DISCLAIMER


WINDOWS = {"1y": 365, "3y": 365 * 3, "5y": 365 * 5, "max": 100000}


@router.get("/funds/schemes/{scheme_code}", response_model=FundDetail)
def fund_detail(scheme_code: int, window: str = Query("3y", pattern="^(1y|3y|5y|max)$")):
    d = read_sql("SELECT m.*, s.as_of, s.basis, s.returns, s.risk, s.quality, s.peers FROM mf_scheme_master m LEFT JOIN mf_fund_snapshot s USING (scheme_code) WHERE m.scheme_code = :c",
                 params={"c": scheme_code})
    if d.empty:
        raise HTTPException(status_code=404, detail="scheme not found")
    nav = read_sql("SELECT date, nav FROM mf_nav WHERE scheme_code = :c ORDER BY date", params={"c": scheme_code})
    nav["date"] = pd.to_datetime(nav["date"])
    s = nav.set_index("date")["nav"].astype(float)
    row = _row(d.iloc[0:1].itertuples().__next__(), {scheme_code: (s.index[-1].date(), float(s.iloc[-1]))} if len(s) else None)
    r = s.pct_change().dropna()
    jumps = r[r.abs() > 0.5]
    gp = s.index.to_series().diff().dt.days
    gaps = gp[gp > 30]
    events = {"unit_changes": [{"date": str(i.date()), "move": float(v)} for i, v in jumps.items()][-10:],
              "gaps_over_30d": [{"from": str((i - pd.Timedelta(days=int(v))).date()), "to": str(i.date()), "days": int(v)} for i, v in gaps.items()][-10:]}
    series, bench = None, None
    if len(s) >= 2:
        w = s[s.index >= s.index[-1] - pd.Timedelta(days=WINDOWS[window])]
        # a unit change inside the window makes a rebased line meaningless: start the line after the last one
        wj = w.pct_change().abs()
        if (wj > 0.5).any():
            w = w[w.index >= wj[wj > 0.5].index[-1]]
        if len(w) >= 2:
            if len(w) > 500:
                w = w.resample("W").last().dropna() if len(w) > 1500 else w.iloc[::-(-len(w) // 500)]
                if w.index[-1] != s.index[-1]:
                    w = pd.concat([w, s.iloc[[-1]]])
            eq = w / w.iloc[0] * 100.0
            series = Series(dates=[str(x.date()) for x in eq.index], value=[float(x) for x in eq.values], drawdown=[float(x) for x in (eq / eq.cummax() - 1.0).values])
            if row.asset_class in ("equity", "international") and row.basis != "price_return_only":
                from qsde.ml import data as mldata
                bx = mldata.load_prices(["NIFTYBEES"], start=str(w.index[0].date()))
                if len(bx):
                    from qsde.funds.returns import trim_after_unit_change
                    bser, cut = trim_after_unit_change(bx.set_index("date")["close"].astype(float))        # the ETF had an unadjusted unit change in 2019
                    b = bser.reindex(eq.index, method="ffill").dropna()
                    if len(b) >= 2:
                        bench = {"label": "Nifty 50 ETF (NIFTYBEES) price return: a tradable proxy for the index, shown for context only" + (f"; shown from {b.index[0].date()} because of a unit change in the ETF's price on {cut.date()}" if cut is not None else ""),
                                 "dates": [str(x.date()) for x in b.index], "value": [float(x) for x in (b / b.iloc[0] * 100.0).values]}
    return FundDetail(fund=row, window=window, series=series, benchmark=bench, events=events)
