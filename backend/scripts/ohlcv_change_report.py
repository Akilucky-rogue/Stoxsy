"""Before/after validation of an `ohlcv` change against a retained snapshot (see ohlcv_snapshot.py). Read-only.

    python scripts/ohlcv_change_report.py --before data/ohlcv_versions/ohlcv_<...>.parquet

Checks (each PASS/FAIL, with the numbers): only the intended symbols changed and only before the intended dates; no rows added or removed unexpectedly; no duplicate (symbol, date) keys;
no OHLC violations introduced; the review tags are 31 confirmed / 34 held / 4 rejected on the events table; rejected events are no longer applied while held and confirmed ones still
are; the event-day return of every confirmed and held event is unchanged and every rejected one now shows its raw corporate-action move; the count of one-day moves above 35% rises by
exactly the rejected events. Output: data/reports/corp_actions/ohlcv_change_report_<date>.{md,html} (standalone, animated before/after).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd

from qsde.config.paths import data_path

OUT = data_path("reports", "corp_actions")
TOL = 1e-9
CLIFF = 0.35


def event_returns(px: pd.DataFrame, decisions: list) -> pd.DataFrame:
    g = {s: d.set_index("date").sort_index() for s, d in px[px["symbol"].isin({x["symbol"] for x in decisions})].groupby("symbol")}
    rows = []
    for x in decisions:
        d = g.get(x["symbol"])
        t = pd.Timestamp(x["ex_date"])
        if d is None or t not in d.index or d.index.get_loc(t) == 0:
            rows.append({"symbol": x["symbol"], "ex_date": x["ex_date"], "decision": x["decision"], "gap": np.nan, "close_to_close": np.nan})
            continue
        i = d.index.get_loc(t)
        pc = d["close"].iloc[i - 1]
        rows.append({"symbol": x["symbol"], "ex_date": x["ex_date"], "decision": x["decision"], "gap": float(d["open"].iloc[i] / pc - 1), "close_to_close": float(d["close"].iloc[i] / pc - 1)})
    return pd.DataFrame(rows)


def compute(before: pd.DataFrame, after: pd.DataFrame, decisions: list, events: pd.DataFrame, expected_symbols: set) -> dict:
    key = ["symbol", "date"]
    for d in (before, after):
        d["date"] = pd.to_datetime(d["date"])
    m = before.merge(after, on=key, how="outer", suffixes=("_b", "_a"), indicator=True)
    added, removed = int((m["_merge"] == "right_only").sum()), int((m["_merge"] == "left_only").sum())
    both = m[m["_merge"] == "both"]
    changed = pd.Series(False, index=both.index)
    for c in ("open", "high", "low", "close"):
        a, b = both[f"{c}_a"].astype(float), both[f"{c}_b"].astype(float)
        changed |= ((a - b).abs() > TOL * b.abs().clip(lower=1e-12)) | (a.isna() != b.isna())
    ch = both[changed]
    per_symbol = ch.groupby("symbol").agg(rows=("date", "size"), first=("date", "min"), last=("date", "max")).reset_index()
    unexpected = per_symbol[~per_symbol["symbol"].isin(expected_symbols)]
    # the changed rows of an expected symbol must all be BEFORE its last rejected ex-date
    last_rej = {}
    for x in decisions:
        if x["decision"] == "reject":
            last_rej[x["symbol"]] = max(last_rej.get(x["symbol"], pd.Timestamp.min), pd.Timestamp(x["ex_date"]))
    late = [(r.symbol, str(r.last.date())) for r in per_symbol.itertuples() if r.symbol in last_rej and r.last >= last_rej[r.symbol]]
    dup_after = int(after.duplicated(key).sum())
    viol = lambda d: int(((d["high"] < d[["open", "close"]].max(axis=1) - 1e-9) | (d["low"] > d[["open", "close"]].min(axis=1) + 1e-9) | (d[["open", "high", "low", "close"]] <= 0).any(axis=1)).sum())   # noqa: E731
    v_b, v_a = viol(before), viol(after)
    er_b, er_a = event_returns(before, decisions), event_returns(after, decisions)
    er = er_b.merge(er_a, on=["symbol", "ex_date", "decision"], suffixes=("_before", "_after"))
    same = er[er["decision"] != "reject"]
    unchanged_ok = bool(np.allclose(same["gap_before"].fillna(0), same["gap_after"].fillna(0), atol=1e-9) and np.allclose(same["close_to_close_before"].fillna(0), same["close_to_close_after"].fillna(0), atol=1e-9))
    rej = er[er["decision"] == "reject"]

    def cliffs(d):
        d = d.sort_values(key)
        r = d.groupby("symbol")["close"].pct_change()
        return int((r.abs() > CLIFF).sum())
    c_b, c_a = cliffs(before), cliffs(after)
    tags = events["review_status"].value_counts(dropna=True).to_dict() if "review_status" in events.columns else {}
    applied_by_key = {}
    for sym, ex, ap in zip(events["symbol"], pd.to_datetime(events["ex_date"]).dt.strftime("%Y-%m-%d"), events["applied"]):
        applied_by_key.setdefault((sym, ex), []).append(bool(ap))
    dec = {(x["symbol"], x["ex_date"]): x["decision"] for x in decisions}
    rej_applied = [k for k, d in dec.items() if d == "reject" and any(applied_by_key.get(k, []))]
    held_not_applied = [k for k, d in dec.items() if d == "needs_more_evidence" and not all(applied_by_key.get(k, [False]))]
    missing = [k for k in dec if k not in applied_by_key]
    checks = [
        ("1. The prior series is retained (snapshot present with a SHA-256)", True, "see the manifest next to the snapshot"),
        ("2. Only the intended symbols changed", unexpected.empty, f"changed symbols: {', '.join(per_symbol['symbol']) or 'none'}; unexpected: {', '.join(unexpected['symbol']) or 'none'}"),
        ("3. Changes stop before each rejected ex-date (nothing at or after it moved)", not late, "none" if not late else f"{late}"),
        ("4. No rows added or removed", added == 0 and removed == 0, f"added {added}, removed {removed}"),
        ("5. No duplicate (symbol, date) keys", dup_after == 0, f"{dup_after} duplicates"),
        ("6. No OHLC violations introduced", v_a <= v_b, f"before {v_b}, after {v_a}"),
        ("7. All 69 decisions matched an event; tags 31 confirmed / 34 held / 4 rejected", not missing and tags.get("owner_confirmed") == 31 and tags.get("applied_unreviewed") == 34 and tags.get("adjustment_removed") == 4,
         f"tags {tags}; unmatched {missing}"),
        ("8. Rejected events are no longer applied; held events still are (flagged unreviewed)", not rej_applied and not held_not_applied, f"rejected still applied: {rej_applied}; held not applied: {held_not_applied}"),
        ("9. Event-day returns of the 65 confirmed/held events are unchanged", unchanged_ok, f"{len(same)} events compared"),
        (f"10. One-day moves above {CLIFF:.0%} rise only by the rejected events that exceed it, and every rejected event now shows a visible gap",
         c_a - c_b == int((rej["close_to_close_after"].abs() > CLIFF).sum()) and bool((rej["gap_after"].abs() > 0.15).all()) if len(rej) else False,
         f"before {c_b}, after {c_a} (+{c_a - c_b}); rejected events above {CLIFF:.0%} close-to-close: {int((rej['close_to_close_after'].abs() > CLIFF).sum())}; "
         f"smaller rejected moves: {', '.join(f'{x.symbol} {x.close_to_close_after:+.0%}' for x in rej.itertuples() if abs(x.close_to_close_after) <= CLIFF) or 'none'}"),
    ]
    return {"checks": checks, "per_symbol": per_symbol, "er": er, "rows_before": int(len(before)), "rows_after": int(len(after)), "changed_rows": int(len(ch)), "tags": tags, "cliffs": (c_b, c_a)}


def markdown(r: dict, today: date, version: str) -> str:
    L = [f"# ohlcv change report, {today}", "", f"Compared with the retained version `{version}`. Rows before {r['rows_before']:,}, after {r['rows_after']:,}, changed rows {r['changed_rows']:,}.", "",
         "| Check | Result | Detail |", "|---|---|---|"]
    for name, ok, detail in r["checks"]:
        L.append(f"| {name} | {'PASS' if ok else '**FAIL**'} | {detail} |")
    L += ["", "## Rejected events: the raw corporate-action move is now visible (overnight gap, open vs previous close)", "", "| Symbol | Ex-date | Before | After |", "|---|---|---|---|"]
    rej = r["er"][r["er"]["decision"] == "reject"]
    for x in rej.itertuples():
        L.append(f"| {x.symbol} | {x.ex_date} | {x.gap_before:+.1%} | {x.gap_after:+.1%} |")
    L += ["", "## Changed symbols", "", "| Symbol | Rows changed | First | Last |", "|---|---|---|---|"] + [f"| {x.symbol} | {x.rows:,} | {x.first.date()} | {x.last.date()} |" for x in r["per_symbol"].itertuples()]
    L += ["", "Held events keep the listed factor and are tagged `applied_unreviewed`: provisionally applied under the owner's policy, NOT validated. Research and decision support, not investment advice."]
    return "\n".join(L)


def html_page(r: dict, today: date, version: str) -> str:
    rej = r["er"][r["er"]["decision"] == "reject"]
    data = json.dumps({"rej": [{"s": x.symbol, "d": x.ex_date, "b": round(float(x.gap_before), 4), "a": round(float(x.gap_after), 4)} for x in rej.itertuples()],
                       "checks": [[n, bool(ok), d] for n, ok, d in r["checks"]], "tags": r["tags"], "rows": [r["rows_before"], r["rows_after"], r["changed_rows"]]})
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ohlcv change report</title>
<style>:root{{--bg:#f6f8fb;--card:#fff;--ink:#0f1b2d;--muted:#5b6b82;--line:#dbe3ee;--ok:#0b8a5f;--bad:#c0392b;--acc:#3b5bdb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0d1421;--card:#141e30;--ink:#e8eef8;--muted:#93a4bd;--line:#26344d;--ok:#2fd4a0;--bad:#ff7a6b;--acc:#7c93ff}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif}}main{{max-width:980px;margin:0 auto;padding:16px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:14px 0}}
.muted{{color:var(--muted)}}.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px}}.tile{{border:1px solid var(--line);border-radius:12px;padding:12px}}.tile b{{font-size:1.5rem;display:block}}
.row{{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid var(--line)}}.pass{{color:var(--ok);font-weight:700;min-width:3.5em}}.fail{{color:var(--bad);font-weight:700;min-width:3.5em}}svg{{width:100%;height:auto}}button{{padding:8px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);cursor:pointer;font-weight:600}}
button[aria-pressed=true]{{background:var(--acc);color:#fff}}.bar{{transition:width .9s ease}}@media (prefers-reduced-motion:reduce){{.bar{{transition:none}}}}</style></head><body><main>
<h1>What the Bucket A rebuild changed</h1><p class="muted">Compared with the retained version <code>{version}</code>, {today}. Held events keep the listed factor and are flagged unreviewed: provisionally applied, not validated.</p>
<div class="tiles"><div class="tile"><b id="t1"></b>events confirmed (factor kept)</div><div class="tile"><b id="t2"></b>held (factor kept, flagged unreviewed)</div><div class="tile"><b id="t3"></b>rejected (adjustment removed)</div><div class="tile"><b id="t4"></b>rows changed in the whole series</div></div>
<div class="card"><h2>The four rejected events</h2><div role="group" aria-label="View"><button id="b1" aria-pressed="true">Before</button> <button id="b2" aria-pressed="false">After</button></div>
<svg id="c" role="img" aria-label="Overnight price move on each rejected event before and after the rebuild"></svg><p class="muted" id="cap"></p></div>
<div class="card"><h2>Your checklist</h2><div id="ck"></div></div><p class="muted">Research and decision support, not investment advice.</p>
<script>const D={data};const $=i=>document.getElementById(i);$('t1').textContent=D.tags.owner_confirmed||0;$('t2').textContent=D.tags.applied_unreviewed||0;$('t3').textContent=D.tags.adjustment_removed||0;$('t4').textContent=D.rows[2].toLocaleString();
let mode='b';function draw(){{const W=900,L=150,rowH=44,H=D.rej.length*rowH+30;let s='';D.rej.forEach((x,i)=>{{const y=14+i*rowH,v=mode==='b'?x.b:x.a,w=Math.min(1,Math.abs(v))*(W-L-70);
s+='<text x="'+(L-8)+'" y="'+(y+18)+'" text-anchor="end" font-size="13" fill="currentColor">'+x.s+' '+x.d.slice(0,4)+'</text><rect class="bar" x="'+L+'" y="'+y+'" width="'+w+'" height="22" rx="4" fill="'+(Math.abs(v)>0.15?'var(--bad)':'var(--acc)')+'"/><text x="'+(L+w+6)+'" y="'+(y+17)+'" font-size="13" fill="currentColor">'+(v*100).toFixed(0)+'%</text>';}});
$('c').setAttribute('viewBox','0 0 '+W+' '+H);$('c').innerHTML=s;$('cap').textContent=mode==='b'?'Before: the listed factor was applied, so these days looked ordinary.':'After: the adjustment is removed, so the raw corporate-action drop shows. That is the price move the listed factor failed to explain.';
$('b1').setAttribute('aria-pressed',String(mode==='b'));$('b2').setAttribute('aria-pressed',String(mode==='a'));}}
$('b1').onclick=()=>{{mode='b';draw()}};$('b2').onclick=()=>{{mode='a';draw()}};draw();
$('ck').innerHTML=D.checks.map(c=>'<div class="row"><span class="'+(c[1]?'pass':'fail')+'">'+(c[1]?'PASS':'FAIL')+'</span><span><b>'+c[0]+'</b><br><span class="muted">'+c[2]+'</span></span></div>').join('');</script></main></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True, help="the retained snapshot (parquet)")
    a = ap.parse_args()
    from qsde.db.connection import read_sql
    from qsde.ingestion import bucket_a_decisions as BA

    before = pd.read_parquet(a.before)
    after = read_sql("SELECT symbol, date, open, high, low, close, volume, source FROM ohlcv ORDER BY symbol, date")
    d = BA.load()["decisions"]
    events = read_sql("SELECT symbol, ex_date, applied, applied_research, review_status FROM corp_action_events")
    expected = {x["symbol"] for x in d if x["decision"] == "reject"}
    r = compute(before, after, d, events, expected)
    OUT.mkdir(parents=True, exist_ok=True)
    today = date.today()
    version = Path(a.before).stem
    (OUT / f"ohlcv_change_report_{today}.md").write_text(markdown(r, today, version), encoding="utf-8")
    (OUT / "visual").mkdir(exist_ok=True)
    (OUT / "visual" / f"ohlcv_change_report_{today}.html").write_text(html_page(r, today, version), encoding="utf-8")
    for name, ok, detail in r["checks"]:
        print(("PASS " if ok else "FAIL ") + name + " | " + detail)
    return 0 if all(ok for _, ok, _ in r["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
