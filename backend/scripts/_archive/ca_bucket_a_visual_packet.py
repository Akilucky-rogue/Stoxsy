"""Visual, interactive Bucket A decision packet: a standalone HTML file (inline SVG + vanilla JS, no external dependency, works offline).

    python scripts/ca_bucket_a_visual_packet.py --remaining      # events without a recorded owner decision
    python scripts/ca_bucket_a_visual_packet.py --all
For every event: an animated chart (raw prices -> apply the listed factor -> what is left over), the facts, flags, the system's recommendation, and three buttons to record YOUR decision.
"Export my decisions" produces the CSV/JSON to send back; nothing is applied by this file. Source rows come from the review worksheet, the decision-packet CSV and raw NSE prices.
"""
from __future__ import annotations

import argparse
import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qsde.config.paths import data_path, DATA
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd

OUT = data_path("reports", "corp_actions")
VIS = OUT / "visual"


def build_events(remaining: bool) -> list:
    from qsde.db.connection import read_sql
    from qsde.ingestion import bucket_a_decisions as BA
    from qsde.ingestion import nse_eod

    w = pd.read_csv(OUT / "review_bucket_a_official_match_price_mismatch.csv")
    w["gap_date"] = pd.to_datetime(w["gap_date"])
    done = {(x["symbol"], x["ex_date"]): x["decision"] for x in BA.load()["decisions"]}
    pk = pd.read_csv(OUT / ("packet_bucket_a_decision_remaining.csv" if remaining else "packet_bucket_a_decision_top10.csv"))
    pk["gap_date"] = pd.to_datetime(pk["gap_date"])
    w = w.merge(pk[["symbol", "gap_date", "recommended", "reason", "flags"]], on=["symbol", "gap_date"], how="inner", suffixes=("", "_pk"))
    syms = sorted(w["symbol"].unique())
    raw = read_sql("SELECT exchange, symbol, series, date, open, high, low, close, volume FROM eod_raw WHERE exchange = 'NSE' AND symbol = ANY(:s)", params={"s": syms})
    raw["date"] = pd.to_datetime(raw["date"])
    bars = {s: g.sort_values("date").reset_index(drop=True) for s, g in nse_eod.pick_primary_series(raw).groupby("symbol")}
    events = []
    for r in w.itertuples():
        g = bars[r.symbol]
        i = int(g.index[g["date"] >= r.gap_date][0])
        pre60 = g.iloc[max(0, i - 60): i]
        med = float(pre60["volume"].median()) if len(pre60) else float("nan")
        win = g.iloc[max(0, i - 5): i + 6]
        rows = []
        for k, x in zip(range(max(0, i - 5) - i, max(0, i - 5) - i + len(win)), win.itertuples()):
            vx = (x.volume * (r.listed_factor if x.date >= r.gap_date else 1.0)) / med if med and med == med else None
            rows.append({"k": k, "date": str(x.date.date()), "open": float(x.open), "close": float(x.close), "volx": None if vx is None or vx != vx else round(float(vx), 2)})
        ex = str(pd.Timestamp(r.nse_ex_date).date())
        events.append({"symbol": r.symbol, "exdate": ex, "action": " ".join(str(r.nse_subject).split())[:60], "isin": r.isin, "listed": float(r.listed_factor), "observed": float(r.observed_ratio),
                       "resid_pct": float(r.residual_overnight_after_listed_factor) * 100, "resid_sigma": float(r.residual_in_sigma), "sigma": float(r.daily_sigma_60d) * 100,
                       "gap_volx": round(float(r.gap_volume_x_median) * float(r.listed_factor), 2), "next5": float(r.next5d_return) * 100,
                       "flags": "" if pd.isna(r.flags_pk) else str(r.flags_pk), "rec": r.recommended, "reason": str(r.reason), "decided": done.get((r.symbol, ex)), "rows": rows})
    order = {"reject": 0, "needs_more_evidence": 1, "confirm": 2}
    events.sort(key=lambda e: (order.get(e["rec"], 3), e["symbol"], e["exdate"]))
    return events


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--remaining", action="store_true")
    g.add_argument("--all", action="store_true")
    a = ap.parse_args()
    ev = build_events(remaining=not a.all)
    if a.all:                       # the top-10 packet and the remaining packet together
        ev = [e for e in ev] + [e for e in build_events(remaining=True) if e["symbol"] + e["exdate"] not in {x["symbol"] + x["exdate"] for x in ev}]
    VIS.mkdir(parents=True, exist_ok=True)
    html = TEMPLATE.replace("/*DATA*/[]", json.dumps(ev, allow_nan=False, default=float)).replace("__TITLE__", "Bucket A: decide the remaining corporate-action events" if not a.all else "Bucket A: all events")
    p = VIS / ("bucket_a_remaining_visual.html" if not a.all else "bucket_a_all_visual.html")
    p.write_text(html, encoding="utf-8")
    print(f"{len(ev)} events -> {p} ({p.stat().st_size / 1024:.0f} KB)")
    return 0


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f8fa;--card:#fff;--ink:#14202b;--mut:#5b6b7a;--line:#dfe5ea;--brand:#0a8f6a;--blue:#2563eb;--red:#c0392b;--amber:#b7791f;--soft:#eef3f7}
@media (prefers-color-scheme:dark){:root{--bg:#0f1720;--card:#16212c;--ink:#e6edf3;--mut:#9fb0bf;--line:#26343f;--brand:#2fc79a;--blue:#6ea0ff;--red:#ff7b6b;--amber:#f2b84b;--soft:#1b2a36}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--ink)}
main{max-width:1060px;margin:0 auto;padding:18px 16px 80px}h1{font-size:1.5rem;margin:.2rem 0}h2{font-size:1.1rem;margin:0 0 .4rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}
.muted{color:var(--mut)}.row{display:flex;gap:14px;flex-wrap:wrap}.col{flex:1 1 340px;min-width:0}
.chip{display:inline-block;padding:2px 9px;border-radius:99px;font-size:.78rem;border:1px solid var(--line);background:var(--soft)}
.chip.reject{color:var(--red)}.chip.needs_more_evidence{color:var(--amber)}.chip.confirm{color:var(--brand)}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:7px 12px;border-radius:10px;cursor:pointer}
button:hover{background:var(--soft)}button.primary{background:var(--brand);color:#fff;border-color:transparent}button.sel{outline:2px solid var(--brand)}
button.sel.reject{outline-color:var(--red)}button.sel.needs_more_evidence{outline-color:var(--amber)}
.verd{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}.facts{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:.9rem}.facts dt{color:var(--mut)}.facts dd{margin:0}
svg{width:100%;height:auto;display:block}.cap{font-size:.9rem;min-height:3.2em;margin:.3rem 0}.stepbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.bar{height:12px;border-radius:6px}.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:.82rem;color:var(--mut)}.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:4px}
textarea{width:100%;min-height:120px;font:12px/1.4 ui-monospace,Consolas,monospace;background:var(--soft);color:var(--ink);border:1px solid var(--line);border-radius:10px;padding:8px}
.sticky{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;border-bottom:1px solid var(--line)}
</style></head><body><main>
<h1>__TITLE__</h1>
<p class="muted">Nothing here changes any price. You choose, the file records your choices, and you send them back. Open the first card to see how to read the chart.</p>

