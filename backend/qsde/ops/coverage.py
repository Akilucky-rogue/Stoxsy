"""The data-coverage matrix: for every dataset the platform depends on, what is loaded, from when to when, how stale, and what is missing.

Built from the database itself (no cached claims), so "do we really have data from 2000?" is answered with dates. Row counts for very large tables are planner estimates and are
marked with a tilde; first and last dates are exact (index lookups). Pure formatting helpers are separate from the database query so they can be tested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pandas as pd

# (asset class, dataset, table, date expression, filter, expected cadence, what it unlocks / the known gap)
DATASETS = [
    ("Equity, cash", "NSE end-of-day prices", "eod_raw", "date", "exchange = 'NSE'", "daily", "equity research, signals, cards"),
    ("Equity, cash", "BSE end-of-day prices (raw)", "bse_eod_raw", "date", None, "daily", "BSE-only names; adjusted series not validated"),
    ("Equity, cash", "Official adjusted series (ohlcv)", "ohlcv", "date", None, "daily", "what every consumer reads; 69 Bucket A events adjudicated, rebuild pending"),
    ("Equity, cash", "NSE corporate actions", "nse_corporate_actions", "ex_date", None, "event", "price adjustment evidence"),
    ("Equity, cash", "BSE corporate actions", "bse_corporate_actions", "ex_date", None, "event", "evidence only; no BSE adjusted series yet"),
    ("Equity, cash", "Bulk and block deals", "deals_raw", "trade_date", None, "daily", "rolling NSE files: history starts when we began landing them"),
    ("Equity, cash", "Institutional flows (FII/DII)", "institutional_flows", "date", None, "daily", "flow and regime features"),
    ("Equity, cash", "Intraday minute bars (streamed)", "ohlcv_intraday", "ts", None, "session", "live view; reconciled to Kite history after the close"),
    ("Equity, cash", "Intraday minute bars (Kite official)", "ohlcv_intraday_official", "ts", None, "session", "reference for the reconcile"),
    ("Futures and options", "NSE F&O end-of-day archive", "fno_eod_raw", "trade_date", None, "daily", "full chain per session; implied volatility not published (derive)"),
    ("Futures and options", "Kite instrument master", "kite_instruments", "refreshed_at", None, "daily", "lot sizes, expiries, live subscription"),
    ("Mutual funds", "Scheme NAVs", "mf_nav", "date", None, "daily", "returns, risk, peer context"),
    ("Mutual funds", "Scheme master and classification", "mf_scheme_master", "updated_at", None, "daily", "like-for-like grouping"),
    ("Mutual funds", "Fund snapshot (returns, risk, quality)", "mf_fund_snapshot", "as_of", None, "daily", "funds page"),
    ("Mutual funds", "Expense ratios (TER)", "mf_expense_ratio", "as_of", None, "daily", "NOT BUILT: cost-aware fund ranking"),
    ("Mutual funds", "Portfolio holdings", "mf_holdings", "as_of", None, "monthly", "NOT BUILT: overlap and look-through allocation"),
    ("Fundamentals", "Company financials (filings)", "fundamentals", "fiscal_date", None, "quarterly", "table exists, no rows: value / quality / growth features"),
    ("News and events", "Announcements and news", "news_sentiment", "published_at", None, "daily", "table exists, no rows"),
    ("Macro", "Macro series", "macro", "date", None, "daily", "table exists, no rows: regimes, real returns"),
]


@dataclass
class Row:
    asset_class: str
    dataset: str
    table: str
    status: str                       # loaded | empty | not_built
    first: Optional[date] = None
    last: Optional[date] = None
    rows: Optional[int] = None
    approx: bool = False
    age_days: Optional[int] = None
    note: str = ""
    cadence: str = ""
    sessions: Optional[int] = None


def _exists(read_sql, table: str) -> bool:
    return not read_sql("SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = :t", params={"t": table}).empty


def gather(read_sql=None, today: Optional[date] = None) -> list:
    from qsde.db.connection import read_sql as _rs

    rs = read_sql or _rs
    today = today or date.today()
    out = []
    for ac, name, table, col, flt, cadence, note in DATASETS:
        if not _exists(rs, table):
            out.append(Row(ac, name, table, "not_built", note=note, cadence=cadence))
            continue
        where = f"WHERE {flt}" if flt else ""
        est = int(rs("SELECT COALESCE(reltuples, 0)::bigint AS n FROM pg_class WHERE relname = :t", params={"t": table})["n"].iloc[0])
        mm = rs(f"SELECT min({col}) AS a, max({col}) AS b FROM {table} {where}").iloc[0]
        if pd.isna(mm["b"]):
            out.append(Row(ac, name, table, "empty", rows=0, note=note, cadence=cadence))
            continue
        approx = est > 1_000_000
        n = est if approx else int(rs(f"SELECT count(*) AS n FROM {table} {where}")["n"].iloc[0])
        a, b = pd.Timestamp(mm["a"]), pd.Timestamp(mm["b"])
        if a.tzinfo is not None:
            a, b = a.tz_convert("Asia/Kolkata"), b.tz_convert("Asia/Kolkata")
        sessions = None
        if cadence in ("daily", "session"):
            sessions = int(rs(f"SELECT count(DISTINCT ({col})::date) AS n FROM {table} {where}")["n"].iloc[0]) if not approx else None
        out.append(Row(ac, name, table, "loaded", a.date(), b.date(), n, approx, (today - b.date()).days, note, cadence, sessions))
    return out


def years(first: date, last: date) -> float:
    return round((last - first).days / 365.25, 1)


def table_lines(rows: list) -> list:
    hdr = ["Asset class", "Dataset", "Status", "From", "To", "Years", "Rows", "Age (d)", "Note"]
    lines = [hdr]
    for r in rows:
        lines.append([r.asset_class, r.dataset, r.status.replace("_", " "), str(r.first or "-"), str(r.last or "-"), "-" if r.first is None else str(years(r.first, r.last)),
                      "-" if r.rows is None else (f"~{r.rows:,}" if r.approx else f"{r.rows:,}"), "-" if r.age_days is None else str(r.age_days), r.note])
    return lines


def render_text(rows: list) -> str:
    lines = table_lines(rows)
    w = [max(len(str(l[i])) for l in lines) for i in range(len(lines[0]) - 1)]
    out = []
    for k, l in enumerate(lines):
        out.append("  ".join(str(l[i]).ljust(w[i]) for i in range(len(w))) + "  " + str(l[-1]))
        if k == 0:
            out.append("  ".join("-" * x for x in w))
    return "\n".join(out)


def render_markdown(rows: list, today: date) -> str:
    lines = table_lines(rows)
    md = [f"# Data coverage, {today}", "", "| " + " | ".join(lines[0]) + " |", "|" + "---|" * len(lines[0])]
    md += ["| " + " | ".join(l) + " |" for l in lines[1:]]
    md += ["", "Row counts with a tilde are planner estimates for very large tables; first and last dates are exact. A dataset marked 'not built' has no table yet; 'empty' has a table and no rows."]
    return "\n".join(md)


def render_html(rows: list, today: date, axis_from: int = 2000) -> str:
    """Standalone page: one bar per dataset from its first to its last date on a common axis from `axis_from`, so a gap to year 2000 is visible at a glance."""
    import json

    y0, y1 = axis_from, today.year + 1
    data = [{"ac": r.asset_class, "name": r.dataset, "st": r.status, "a": None if r.first is None else r.first.year + (r.first.timetuple().tm_yday - 1) / 366,
             "b": None if r.last is None else r.last.year + (r.last.timetuple().tm_yday - 1) / 366, "rows": r.rows, "approx": r.approx, "age": r.age_days, "note": r.note, "first": str(r.first or ""), "last": str(r.last or "")} for r in rows]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Data coverage</title>
<style>:root{{--bg:#f6f8fb;--card:#fff;--ink:#0f1b2d;--muted:#5b6b82;--line:#dbe3ee;--ok:#0b8a5f;--bad:#c0392b;--warn:#b7791f;--acc:#3b5bdb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1421;--card:#141e30;--ink:#e8eef8;--muted:#93a4bd;--line:#26344d;--ok:#2fd4a0;--bad:#ff7a6b;--warn:#f6c453;--acc:#7c93ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif}}main{{max-width:1080px;margin:0 auto;padding:16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}}
.muted{{color:var(--muted)}}svg{{width:100%;height:auto}}.bar{{transition:width 1.1s ease}}@media (prefers-reduced-motion:reduce){{.bar{{transition:none}}}}</style></head><body><main>
<h1>What data we actually hold, {today}</h1><p class="muted">Each bar runs from the first to the last date in the database. The axis starts in {y0}: an empty left side means no data from before the bar starts. Red = nothing loaded.</p>
<div class="card"><svg id="c" role="img" aria-label="Coverage of each dataset by date range"></svg></div>
<div class="card"><table id="t" style="width:100%;border-collapse:collapse;font-size:.88rem"></table></div>
<p class="muted">Research and decision support, not investment advice. Row counts with ~ are estimates.</p>
<script>const D={json.dumps(data)},Y0={y0},Y1={y1};const rowH=26,L=250,R=20,W=1000,H=D.length*rowH+40;const X=y=>L+(y-Y0)/(Y1-Y0)*(W-L-R);let s='';
for(let y=Y0;y<=Y1;y+=5){{s+='<line x1="'+X(y)+'" x2="'+X(y)+'" y1="14" y2="'+(H-22)+'" stroke="currentColor" opacity=".12"/><text x="'+X(y)+'" y="'+(H-6)+'" text-anchor="middle" font-size="11" fill="currentColor">'+y+'</text>';}}
let last='';D.forEach((d,i)=>{{const y=22+i*rowH;if(d.ac!==last){{last=d.ac}}
s+='<text x="'+(L-8)+'" y="'+(y+13)+'" text-anchor="end" font-size="12" fill="currentColor">'+d.name+'</text>';
if(d.st==='loaded'){{const x0=X(d.a),w=Math.max(3,X(d.b)-X(d.a));const stale=d.age>5;s+='<rect class="bar" x="'+x0+'" y="'+y+'" width="'+w+'" height="16" rx="4" fill="'+(stale?'var(--warn)':'var(--ok)')+'"><animate attributeName="width" from="0" to="'+w+'" dur="1s"/><title>'+d.first+' to '+d.last+'</title></rect>';}}
else{{s+='<rect x="'+L+'" y="'+y+'" width="90" height="16" rx="4" fill="var(--bad)" opacity=".75"/><text x="'+(L+98)+'" y="'+(y+12)+'" font-size="11" fill="currentColor">'+(d.st==='empty'?'table exists, no rows':'not built')+'</text>';}}}});
document.getElementById('c').setAttribute('viewBox','0 0 '+W+' '+H);document.getElementById('c').innerHTML=s;
document.getElementById('t').innerHTML='<tr><th align=left>Dataset</th><th align=left>From</th><th align=left>To</th><th align=right>Rows</th><th align=left>Note</th></tr>'+D.map(d=>'<tr style="border-top:1px solid var(--line)"><td>'+d.name+'</td><td>'+d.first+'</td><td>'+d.last+'</td><td align=right>'+(d.rows==null?'':(d.approx?'~':'')+d.rows.toLocaleString())+'</td><td class=muted>'+d.note+'</td></tr>').join('');</script></main></body></html>"""
