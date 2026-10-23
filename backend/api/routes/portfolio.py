"""Portfolio cockpit: import your holdings (CSV), then see allocation, concentration, P&L, pro-forma drawdown and a benchmark comparison.

Your holdings are your own data: every query is filtered by the signed-in user's id, imports are immutable (a newer import supersedes an older one
for display), viewers cannot import, and nothing here can place or modify an order.
"""
from __future__ import annotations

from typing import Literal, Optional

import pandas as pd
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from api.auth_deps import Principal, require_auth, require_role
from api.routes import cockpit
from qsde.db.connection import get_sync_conn, read_sql
from qsde.modes import policy as mode_policy, profile as mode_profile
from qsde.portfolio import analytics, holdings

router = APIRouter()
BENCHMARKS = {"NIFTYBEES": "Nifty 50 ETF (price return; a tradable proxy for the index)", "JUNIORBEES": "Nifty Next 50 ETF (price return, proxy)",
              "BANKBEES": "Bank Nifty ETF (price return, proxy)"}
DISCLAIMER = ("Research and decision support, not investment advice. P&L is unrealised and uses your file's average cost; history is pro-forma (today's quantities held constant), "
              "not your actual past value.")


Pot = Literal["investment", "trading"]


class ImportIn(BaseModel):
    pot: Pot                      # required: every import belongs to exactly one capital pot; a position never changes pot after this
    csv_text: str = Field(min_length=1, max_length=1_200_000)
    filename: Optional[str] = Field(default=None, max_length=200)
    label: Optional[str] = Field(default=None, max_length=80)


class Issue(BaseModel):
    row: int
    symbol: str
    severity: str
    reason: str


class ImportOut(BaseModel):
    import_id: int
    n_rows: int
    n_accepted: int
    n_rejected: int
    sha256: str
    pot: str
    issues: list[Issue]
    replaced_previous: bool = False


class ImportRow(BaseModel):
    id: int
    filename: Optional[str] = None
    label: Optional[str] = None
    n_accepted: int
    n_rejected: int
    imported_at: str
    pot: Optional[str] = None


class HoldingRow(BaseModel):
    symbol: str
    quantity: float
    avg_cost: float
    last_price: Optional[float] = None
    price_date: Optional[str] = None
    value: Optional[float] = None
    weight: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    stale: bool = False
    defensive_bucket: Optional[int] = None


class PortfolioSummary(BaseModel):
    import_id: int
    pot: Optional[str] = None
    imported_at: str
    as_of: Optional[str]
    totals: dict
    holdings: list[HoldingRow]
    concentration: dict
    defensive: dict
    history: Optional[dict] = None
    benchmark_label: Optional[str] = None
    notes: list[str]
    disclaimer: str = DISCLAIMER


def _known() -> tuple[set, dict, dict]:
    syms = set(read_sql("SELECT DISTINCT symbol FROM ohlcv WHERE source LIKE 'nse_adj_%'")["symbol"])
    m = read_sql("SELECT isin, symbol FROM nse_equity_master WHERE isin IS NOT NULL")
    ren = read_sql("SELECT old_symbol, new_symbol FROM symbol_changes")
    return syms, dict(zip(m["isin"], m["symbol"])), dict(zip(ren["old_symbol"], ren["new_symbol"]))