<section class="card" id="guide">
  <h2>How to decide in one minute</h2>
  <div class="row">
    <div class="col">
      <p>A bonus or split makes the share price fall on the <b>ex-date</b> even though shareholders lost nothing (twice the shares, half the price). To compare prices before and after, history is multiplied by the <b>listed factor</b> (1:1 bonus = &times;0.5).</p>
      <p>Here the exchange record is correct about <i>what</i> happened and <i>when</i>. The only doubt is precision: after applying the listed factor, the price still jumps a few percent. That leftover is usually the stock's ordinary ex-date move.</p>
      <p><b>Your three choices</b></p>
      <ul>
        <li><b>Confirm</b>: accept the listed factor. Worst case, old prices are off by the leftover (a median of 6%, at most about 10%).</li>
        <li><b>Hold</b>: not decided. History keeps the fake cliff (a median 50% drop) at that date, and research windows touching it are excluded.</li>
        <li><b>Reject</b>: the listing is wrong for this event (you have a reason). Same fake cliff as hold, but final.</li>
      </ul>
    </div>
    <div class="col">
      <svg viewBox="0 0 360 200" role="img" aria-label="Cost of a wrong decision: a wrongly held event leaves a median 50 percent fake cliff; a wrongly confirmed one a median 6 percent error">
        <text x="0" y="14" font-size="12" fill="currentColor">What a wrong decision costs (typical event)</text>
        <text x="0" y="52" font-size="12" fill="currentColor">Hold / reject by mistake</text><rect x="0" y="58" width="230" height="22" rx="5" fill="#c0392b"><animate attributeName="width" from="0" to="230" dur="1.2s" fill="freeze"/></rect><text x="236" y="74" font-size="12" fill="currentColor">~50% cliff</text>
        <text x="0" y="112" font-size="12" fill="currentColor">Confirm by mistake</text><rect x="0" y="118" width="28" height="22" rx="5" fill="#0a8f6a"><animate attributeName="width" from="0" to="28" dur="1.2s" fill="freeze"/></rect><text x="34" y="134" font-size="12" fill="currentColor">~6% error (max ~10%)</text>
        <text x="0" y="172" font-size="11" fill="#7a8a99">Neither choice changes today's signal or cards:</text><text x="0" y="187" font-size="11" fill="#7a8a99">no event here touches the current universe.</text>
      </svg>
      <p class="muted" style="font-size:.85rem">Rule of thumb used for the recommendations: <b>confirm</b> when there is no flag and the leftover is within 2.5 daily moves; <b>reject</b> when it is above 4 (too large to be an ordinary move, or a demerger sits nearby); otherwise <b>hold</b>.</p>
    </div>
  </div>
