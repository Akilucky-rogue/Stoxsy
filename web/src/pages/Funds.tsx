import { useMemo, useState } from "react";
import { BASIS_SHORT, ExpenseCell, FundDetailPanel, QualityBadge, RetCell } from "../components/FundDetail";
import { Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton } from "../components/ui";
import { num, pct, shortDate } from "../lib/format";
import { FUND_PAGE, useFundFacets, useFunds, type FundParams } from "../lib/queries";
import type { FundRow } from "../lib/api";

const CLASSES = [
  { value: "", label: "All" }, { value: "equity", label: "Equity" }, { value: "debt", label: "Debt" }, { value: "hybrid", label: "Hybrid" },
  { value: "commodity", label: "Gold & silver" }, { value: "international", label: "International" }, { value: "solution", label: "Solution" },
];
const SORTS = [
  { value: "name", label: "Name" }, { value: "ret_1y", label: "1Y return" }, { value: "ret_3y", label: "3Y return" }, { value: "ret_5y", label: "5Y return" },
  { value: "vol_3y", label: "Volatility" }, { value: "maxdd_3y", label: "Worst fall" }, { value: "ter_base", label: "Expense (base)" }, { value: "ter_total", label: "Expense (total)" },
];
const lbl = (s: string) => s.replace(/_/g, " ");

