import { useState } from "react";
import type { FundRow, ReturnCell } from "../lib/api";
import { dash, num, pct, shortDate, signedPct } from "../lib/format";
import { useFundDetail } from "../lib/queries";
import { LineChart } from "./LineChart";
import { Chip, ErrorState, Segmented, Skeleton } from "./ui";

export const STATUS_TEXT: Record<string, string> = {
  insufficient_history: "insufficient history",
  stale_start: "start NAV too old",
  nav_discontinuity: "unit change in window",
};
export const BASIS_SHORT: Record<string, string> = {
  total_return_after_expenses: "total return after expenses",
  price_return_only: "price return only (IDCW)",
  price_return: "ETF price return",
  option_unknown: "option unknown",
};
export const QUALITY_TONE = { ok: "ok", caution: "warn", poor: "bad" } as const;

export function QualityBadge({ q }: { q: FundRow["quality"] }) {
  if (!q) return <Chip>no snapshot</Chip>;
  return <Chip tone={QUALITY_TONE[q.level]} title={q.reasons.length ? q.reasons.join("\n") : "No data-quality issues found"}>{q.level === "ok" ? "data ok" : q.level === "caution" ? "data caution" : "data poor"}</Chip>;
}

export function RetCell({ c, annual }: { c: ReturnCell | undefined; annual: boolean }) {
  if (!c) return <span className="muted" title="no figure computed">{dash}</span>;
  if (c.status !== "ok") return <span className="muted" title={c.detail}>n/a <span style={{ fontSize: "0.7rem" }}>({STATUS_TEXT[c.status]})</span></span>;
  const v = annual && c.annualised != null ? c.annualised : c.cumulative;
  return <span style={{ color: v < 0 ? "var(--down)" : undefined }}>{signedPct(v)}</span>;
}

const HORIZONS = ["1D", "1W", "1M", "3M", "6M", "1Y", "3Y", "5Y"];