@router.post("/portfolio/import", response_model=ImportOut)
def import_holdings(body: ImportIn, p: Principal = Depends(require_role("admin", "trader"))):
    syms, by_isin, renamed = _known()
    try:
        parsed = holdings.parse_holdings(body.csv_text, syms, by_isin, renamed)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT count(*) FROM portfolio_imports WHERE user_id = %s AND pot = %s", (p.id, body.pot))
        had = cur.fetchone()["count"] > 0
        cur.execute("INSERT INTO portfolio_imports (user_id, pot, label, filename, sha256, n_rows_file, n_accepted, n_rejected, issues) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (p.id, body.pot, body.label, body.filename, parsed.sha256, parsed.n_rows, len(parsed.holdings), parsed.n_rejected, psycopg2.extras.Json(parsed.issues)))
        imp = cur.fetchone()["id"]
        psycopg2.extras.execute_values(cur, "INSERT INTO portfolio_holdings (import_id, symbol, isin, quantity, avg_cost) VALUES %s",
                                       [(imp, h["symbol"], h["isin"], h["quantity"], h["avg_cost"]) for h in parsed.holdings])
        mode_profile.record(c, p.id, p.role, "portfolio.import", {"import_id": imp, "pot": body.pot, "accepted": len(parsed.holdings), "rejected": parsed.n_rejected, "sha256": parsed.sha256})
        if p.authenticated:
            from qsde.auth import service
            service.audit("portfolio.import", user_id=p.id, email=p.email, entity_type="portfolio_import", entity_id=imp,
                          payload={"accepted": len(parsed.holdings), "rejected": parsed.n_rejected, "sha256": parsed.sha256}, conn=c)
    return ImportOut(import_id=imp, n_rows=parsed.n_rows, n_accepted=len(parsed.holdings), n_rejected=parsed.n_rejected, sha256=parsed.sha256, pot=body.pot,
                     issues=[Issue(**i) for i in parsed.issues], replaced_previous=had)


@router.get("/portfolio/imports", response_model=list[ImportRow])
def list_imports(p: Principal = Depends(require_auth)):
    d = read_sql("SELECT id, filename, label, n_accepted, n_rejected, imported_at, pot FROM portfolio_imports WHERE user_id = :u ORDER BY imported_at DESC LIMIT 50", params={"u": p.id})
    return [ImportRow(id=int(r.id), filename=r.filename, label=r.label, n_accepted=int(r.n_accepted), n_rejected=int(r.n_rejected),
                      imported_at=pd.Timestamp(r.imported_at).isoformat(),
                      pot=None if pd.isna(r.pot) else str(r.pot)) for r in d.itertuples()]


class AssignIn(BaseModel):
    pot: Pot


@router.post("/portfolio/imports/{import_id}/assign-pot")
def assign_pot(import_id: int, body: AssignIn, p: Principal = Depends(require_role("admin", "trader"))):
    """One-time assignment for an import that predates pots. After this the pot is immutable (database trigger); moving a position between
    pots needs an explicit audited transfer, which is not offered."""
    with get_sync_conn() as c:
        cur = c.cursor()
        cur.execute("SELECT pot FROM portfolio_imports WHERE id = %s AND user_id = %s FOR UPDATE", (import_id, p.id))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="import not found")
        if row["pot"] is not None:
            raise HTTPException(status_code=409, detail=f"this import is already in the {row['pot']} pot and cannot be moved")
        cur.execute("UPDATE portfolio_imports SET pot = %s WHERE id = %s", (body.pot, import_id))
        mode_profile.record(c, p.id, p.role, "portfolio.assign_pot", {"import_id": import_id, "pot": body.pot})
    return {"import_id": import_id, "pot": body.pot}