export function Funds() {
  const [p, setP] = useState<FundParams>({ q: "", assetClass: "", amc: "", fundGroup: "", category: "", plan: "direct", option: "growth", status: "active", quality: "", sort: "name", order: "asc", offset: 0 });
  const [sel, setSel] = useState<FundRow | null>(null);
  const set = (patch: Partial<FundParams>) => setP((x) => ({ ...x, offset: 0, ...patch }));
  const f = useFunds(p);
  const fx = useFundFacets();
  const cats = useMemo(() => (fx.data?.categories ?? []).filter((c) => !p.assetClass || c.asset_class === p.assetClass), [fx.data, p.assetClass]);
  const total = f.data?.n_matching ?? 0;

  return (
    <>
      <PageHead
        title="Mutual funds & ETFs"
        sub="Scheme master and NAV-based returns and risk from AMFI. A separate asset class: nothing here feeds the stock signal or decision cards."
        right={<Evidence kind="diagnostic" />}
      />
      <div className="notice">
        Every figure carries its basis: <strong>total return after expenses</strong> (growth options), <strong>price return only</strong> (IDCW / payout lines: payouts are not in the NAV file) or <strong>ETF price return</strong>.
        Returns up to 6 months are cumulative; 1Y, 3Y and 5Y are per year. A missing figure says why (insufficient history, a unit change inside the window) and is never extrapolated.
        The <strong>Expense</strong> column is the base expense ratio from AMFI's monthly files (hover for the total as published; the detail panel shows every part). Holdings overlap and AUM are not loaded. Past performance is not a forecast or advice.
      </div>

      <div className="card">
        <header>
          <label className="sr-only" htmlFor="fq">Search funds</label>
          <input id="fq" className="input" style={{ maxWidth: 260 }} placeholder="Search fund or fund house" value={p.q} onChange={(e) => set({ q: e.target.value })} />
          <Segmented label="Asset class" value={p.assetClass} onChange={(v) => set({ assetClass: v, category: "" })} options={CLASSES} />
        </header>
        <div className="grid tiles" style={{ alignItems: "end" }}>
          <div className="field"><label htmlFor="famc">Fund house</label>
            <select id="famc" className="input" value={p.amc} onChange={(e) => set({ amc: e.target.value })}>
              <option value="">All fund houses</option>{(fx.data?.amcs ?? []).map((a) => <option key={a.name} value={a.name}>{a.name} ({a.n})</option>)}
            </select></div>
          <div className="field"><label htmlFor="fcat">Category</label>
            <select id="fcat" className="input" value={p.category} onChange={(e) => set({ category: e.target.value })}>
              <option value="">All categories</option>{cats.map((c) => <option key={`${c.asset_class}${c.name}`} value={c.name}>{c.name} ({c.n})</option>)}
            </select></div>
          <div className="field"><label htmlFor="fgrp">Vehicle</label>
            <select id="fgrp" className="input" value={p.fundGroup} onChange={(e) => set({ fundGroup: e.target.value })}>
              <option value="">All vehicles</option>{(fx.data?.fund_groups ?? []).map((g) => <option key={g.name} value={g.name}>{lbl(g.name)} ({g.n})</option>)}
            </select></div>
          <div className="field"><label htmlFor="fq2">Data quality</label>
            <select id="fq2" className="input" value={p.quality} onChange={(e) => set({ quality: e.target.value })}>
              <option value="">Any</option><option value="ok">Data ok</option><option value="caution">Caution</option><option value="poor">Poor</option>
            </select></div>
          <div className="field"><label htmlFor="fsort">Sort by</label>
            <select id="fsort" className="input" value={`${p.sort}:${p.order}`} onChange={(e) => { const [sort, order] = e.target.value.split(":"); set({ sort, order }); }}>
              {SORTS.flatMap((s) => (s.value === "name" ? [{ ...s, o: "asc" }] : [{ ...s, o: "desc" }, { ...s, o: "asc" }])).map((s) => (
                <option key={`${s.value}${s.o}`} value={`${s.value}:${s.o}`}>{s.label}{s.value === "name" ? "" : s.o === "desc" ? " (high first)" : " (low first)"}</option>
              ))}
            </select></div>
        </div>
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Segmented label="Plan" value={p.plan} onChange={(v) => set({ plan: v })} options={[{ value: "", label: "Any plan" }, { value: "direct", label: "Direct" }, { value: "regular", label: "Regular" }]} />
          <Segmented label="Option" value={p.option} onChange={(v) => set({ option: v })} options={[{ value: "growth", label: "Growth" }, { value: "idcw", label: "IDCW / payout" }, { value: "etf", label: "ETFs" }, { value: "any", label: "All" }]} />
          <Segmented label="Status" value={p.status} onChange={(v) => set({ status: v })} options={[{ value: "active", label: "Active" }, { value: "all", label: "Incl. closed" }]} />
        </div>
      </div>

      {sel && <FundDetailPanel fund={sel} onClose={() => setSel(null)} />}

      <div className="card">
        {f.isLoading && <Skeleton rows={6} />}
        {f.isError && <ErrorState error={f.error} onRetry={() => f.refetch()} />}
        {f.data && (f.data.rows.length === 0 ? <Empty>No schemes match these filters.</Empty> : (
          <>
            <p className="muted" style={{ fontSize: "0.85rem", marginTop: 0 }}>
              {total.toLocaleString("en-IN")} matching schemes; showing {p.offset + 1}–{Math.min(p.offset + FUND_PAGE, total)}. Select a row for charts, every horizon and the peer context.
            </p>
            <div className="table-wrap" style={{ maxHeight: 620 }}>
              <table>
                <thead>
                  <tr>
                    <th>Scheme</th><th>Basis</th><th className="num">NAV</th><th className="num">3M</th><th className="num">6M</th>
                    <th className="num">1Y /yr</th><th className="num">3Y /yr</th><th className="num">5Y /yr</th><th className="num">Volatility</th><th className="num">Worst fall</th><th className="num" title="Base expense ratio, from AMFI. Hover a figure for the total as published.">Expense</th><th className="num">1Y vs peers</th><th>Data</th>
                  </tr>
                </thead>
                <tbody>
                  {f.data.rows.map((r) => {
                    const pr = r.peers?.horizons["1Y"];
                    return (
                      <tr key={r.scheme_code} tabIndex={0} aria-selected={sel?.scheme_code === r.scheme_code} style={{ cursor: "pointer" }}
                        onClick={() => setSel(r)} onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setSel(r); } }}>
                        <td className="wrap"><strong>{r.name}</strong><div className="muted" style={{ fontSize: "0.78rem" }}>{r.amc ?? ""} · {r.category ?? ""} · code {r.scheme_code}</div></td>
                        <td className="wrap" style={{ fontSize: "0.8rem" }}>{r.basis ? <Chip title={r.basis_text ?? undefined}>{BASIS_SHORT[r.basis]}</Chip> : <span className="muted">{r.lifecycle === "inactive_no_recent_nav" ? "closed / no recent NAV" : "no snapshot"}</span>}</td>
                        <td className="num">{num(r.last_nav, 4)}<div className="muted" style={{ fontSize: "0.72rem" }}>{shortDate(r.last_nav_date)}</div></td>
                        <td className="num"><RetCell c={r.returns["3M"]} annual={false} /></td><td className="num"><RetCell c={r.returns["6M"]} annual={false} /></td>
                        <td className="num"><RetCell c={r.returns["1Y"]} annual /></td><td className="num"><RetCell c={r.returns["3Y"]} annual /></td><td className="num"><RetCell c={r.returns["5Y"]} annual />
                        </td>
                        <td className="num">{r.risk.status === "ok" ? pct(r.risk.ann_volatility) : <span className="muted" title={r.risk.detail}>n/a</span>}</td>
                        <td className="num">{r.risk.status === "ok" ? pct(r.risk.max_drawdown) : <span className="muted" title={r.risk.detail}>n/a</span>}</td>
                        <td className="num"><ExpenseCell e={r.expense} /></td>
                        <td className="num">{pr?.percentile == null ? <span className="muted" title="fewer than 8 like-for-like peers, or not comparable">—</span> : <span title={`ahead of ${pct(pr.percentile, 0)} of ${pr.n} peers`}>{pct(pr.percentile, 0)}</span>}</td>
                        <td><QualityBadge q={r.quality} /></td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
              <button className="btn" disabled={p.offset === 0} onClick={() => setP((x) => ({ ...x, offset: Math.max(0, x.offset - FUND_PAGE) }))}>Previous</button>
              <button className="btn" disabled={p.offset + FUND_PAGE >= total} onClick={() => setP((x) => ({ ...x, offset: x.offset + FUND_PAGE }))}>Next</button>
            </div>
            <p className="muted" style={{ fontSize: "0.8rem" }}>{f.data.methodology}</p>
          </>
        ))}
      </div>
    </>
  );
}