export function FundDetailPanel({ fund, onClose }: { fund: FundRow; onClose: () => void }) {
  const [win, setWin] = useState("3y");
  const d = useFundDetail(fund.scheme_code, win);
  const f = d.data?.fund ?? fund;
  const r = f.risk;
  return (
    <div className="card" aria-label={`Details for ${f.name}`}>
      <header>
        <h2 style={{ marginRight: "auto" }}>{f.name}</h2>
        <QualityBadge q={f.quality} />
        <button className="btn" onClick={onClose}>Close</button>
      </header>
      <p className="muted" style={{ marginTop: 0 }}>
        {f.amc} · {f.category ?? "uncategorised"} · {f.asset_class} · {f.fund_group?.replace(/_/g, " ")} · {f.plan ?? "plan n/a"} · {f.option_type ?? "no option"} · code {f.scheme_code}
        {f.last_nav != null && <> · NAV {num(f.last_nav, 4)} on {shortDate(f.last_nav_date)}</>}
      </p>
      <p style={{ marginTop: 0 }}><strong>Return basis:</strong> {f.basis_text ?? "no snapshot (inactive scheme)"}</p>
      {f.quality && f.quality.reasons.length > 0 && (
        <div className="notice" role="status"><strong>Data quality:</strong><ul style={{ margin: "4px 0 0", paddingLeft: "1.2em" }}>{f.quality.reasons.map((x) => <li key={x}>{x}</li>)}</ul></div>
      )}

      <div className="grid two" style={{ marginTop: 12 }}>
        <div>
          <h3 style={{ marginTop: 0 }}>Returns</h3>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Horizon</th><th className="num">Cumulative</th><th className="num">Per year</th><th>From → to</th></tr></thead>
              <tbody>
                {HORIZONS.map((h) => {
                  const c = f.returns[h];
                  return (
                    <tr key={h}>
                      <td>{h}</td>
                      {c && c.status === "ok" ? (
                        <><td className="num">{signedPct(c.cumulative)}</td><td className="num">{c.annualised == null ? <span className="muted">—</span> : signedPct(c.annualised)}</td><td>{shortDate(c.start)} → {shortDate(c.end)}</td></>
                      ) : (
                        <td colSpan={3} className="muted">{c ? `${STATUS_TEXT[c.status]}: ${c.detail}` : "not computed"}</td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <h3>Risk (last ~3 years, daily NAV)</h3>
          {r.status === "ok" ? (
            <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
              <li>Volatility {pct(r.ann_volatility)} a year; downside deviation {pct(r.downside_deviation)}.</li>
              <li>Worst fall from a peak {signedPct(r.max_drawdown)}; now {signedPct(r.current_drawdown)} from its peak in the window.</li>
              <li className="muted">{r.observations} daily changes, {shortDate(r.start)} to {shortDate(r.end)}.</li>
            </ul>
          ) : <p className="muted">Not shown: {r.detail ?? "no risk figures"}.</p>}
          <h3>Peer context</h3>
          {f.peers ? (
            <>
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                {Object.entries(f.peers.horizons).map(([h, x]) => (
                  <li key={h}>{h}: ahead of {x.percentile == null ? dash : pct(x.percentile, 0)} of {x.n} like-for-like peers; peer median {signedPct(x.median)} a year.</li>
                ))}
              </ul>
              <p className="muted" style={{ fontSize: "0.82rem" }}>Peers: {f.peers.key.replace(/\|/g, " · ")} ({f.peers.group_size} schemes with a clean data-quality badge). Descriptive only: not a recommendation.</p>
            </>
          ) : <p className="muted">No peer context: fewer than 8 comparable schemes with a valid figure, or the line is not comparable (IDCW / unidentified option).</p>}
        </div>

        <div>
          <header style={{ marginBottom: 6 }}><h3 style={{ margin: 0 }}>NAV, rebased to 100</h3><Segmented label="Window" value={win} onChange={setWin} options={[{ value: "1y", label: "1Y" }, { value: "3y", label: "3Y" }, { value: "5y", label: "5Y" }, { value: "max", label: "Max" }]} /></header>
          {d.isLoading && <Skeleton rows={4} />}
          {d.isError && <ErrorState error={d.error} onRetry={() => d.refetch()} />}
          {d.data && (d.data.series ? (
            <>
              <LineChart label={`${f.name} NAV rebased to 100`} dates={d.data.series.dates} format={(v) => v.toFixed(0)}
                series={[{ name: "This scheme", values: d.data.series.value, color: "var(--brand)" }, ...(d.data.benchmark ? [{ name: "Nifty 50 ETF proxy", values: d.data.benchmark.value, color: "var(--muted)" }] : [])]} />
              {d.data.benchmark && <p className="muted" style={{ fontSize: "0.82rem" }}>{d.data.benchmark.label}.</p>}
              <h3>Fall from the running peak</h3>
              <LineChart label={`${f.name} drawdown`} dates={d.data.series.dates} zero format={(v) => `${(v * 100).toFixed(0)}%`} series={[{ name: "This scheme", values: d.data.series.drawdown, color: "var(--down)" }]} />
              <p className="muted" style={{ fontSize: "0.82rem" }}>
                {f.basis === "price_return_only" && "IDCW lines show price return only: payouts are not in the NAV. "}A line starts after the most recent unit change, if there was one.
              </p>
            </>
          ) : <p className="muted">Not enough NAV history in this window to draw a line.</p>)}
          {d.data && (d.data.events.unit_changes.length > 0 || d.data.events.gaps_over_30d.length > 0) && (
            <details>
              <summary>NAV events ({d.data.events.unit_changes.length} unit changes, {d.data.events.gaps_over_30d.length} gaps over 30 days)</summary>
              <ul>
                {d.data.events.unit_changes.map((e) => <li key={e.date}>{shortDate(e.date)}: one-day NAV move of {signedPct(e.move, 0)} (unit change or bad print).</li>)}
                {d.data.events.gaps_over_30d.map((e) => <li key={e.from}>No NAV from {shortDate(e.from)} to {shortDate(e.to)} ({e.days} days).</li>)}
              </ul>
            </details>
          )}
        </div>
      </div>
    </div>
  );
}
