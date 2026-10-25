"""Dry run: what the single `ohlcv` rebuild would change once all 69 Bucket A events are adjudicated, under the owner's Option B. NOTHING is written to the database or to any price series.

    python scripts/ca_bucket_a_rebuild_preview.py

For each event: the overnight gap (ex-date open against the previous close, the packet's basis) in today's `ohlcv` (listed factor applied or not, as recorded in corp_action_events) versus the event-day return after the rebuild
(listed factor kept for confirmed events and, under the owner's Option B, for held events flagged unreviewed; rejected events lose it and show the raw corporate-action cliff).
Outputs (data/reports/corp_actions/): rebuild_preview_bucket_a.{csv,md} and visual/rebuild_preview_bucket_a.html (standalone, animated before/after).
"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path

import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")
CLIFF = 0.20


def compute() -> pd.DataFrame:
    from qsde.db.connection import read_sql
    from qsde.ingestion import bucket_a_decisions as D, nse_eod

    cfg = D.load()
    keep_held = cfg.get("rebuild_policy", {}).get("held") == "keep_listed_factor_flagged_unreviewed"      # owner's Option B (2026-10-23)
    dec = pd.DataFrame(cfg["decisions"])
    syms = sorted(dec["symbol"].unique())
    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE' AND symbol = ANY(:s)", params={"s": syms})
    raw["date"] = pd.to_datetime(raw["date"])
    px = nse_eod.pick_primary_series(raw)[["symbol", "date", "open", "close"]].sort_values(["symbol", "date"])
    ev = read_sql("SELECT symbol, ex_date, price_factor, applied FROM corp_action_events WHERE symbol = ANY(:s) AND kind = 'SPLIT_BONUS'", params={"s": syms})
    ev["ex_date"] = pd.to_datetime(ev["ex_date"]).dt.date.astype(str)
    rows = []
    for r in dec.itertuples():
        q = px[px["symbol"] == r.symbol].set_index("date")
        p = q["close"]
        d0 = pd.Timestamp(r.ex_date)
        if d0 not in p.index or p.index.get_loc(d0) == 0:
            rows.append({"symbol": r.symbol, "ex_date": r.ex_date, "decision": r.decision, "note": "no bar on the ex-date"})
            continue
        i = p.index.get_loc(d0)
        raw_ratio = float(q["open"].iloc[i] / p.iloc[i - 1])          # overnight gap: ex-date open vs previous close (the packet's residual basis)
        e = ev[(ev["symbol"] == r.symbol) & (ev["ex_date"] == r.ex_date)]
        f = float(e["price_factor"].iloc[0]) if len(e) else np.nan
        applied_now = bool(e["applied"].iloc[0]) if len(e) else False
        ret_now = raw_ratio / f - 1 if applied_now else raw_ratio - 1
        adj_after = r.decision == "confirm" or (keep_held and r.decision == "needs_more_evidence")
        ret_after = raw_ratio / f - 1 if adj_after else raw_ratio - 1
        rows.append({"symbol": r.symbol, "ex_date": r.ex_date, "decision": r.decision, "listed_factor": f, "raw_ratio": raw_ratio, "applied_now": applied_now,
                     "overnight_gap_return_now": ret_now, "overnight_gap_return_after": ret_after, "changes": applied_now != adj_after, "note": "", "flag": "applied, unreviewed" if (adj_after and r.decision != "confirm") else ""})
    out = pd.DataFrame(rows)
    out["cliff_after"] = out["overnight_gap_return_after"].abs() > CLIFF
    return out


def table(t: pd.DataFrame) -> str:
    pc = lambda x: "n/a" if pd.isna(x) else f"{x:+.1%}"      # noqa: E731
    lines = ["| Symbol | Ex-date | Decision | Listed factor | In ohlcv today | Overnight gap today | After rebuild |", "|---|---|---|---|---|---|---|"]
    for r in t.itertuples():
        lines.append(f"| {r.symbol} | {r.ex_date} | {r.decision} | {r.listed_factor:.4f} | {'adjusted' if r.applied_now else 'not adjusted'} | {pc(r.overnight_gap_return_now)} | {pc(r.overnight_gap_return_after)} |")
    return "\n".join(lines)


def markdown(t: pd.DataFrame) -> str:
    ch = t[t["changes"].fillna(False)]
    held = t[t["decision"] == "needs_more_evidence"]
    rej = t[t["decision"] == "reject"]
    err = held["overnight_gap_return_after"].abs()
    return f"""# Bucket A: what the single `ohlcv` rebuild would change (DRY RUN, nothing applied)

Owner policy (Option B, 2026-10-23): confirmed events keep the listed factor; HELD events keep it too, flagged "applied, unreviewed"; only REJECTED events lose their adjustment.
All 69 are adjudicated: {int((t.decision == 'confirm').sum())} confirm, {len(held)} hold, {len(rej)} reject. Today every one of these listed factors is applied in `ohlcv`.

* Treatment changes: **{len(ch)}** events (the rejected ones). Unchanged: {len(t) - len(ch)}.
* Held events keep a residual overnight gap of median **{err.median():.1%}** (max {err.max():.1%}) instead of a fake cliff; it is the price the listed factor may be off by, and each stays flagged for review.
* Rejected events would show the raw corporate-action drop: {', '.join(f'{r.symbol} {r.ex_date} ({r.overnight_gap_return_after:+.0%})' for r in rej.itertuples())}.
* Prior series is retained; the rebuild is versioned and needs your approval of this report.

## Rejected events (adjustment removed)

{table(rej)}

## Held events (listed factor kept, flagged unreviewed)

{table(held)}

## Confirmed events (unchanged)