</section>

<div class="sticky"><div class="stepbar">
  <b id="prog">0 of 0 chosen</b>
  <span class="muted">Show:</span>
  <button data-f="all" class="sel">All</button><button data-f="reject">Reject</button><button data-f="needs_more_evidence">Hold</button><button data-f="confirm">Confirm</button>
  <span style="flex:1"></span>
  <button id="acceptAll">Accept all recommendations</button><button id="exportBtn" class="primary">Export my decisions</button>
</div></div>
<div id="list"></div>

<section class="card" id="exportBox" hidden>
  <h2>Your decisions</h2><p class="muted">Copy this text into the chat (or save it as a .csv). It records your choice for every event; undecided events are listed as <code>pending</code>.</p>
  <textarea id="exportText" readonly></textarea>
  <div class="verd"><button id="copyBtn">Copy</button><button id="dlBtn">Download .csv</button></div>
</section>
</main>
<script>
const EVENTS = /*DATA*/[];
const LABEL = {confirm:"Confirm", needs_more_evidence:"Hold", reject:"Reject"};
const REC_TEXT = {confirm:"System recommends: confirm", needs_more_evidence:"System recommends: hold", reject:"System recommends: reject"};
const state = {}; // key -> decision
const key = e => e.symbol + "|" + e.exdate;
EVENTS.forEach(e => { if (e.decided) state[key(e)] = e.decided; });
const f = (x, d=2) => (x==null||isNaN(x)) ? "-" : Number(x).toFixed(d);
function geoOf(e, zoom){
  const W=520,H=262,L=46,R=14,T=22,B=84, rows=e.rows, n=rows.length;
  const adj = rows.map(r => ({k:r.k, raw:r.close, adj:(r.k<0 ? r.close*e.listed : r.close), op:r.open*(r.k<0?e.listed:1)}));
  const ys = zoom ? adj.flatMap(a=>[a.adj,a.op]) : adj.flatMap(a=>[a.raw,a.adj]);
  let lo=Math.min(...ys), hi=Math.max(...ys); const pad=(hi-lo)*0.1||1; lo-=pad; hi+=pad;
  const X = i => L + (W-L-R)*i/(n-1), Y = v => T + (H-T-B)*(1-(v-lo)/(hi-lo));
  return {W,H,L,R,T,B,n,rows,adj,lo,hi,X,Y, gi: rows.findIndex(r=>r.k===0)};
}
function axes(e,g){
  let s="";
  for(let t=0;t<=3;t++){const v=g.lo+(g.hi-g.lo)*t/3; s+=`<line x1="${g.L}" x2="${g.W-g.R}" y1="${g.Y(v)}" y2="${g.Y(v)}" stroke="currentColor" opacity=".12"/><text x="${g.L-6}" y="${g.Y(v)+4}" font-size="10" text-anchor="end" fill="currentColor" opacity=".7">${f(v,v>100?0:2)}</text>`;}
  g.rows.forEach((r,i)=>{ s+=`<text x="${g.X(i)}" y="${g.H-g.B+14}" font-size="10" text-anchor="middle" fill="currentColor" opacity=".7">${r.k===0?"ex-date":(r.k>0?"+"+r.k:r.k)}</text>`; });
  s += `<rect x="${g.X(g.gi)-((g.W-g.L-g.R)/(g.n-1))/2}" y="${g.T}" width="${(g.W-g.L-g.R)/(g.n-1)}" height="${g.H-g.T-g.B}" fill="currentColor" opacity=".06"/>`;
  const vmax = Math.max(1, ...g.rows.map(r=>r.volx||0));
  g.rows.forEach((r,i)=>{ const h=((r.volx||0)/vmax)*34; s+=`<rect x="${g.X(i)-9}" y="${g.H-24-h}" width="18" height="${h}" fill="${r.k===0?'#2563eb':'#8a97a3'}" opacity=".55"/>`; });
  s += `<text x="${g.L}" y="${g.H-2}" font-size="10" fill="currentColor" opacity=".7">volume vs normal (bars; share-count adjusted)</text>`;
  return s;
}
function draw(e, el, step){
  const wrap = el.querySelector(".svgwrap"), cap = el.querySelector(".cap");
  const pct = Math.round((1-e.listed)*100);
  if (step===3){
    const g = geoOf(e,true), gi=g.gi, pi=gi-1, prevAdj=g.adj[pi].adj, gapOpen=g.rows[gi].open, sig=e.sigma/100;
    const bandHi=Math.min(prevAdj*(1+2*sig),g.hi), bandLo=Math.max(prevAdj*(1-2*sig),g.lo), col = gapOpen>=prevAdj?'#0a8f6a':'#c0392b';
    let s=`<svg viewBox="0 0 ${g.W} ${g.H}" role="img" aria-label="Zoom on ${e.symbol}: adjusted prices and the leftover move at the ex-date">`+axes(e,g);
    s+=`<rect x="${g.X(gi)-30}" y="${g.Y(bandHi)}" width="${g.X(g.n-1)-g.X(gi)+44}" height="${Math.max(2,g.Y(bandLo)-g.Y(bandHi))}" fill="#2563eb" opacity=".14"/>`;
    s+=`<polyline fill="none" stroke="#2563eb" stroke-width="3" points="${g.adj.map((a,i)=>g.X(i)+","+g.Y(a.adj)).join(" ")}"/>`+g.adj.map((a,i)=>`<circle cx="${g.X(i)}" cy="${g.Y(a.adj)}" r="3.2" fill="#2563eb"/>`).join("");
    s+=`<line x1="${g.X(gi)}" x2="${g.X(gi)}" y1="${g.Y(prevAdj)}" y2="${g.Y(gapOpen)}" stroke="${col}" stroke-width="4"/><circle cx="${g.X(pi)}" cy="${g.Y(prevAdj)}" r="5" fill="none" stroke="${col}" stroke-width="2"/><circle cx="${g.X(gi)}" cy="${g.Y(gapOpen)}" r="5" fill="none" stroke="${col}" stroke-width="2"/>`;
    s+=`<text x="${g.W-g.R}" y="14" font-size="12" font-weight="700" text-anchor="end" fill="${col}">leftover ${e.resid_pct>=0?"+":""}${f(e.resid_pct,1)}% = ${f(Math.abs(e.resid_sigma),1)} normal days</text></svg>`;
    wrap.innerHTML=s; wrap.style.opacity=0; requestAnimationFrame(()=>{wrap.style.transition="opacity .5s"; wrap.style.opacity=1;});
    const inside = Math.abs(e.resid_sigma) <= 2.5;
    cap.textContent = `3. Zoomed in on the adjusted prices. The leftover (${e.resid_pct>=0?"+":""}${f(e.resid_pct,1)}%) is the jump between the circled points. The blue band is this stock's normal range (2 typical days = 2 x ${f(e.sigma,1)}%). `+(inside?"The leftover is within about 2.5 normal days: an ordinary ex-date move.":"The leftover is larger than an ordinary ex-date move: look closer.");
    return;
  }
  const g = geoOf(e,false);
  const render = vals => `<svg viewBox="0 0 ${g.W} ${g.H}" role="img" aria-label="Raw and adjusted prices around the ex-date of ${e.symbol}">`+axes(e,g)+
    `<polyline fill="none" stroke="#8a97a3" stroke-width="2" stroke-dasharray="5 4" points="${g.adj.map((a,i)=>g.X(i)+","+g.Y(a.raw)).join(" ")}"/>`+
    `<polyline fill="none" stroke="#2563eb" stroke-width="3" points="${vals.map((v,i)=>g.X(i)+","+g.Y(v)).join(" ")}"/>`+vals.map((v,i)=>`<circle cx="${g.X(i)}" cy="${g.Y(v)}" r="3.2" fill="#2563eb"/>`).join("")+`</svg>`;
  const from=g.adj.map(a=>a.raw), to=g.adj.map(a=>a.adj);
  if (step===1){ wrap.innerHTML=render(from); cap.textContent=`1. As traded: the price drops about ${pct}% overnight at the ex-date. Nothing was lost: the number of shares went up.`; return; }
  cap.textContent=`2. Apply the listed factor (x${f(e.listed,4)}) to every price before the ex-date: the cliff disappears.`;
  const t0=performance.now(), dur=1400;
  const tick=now=>{ const p=Math.min(1,(now-t0)/dur), q=p<.5?2*p*p:1-Math.pow(-2*p+2,2)/2; wrap.innerHTML=render(from.map((v,i)=>v+(to[i]-v)*q)); if(p<1) requestAnimationFrame(tick); };
  requestAnimationFrame(tick);
}
function card(e, idx){
  e.id = idx; const k = key(e);
  const flags = e.flags ? `<span class="chip needs_more_evidence">${e.flags}</span>` : `<span class="chip">no flags</span>`;
  const el = document.createElement("section"); el.className="card"; el.dataset.rec=e.rec; el.id="ev-"+idx;
  el.innerHTML = `<div class="stepbar"><h2 style="margin:0">${e.symbol} <span class="muted" style="font-weight:400">${e.exdate}</span></h2><span class="chip ${e.rec}">${REC_TEXT[e.rec]}</span>${flags}${e.decided?'<span class="chip">already decided: '+LABEL[e.decided]+'</span>':''}</div>
  <div class="row"><div class="col"><div class="svgwrap"></div>
    <div class="stepbar"><button data-s="1">1 As traded</button><button data-s="2">2 Apply factor</button><button data-s="3">3 Leftover</button><button data-s="play" class="primary">Play</button></div>
    <p class="cap" id="cap-${idx}"></p>
    <div class="legend"><span><i class="dot" style="background:#8a97a3"></i>raw close</span><span><i class="dot" style="background:#2563eb"></i>close x listed factor</span><span><i class="dot" style="background:#2563eb;opacity:.3"></i>normal range</span></div></div>
  <div class="col"><dl class="facts"><dt>Action</dt><dd>${e.action}</dd><dt>Listed factor</dt><dd>x${f(e.listed,4)} (the price should fall to ${f(e.listed*100,1)}%)</dd><dt>Observed overnight</dt><dd>x${f(e.observed,4)}</dd>
    <dt>Leftover</dt><dd>${e.resid_pct>=0?"+":""}${f(e.resid_pct,1)}% = ${f(Math.abs(e.resid_sigma),1)} normal days</dd><dt>Volume at the ex-date</dt><dd>${f(e.gap_volx,1)}x normal</dd><dt>Next 5 sessions</dt><dd>${e.next5>=0?"+":""}${f(e.next5,1)}%</dd><dt>ISIN</dt><dd>${e.isin||"-"}</dd></dl>
    <p class="muted" style="font-size:.85rem">${e.reason}</p>
    <div class="verd" data-k="${k}"><button data-d="confirm">Confirm</button><button data-d="needs_more_evidence">Hold</button><button data-d="reject">Reject</button></div>
    <input data-note="${k}" placeholder="Optional note (why)" style="width:100%;padding:7px;border:1px solid var(--line);border-radius:8px;background:var(--soft);color:var(--ink)"></div></div>`;
  el.querySelectorAll("[data-s]").forEach(b=>b.onclick=()=>{ const s=b.dataset.s; if(s==="play"){ draw(e,el,1); setTimeout(()=>draw(e,el,2),900); setTimeout(()=>draw(e,el,3),3200);} else draw(e,el,+s); });
  el.querySelectorAll("[data-d]").forEach(b=>b.onclick=()=>{ state[k]=b.dataset.d; refresh(); });
  return el;
}
function refresh(){
  document.querySelectorAll("#list .verd").forEach(v=>{ const k=v.dataset.k; v.querySelectorAll("button").forEach(b=>{ b.className = "sel_"+(state[k]===b.dataset.d); b.classList.toggle("sel", state[k]===b.dataset.d); b.classList.toggle(b.dataset.d, state[k]===b.dataset.d); }); });
  const n = EVENTS.filter(e=>state[key(e)] && !e.decided).length, total = EVENTS.filter(e=>!e.decided).length;
  document.getElementById("prog").textContent = n + " of " + total + " chosen";
}
const list = document.getElementById("list");
EVENTS.forEach((e,i)=>{ const el = card(e,i); list.appendChild(el); draw(e, el, 3); });
refresh();
if (!EVENTS.length) list.innerHTML = '<div class="card">No events to show.</div>';
document.querySelectorAll("[data-f]").forEach(b=>b.onclick=()=>{ document.querySelectorAll("[data-f]").forEach(x=>x.classList.remove("sel")); b.classList.add("sel");
  list.querySelectorAll("section").forEach(s=>s.hidden = b.dataset.f!=="all" && s.dataset.rec!==b.dataset.f); });