@router.get("/portfolio/summary", response_model=PortfolioSummary)
def portfolio_summary(import_id: Optional[int] = None, pot: Optional[str] = Query(None, pattern="^(investment|trading|unassigned)$"),
                      benchmark: str = Query("NIFTYBEES", pattern="^(NIFTYBEES|JUNIORBEES|BANKBEES|none)$"),
                      lookback_days: int = Query(365, ge=60, le=1825), p: Principal = Depends(require_auth)):
    from qsde.ml import data as mldata

    if import_id is None:
        if pot is None:                       # the lens: the pot that belongs to the current mode
            with get_sync_conn() as c:
                pot = mode_policy.pot_for_mode(mode_profile.load(c, p.id)["mode"])
        cond = "pot IS NULL" if pot == "unassigned" else "pot = :pot"
        imp = read_sql(f"SELECT id, imported_at, pot FROM portfolio_imports WHERE user_id = :u AND {cond} ORDER BY imported_at DESC LIMIT 1",
                       params={"u": p.id, "pot": pot})
        if imp.empty:
            other = read_sql("SELECT pot, count(*) AS n FROM portfolio_imports WHERE user_id = :u GROUP BY pot", params={"u": p.id})
            hint = "; ".join(f"{('unassigned' if pd.isna(r.pot) else r.pot)} pot has {int(r.n)} import(s)" for r in other.itertuples())
            raise HTTPException(status_code=404, detail=f"no holdings imported yet in the {pot} pot" + (f" ({hint})" if hint else ""))
    else:
        imp = read_sql("SELECT id, imported_at, pot FROM portfolio_imports WHERE user_id = :u AND id = :i", params={"u": p.id, "i": import_id})
        if imp.empty:
            raise HTTPException(status_code=404, detail="import not found")
    iid = int(imp["id"].iloc[0])
    h = read_sql("SELECT symbol, quantity::float8 AS quantity, avg_cost::float8 AS avg_cost FROM portfolio_holdings WHERE import_id = :i", params={"i": iid})
    notes: list = []
    start = str((pd.Timestamp.today() - pd.Timedelta(days=lookback_days + 30)).date())
    syms = h["symbol"].tolist() + ([] if benchmark == "none" else [benchmark])
    px = mldata.load_prices(syms, start=start)[["symbol", "date", "close"]]
    if px.empty:
        raise HTTPException(status_code=503, detail="no price history available for these holdings")
    as_of = px["date"].max()
    last = px.sort_values("date").groupby("symbol").tail(1)[["symbol", "date", "close"]]
    df = analytics.value_holdings(h, last[last["symbol"].isin(h["symbol"])], as_of)
    scores = pd.read_parquet(cockpit.SIGNAL) if cockpit.SIGNAL.exists() else None
    prof = analytics.defensive_profile(df, scores)
    bucket = dict(zip(scores["symbol"], scores["risk_bucket"])) if scores is not None else {}
    wide = px[px["symbol"].isin(h["symbol"])].pivot(index="date", columns="symbol", values="close")
    bench = None
    if benchmark != "none":
        b = px[px["symbol"] == benchmark]
        bench = b.set_index("date")["close"] if len(b) else None
        if bench is None:
            notes.append(f"benchmark {benchmark} has no price history")
    hist = analytics.history(h, wide, bench, lookback_days)
    if hist is None:
        notes.append("not enough overlapping price history to build the pro-forma history")
    elif hist["names_missing"]:
        notes.append("no price history for: " + ", ".join(hist["names_missing"]))
    tot = analytics.summarise(df)
    if tot["n_stale"]:
        notes.append(f"{tot['n_stale']} holding(s) have a last price older than {analytics.STALE_DAYS} days")
    if prof.get("available") and prof["scored_share"] < 0.5:
        notes.append("less than half of the portfolio is inside the defensive-signal universe (top-500 by traded value)")
    rows = [HoldingRow(symbol=r.symbol, quantity=r.quantity, avg_cost=r.avg_cost,
                       last_price=None if pd.isna(r.last_price) else float(r.last_price), price_date=None if pd.isna(r.price_date) else str(pd.Timestamp(r.price_date).date()),
                       value=None if pd.isna(r.value) else float(r.value), weight=None if pd.isna(r.weight) else float(r.weight),
                       pnl=None if pd.isna(r.pnl) else float(r.pnl), pnl_pct=None if pd.isna(r.pnl_pct) else float(r.pnl_pct), stale=bool(r.stale),
                       defensive_bucket=int(bucket[r.symbol]) if r.symbol in bucket else None) for r in df.itertuples()]
    return PortfolioSummary(import_id=iid, pot=None if pd.isna(imp["pot"].iloc[0]) else str(imp["pot"].iloc[0]), imported_at=pd.Timestamp(imp["imported_at"].iloc[0]).isoformat(), as_of=str(pd.Timestamp(as_of).date()), totals=tot, holdings=rows,
                            concentration=analytics.concentration(df.set_index("symbol")["weight"]), defensive=prof, history=hist,
                            benchmark_label=BENCHMARKS.get(benchmark), notes=notes)