{table(t[t['decision'] == 'confirm'])}
"""


def html_page(t: pd.DataFrame) -> str:
    ch = t[t["decision"] != "confirm"].sort_values("overnight_gap_return_after").reset_index(drop=True)
    data = json.dumps([{"s": r.symbol, "d": r.ex_date, "dec": r.decision, "now": round(r.overnight_gap_return_now, 4), "after": round(r.overnight_gap_return_after, 4)} for r in ch.itertuples()])
    n_conf = int((t.decision == "confirm").sum())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bucket A rebuild preview</title>
<style>
:root{{--bg:#f6f8fb;--card:#fff;--ink:#0f1b2d;--muted:#5b6b82;--line:#dbe3ee;--up:#0b8a5f;--down:#c0392b;--acc:#3b5bdb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1421;--card:#141e30;--ink:#e8eef8;--muted:#93a4bd;--line:#26344d;--up:#2fd4a0;--down:#ff7a6b;--acc:#7c93ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif}}
main{{max-width:980px;margin:0 auto;padding:16px}} h1{{margin:.2em 0}} .card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}}
.muted{{color:var(--muted)}} button{{padding:8px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);cursor:pointer;font-weight:600}}
button[aria-pressed=true]{{background:var(--acc);color:#fff;border-color:transparent}} svg{{width:100%;height:auto}} .bar{{transition:width .9s ease}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}} .tile{{border:1px solid var(--line);border-radius:12px;padding:12px}} .tile b{{font-size:1.5rem;display:block}}
</style></head><body><main>
<h1>What the single rebuild would change</h1>
<p class="muted">DRY RUN: nothing has been changed. Owner policy (Option B): held events keep the listed factor, flagged unreviewed; rejected events lose it. Each bar is the overnight price move into the ex-date (open vs previous close). A move near -50% on a bonus is not a crash, it is the share count doubling: it looks like a cliff unless the adjustment is applied.</p>
<div class="tiles"><div class="tile"><b>{n_conf}</b>confirmed: factor stays applied</div><div class="tile"><b>{int((t.decision == 'needs_more_evidence').sum())}</b>held: factor kept, flagged unreviewed</div>
<div class="tile"><b>{int((t.decision == 'reject').sum())}</b>rejected: adjustment removed</div></div>
<div class="card"><div role="group" aria-label="View"><button id="b1" aria-pressed="true">ohlcv today</button> <button id="b2" aria-pressed="false">After the rebuild</button> <button id="b3" aria-pressed="false">Play</button></div>
<svg id="svg" role="img" aria-label="Event-day return of each held or rejected event (overnight gap), in today's series and after the rebuild under Option B"></svg>
<p class="muted" id="cap"></p></div>
<p class="muted">Held means undecided, not wrong: its price may be off by up to about 10% until reviewed. Research crossing a rejected event must exclude it. The prior series is kept and the rebuild is versioned. Scenario from a script reading the local price archive; research only, not investment advice.</p>
<script>
const D={data};const W=900,rowH=18,L=130,R=60,H=D.length*rowH+40;const svg=document.getElementById('svg');svg.setAttribute('viewBox','0 0 '+W+' '+H);

const sc=v=>(Math.min(Math.abs(v),1))*(W-L-R);let mode='now';
function draw(){{let s='';s+='<line x1="'+L+'" x2="'+L+'" y1="10" y2="'+(H-20)+'" stroke="currentColor" opacity=".4"/>';
D.forEach((x,i)=>{{const y=20+i*rowH,v=mode==='now'?x.now:x.after,w=sc(v);const col=Math.abs(v)>0.2?'var(--down)':'var(--acc)';
s+='<text x="'+(L-6)+'" y="'+(y+11)+'" text-anchor="end" font-size="11" fill="currentColor">'+x.s+' '+x.d.slice(0,4)+' ('+(x.dec==='reject'?'rejected':'held')+')</text>';
s+='<rect class="bar" x="'+L+'" y="'+y+'" width="'+w+'" height="12" rx="3" fill="'+col+'"/>';s+='<text x="'+(L+w+4)+'" y="'+(y+11)+'" font-size="11" fill="currentColor">'+(v*100).toFixed(0)+'%</text>';}});
svg.innerHTML=s;document.getElementById('cap').textContent=mode==='now'?'Today: the listed factor is applied, so these moves look like ordinary days.':'After the rebuild (Option B): held events keep their factor and show only a small residual move (flagged for review); rejected events are not adjusted, so the corporate action shows as a price drop.';
['b1','b2'].forEach((id,k)=>document.getElementById(id).setAttribute('aria-pressed',String((k===0)===(mode==='now'))));}}
document.getElementById('b1').onclick=()=>{{mode='now';draw()}};document.getElementById('b2').onclick=()=>{{mode='after';draw()}};
document.getElementById('b3').onclick=()=>{{mode='now';draw();setTimeout(()=>{{mode='after';draw()}},1200)}};draw();
</script></main></body></html>"""


def main() -> int:
    t = compute()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "visual").mkdir(exist_ok=True)
    t.to_csv(OUT / "rebuild_preview_bucket_a.csv", index=False)
    (OUT / "rebuild_preview_bucket_a.md").write_text(markdown(t), encoding="utf-8")
    (OUT / "visual" / "rebuild_preview_bucket_a.html").write_text(html_page(t), encoding="utf-8")
    ch = t[t["changes"].fillna(False)]
    print(f"events {len(t)}; treatment changes for {len(ch)}; cliffs over {CLIFF:.0%} after rebuild: {int(t['cliff_after'].sum())}; no-bar notes: {int((t['note'] != '').sum())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
