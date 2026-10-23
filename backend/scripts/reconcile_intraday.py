"""After-close: reconcile the day's streamed minute bars against Kite's historical minute bars, store the official bars, and write a visual report.

    python scripts/reconcile_intraday.py                    # today's session (run after 15:45 IST), every symbol that was streamed
    python scripts/reconcile_intraday.py --date 2026-10-23 --symbols RELIANCE,TCS

Read-only market data from Kite (one historical call per symbol, 3 per second). Streamed bars are never modified. Report: data/reports/intraday/reconcile_<date>.{md,html}
(standalone HTML: summary, how far streamed highs/lows were from the truth, and a streamed-vs-official chart per symbol).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from qsde.live import reconcile as R

OUT = Path(r"D:\Stoxsy\data\reports\intraday")


def _f(x, d=1, suf=""):
    return "n/a" if x is None or (isinstance(x, float) and not np.isfinite(x)) else f"{x:.{d}f}{suf}"


def markdown(d: date, res: list) -> str:
    n_ok = sum(r.status == "ok" for r in res)
    lines = [f"# Intraday reconciliation, {d}", "",
             f"{len(res)} symbols streamed; **{n_ok} ok**, {len(res) - n_ok} flagged. Official = Kite historical minute bars (reference). Streamed bars are kept untouched; "
             "the view `ohlcv_intraday_best` prefers the official bar. Edge bars (first/last streamed minute, partial when the stream started or stopped mid-session) are excluded from the statistics.", "",
             "| Symbol | Status | Streamed | Official | Both | Coverage | Close within 2 bps | Close |diff| p95 bps | High missed p95 bps | Volume ratio |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in res:
        s = r.stats
        lines.append(f"| {r.symbol} | {r.status} | {r.n_stream} | {r.n_official} | {r.n_both} | {_f(s.get('coverage', float('nan')) * 100, 0, '%')} | "
                     f"{_f(s.get('close_match_rate', float('nan')) * 100, 0, '%')} | {_f(s.get('close_abs_bps_p95'))} | {_f(s.get('high_miss_bps_p95'))} | {_f(s.get('volume_ratio_total'), 2)} |")
    lines += ["", "Status meanings: ok; gappy (fewer than 98% of official bars were streamed); price_drift (close differs by more than 5 bps at the 95th percentile); "
              "volume_off (total volume outside 0.85 to 1.15 of official); too_short (fewer than 3 comparable bars); no_official / no_stream (nothing to compare). Research and decision support, not investment advice."]
    return "\n".join(lines)


def html_page(d: date, res: list) -> str:
    rows, series = [], {}
    for r in res:
        s = r.stats
        rows.append({"s": r.symbol, "st": r.status, "n": [r.n_stream, r.n_official, r.n_both], "cov": s.get("coverage"), "p95": s.get("close_abs_bps_p95"), "hm": s.get("high_miss_bps_p95"),
                     "lm": s.get("low_miss_bps_p95"), "vr": s.get("volume_ratio_total")})
    worst = sorted([r for r in res if r.joined is not None and len(r.joined)], key=lambda r: -(r.stats.get("high_miss_bps_p95") or 0))[:12]
    for r in worst:
        j = r.joined
        series[r.symbol] = {"t": [t.tz_convert(R.IST).strftime("%H:%M") for t in j.index], "cs": [round(float(x), 2) for x in j["close_s"]], "co": [round(float(x), 2) for x in j["close_o"]],
                            "hs": [round(float(x), 2) for x in j["high_s"]], "ho": [round(float(x), 2) for x in j["high_o"]], "edge": [bool(x) for x in j["edge"]]}
    diffs = pd.concat([r.joined.loc[~r.joined["edge"], "close_diff_bps"] for r in res if r.joined is not None and len(r.joined)], ignore_index=True) if any(r.joined is not None for r in res) else pd.Series(dtype=float)
    hist, edges = np.histogram(diffs.clip(-10, 10), bins=20, range=(-10, 10)) if len(diffs) else (np.zeros(20), np.linspace(-10, 10, 21))
    cfg = json.dumps({"rows": rows, "series": series, "hist": [int(x) for x in hist], "edges": [float(x) for x in edges], "n": int(len(diffs)), "date": str(d)}, default=lambda o: None)
    n_ok = sum(r.status == "ok" for r in res)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Intraday reconciliation</title>
<style>:root{{--bg:#f6f8fb;--card:#fff;--ink:#0f1b2d;--muted:#5b6b82;--line:#dbe3ee;--up:#0b8a5f;--down:#c0392b;--acc:#3b5bdb;--warn:#b7791f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1421;--card:#141e30;--ink:#e8eef8;--muted:#93a4bd;--line:#26344d;--up:#2fd4a0;--down:#ff7a6b;--acc:#7c93ff;--warn:#f6c453}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif}}main{{max-width:1000px;margin:0 auto;padding:16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}}
.muted{{color:var(--muted)}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}.tile{{border:1px solid var(--line);border-radius:12px;padding:12px}}.tile b{{font-size:1.5rem;display:block}}
table{{border-collapse:collapse;width:100%;font-size:.88rem}}td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}}td:first-child,th:first-child,td:nth-child(2),th:nth-child(2){{text-align:left}}
.ok{{color:var(--up)}}.bad{{color:var(--down)}}.w{{color:var(--warn)}}svg{{width:100%;height:auto}}select{{padding:6px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--ink)}}
.overflow{{overflow-x:auto}}</style></head><body><main>
<h1>Streamed bars vs Kite's official minute bars, {d}</h1>
<p class="muted">The live stream sees price snapshots, not every trade, so it can miss a few paise on a minute's high or low and start mid-minute. After the close we compare it with Kite's own minute bars (the reference) and keep both.</p>
<div class="tiles"><div class="tile"><b>{len(res)}</b>symbols streamed</div><div class="tile"><b class="ok">{n_ok}</b>reconciled ok</div><div class="tile"><b class="{'bad' if len(res) - n_ok else 'ok'}">{len(res) - n_ok}</b>flagged</div></div>
<div class="card"><h2>How far off was the stream?</h2><svg id="hist" role="img" aria-label="Histogram of streamed minus official close, in basis points"></svg>
<p class="muted" id="histcap"></p></div>
<div class="card"><h2>One symbol, minute by minute</h2><label>Symbol <select id="sym"></select></label> <button id="play">Replay</button>
<svg id="chart" role="img" aria-label="Streamed versus official close and high, by minute"></svg><p class="muted">Solid line: official close. Dots: streamed close. Triangles: how far the streamed high sat below the official high.</p></div>
<div class="card overflow"><h2>All symbols</h2><table id="tbl"></table></div>
<p class="muted">Research and decision support, not investment advice. Data: Kite Connect.</p>
<script>
const C={cfg};const $=id=>document.getElementById(id);
function hist(){{const W=900,H=240,L=40,B=30,m=Math.max(...C.hist,1),bw=(W-L-10)/C.hist.length;let s='';
C.hist.forEach((v,i)=>{{const h=(H-B-20)*v/m,x=L+i*bw,y=H-B-h,mid=(C.edges[i]+C.edges[i+1])/2;s+='<rect x="'+x+'" y="'+y+'" width="'+(bw-2)+'" height="'+h+'" rx="2" fill="'+(Math.abs(mid)<=2?'var(--up)':'var(--warn)')+'"><animate attributeName="height" from="0" to="'+h+'" dur=".7s"/><animate attributeName="y" from="'+(H-B)+'" to="'+y+'" dur=".7s"/></rect>';}});
s+='<line x1="'+L+'" x2="'+(W-10)+'" y1="'+(H-B)+'" y2="'+(H-B)+'" stroke="currentColor" opacity=".4"/>';[-10,-5,0,5,10].forEach(v=>{{const x=L+(v+10)/20*(W-L-10);s+='<text x="'+x+'" y="'+(H-10)+'" text-anchor="middle" font-size="11" fill="currentColor">'+v+' bps</text>';}});
$('hist').setAttribute('viewBox','0 0 '+W+' '+H);$('hist').innerHTML=s;
const inside=C.n?C.hist.slice(8,12).reduce((a,b)=>a+b,0)/C.n:0;$('histcap').textContent=C.n?Math.round(inside*100)+'% of '+C.n+' compared minutes had a streamed close within 2 basis points (0.02%) of the official close. Green = within 2 bps.':'Nothing to compare.';}}
function chart(sym,anim){{const d=C.series[sym];if(!d)return;const W=900,H=300,L=56,R=16,T=14,B=30,n=d.t.length;const all=d.cs.concat(d.co,d.hs,d.ho);const lo=Math.min(...all),hi=Math.max(...all),pad=(hi-lo)*.1||1;
const X=i=>L+i*(W-L-R)/Math.max(n-1,1),Y=v=>T+(hi+pad-v)/(hi-lo+2*pad)*(H-T-B);let s='';
for(let k=0;k<=4;k++){{const v=lo-pad+(hi-lo+2*pad)*k/4;s+='<line x1="'+L+'" x2="'+(W-R)+'" y1="'+Y(v)+'" y2="'+Y(v)+'" stroke="currentColor" opacity=".12"/><text x="'+(L-6)+'" y="'+(Y(v)+4)+'" text-anchor="end" font-size="11" fill="currentColor">'+v.toFixed(2)+'</text>';}}
s+='<polyline fill="none" stroke="var(--acc)" stroke-width="2" points="'+d.co.map((v,i)=>X(i)+','+Y(v)).join(' ')+'"/>';
d.cs.forEach((v,i)=>{{s+='<circle cx="'+X(i)+'" cy="'+Y(v)+'" r="3" fill="'+(d.edge[i]?'var(--warn)':'var(--up)')+'"><title>'+d.t[i]+' streamed '+v+' official '+d.co[i]+(d.edge[i]?' (partial edge bar)':'')+'</title></circle>';}});
d.hs.forEach((v,i)=>{{if(d.ho[i]-v>0.005)s+='<path d="M'+(X(i)-4)+','+(Y(d.ho[i])+7)+' L'+(X(i)+4)+','+(Y(d.ho[i])+7)+' L'+X(i)+','+Y(d.ho[i])+' Z" fill="var(--down)" opacity=".7"><title>'+d.t[i]+' streamed high '+v+' vs official '+d.ho[i]+'</title></path>';}});
[0,Math.floor(n/2),n-1].forEach(i=>{{s+='<text x="'+X(i)+'" y="'+(H-8)+'" text-anchor="middle" font-size="11" fill="currentColor">'+d.t[i]+'</text>';}});
$('chart').setAttribute('viewBox','0 0 '+W+' '+H);$('chart').innerHTML=s;if(anim){{const g=$('chart').querySelectorAll('circle,path');g.forEach((e,i)=>{{e.style.opacity=0;setTimeout(()=>{{e.style.transition='opacity .2s';e.style.opacity=1}},i*(1500/Math.max(g.length,1)))}});}}}}
const sel=$('sym');Object.keys(C.series).forEach(k=>{{const o=document.createElement('option');o.textContent=k;sel.appendChild(o)}});sel.onchange=()=>chart(sel.value,false);$('play').onclick=()=>chart(sel.value,true);
const pc=(x,d=0)=>x==null?'n/a':(x*100).toFixed(d)+'%',f=(x,d=1)=>x==null?'n/a':x.toFixed(d);
$('tbl').innerHTML='<tr><th>Symbol</th><th>Status</th><th>Streamed</th><th>Official</th><th>Both</th><th>Coverage</th><th>Close p95 bps</th><th>High missed p95 bps</th><th>Low missed p95 bps</th><th>Volume ratio</th></tr>'+C.rows.map(r=>'<tr><td>'+r.s+'</td><td class="'+(r.st==='ok'?'ok':'bad')+'">'+r.st+'</td><td>'+r.n[0]+'</td><td>'+r.n[1]+'</td><td>'+r.n[2]+'</td><td>'+pc(r.cov)+'</td><td>'+f(r.p95)+'</td><td>'+f(r.hm)+'</td><td>'+f(r.lm)+'</td><td>'+f(r.vr,2)+'</td></tr>').join('');
hist();if(sel.value)chart(sel.value,false);
</script></main></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="session date YYYY-MM-DD (default: today, IST)")
    ap.add_argument("--symbols", default=None)
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else datetime.now(R.IST).date()
    syms = [s.strip().upper() for s in a.symbols.split(",")] if a.symbols else None
    res = R.run(d, syms)
    if not res:
        print(f"no streamed minute bars for {d}: nothing to reconcile")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"reconcile_{d}.md").write_text(markdown(d, res), encoding="utf-8")
    (OUT / f"reconcile_{d}.html").write_text(html_page(d, res), encoding="utf-8")
    print(f"{len(res)} symbols; ok {sum(r.status == 'ok' for r in res)}; report {OUT / f'reconcile_{d}.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