document.getElementById("acceptAll").onclick=()=>{ EVENTS.forEach(e=>{ if(!e.decided) state[key(e)]=e.rec; }); refresh(); };
document.getElementById("exportBtn").onclick=()=>{
  const lines=["symbol,ex_date,decision,note"]; EVENTS.forEach(e=>{ if(e.decided) return; const k=key(e), note=(document.querySelector('[data-note="'+k+'"]')||{}).value||"";
    lines.push([e.symbol,e.exdate,state[k]||"pending",'"'+note.replace(/"/g,'""')+'"'].join(",")); });
  const box=document.getElementById("exportBox"); box.hidden=false; document.getElementById("exportText").value=lines.join("\n"); box.scrollIntoView({behavior:"smooth"}); };
document.getElementById("copyBtn").onclick=async()=>{ const t=document.getElementById("exportText"); t.select(); try{ await navigator.clipboard.writeText(t.value);}catch(e){ document.execCommand("copy"); } };
document.getElementById("dlBtn").onclick=()=>{ const a=document.createElement("a"); a.href=URL.createObjectURL(new Blob([document.getElementById("exportText").value],{type:"text/csv"})); a.download="bucket_a_decisions.csv"; a.click(); };
</script></body></html>
"""

if __name__ == "__main__":
    raise SystemExit(main())
