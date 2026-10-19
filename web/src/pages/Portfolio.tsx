import { useRef, useState } from "react";
import { LineChart } from "../components/LineChart";
import { BUCKET_LABEL, Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton, Tile } from "../components/ui";
import { dash, num, pct, shortDate, signedPct } from "../lib/format";
import { useImportHoldings, usePortfolio } from "../lib/queries";

const inr = (x: number | null | undefined) => (x == null ? dash : `₹${Math.round(x).toLocaleString("en-IN")}`);
const sinr = (x: number) => `${x < 0 ? "-" : "+"}₹${Math.abs(Math.round(x)).toLocaleString("en-IN")}`;
const LOOKBACKS = [{ value: 180, label: "6 months" }, { value: 365, label: "1 year" }, { value: 730, label: "2 years" }];
const BENCH = [{ value: "NIFTYBEES", label: "Nifty 50" }, { value: "JUNIORBEES", label: "Next 50" }, { value: "BANKBEES", label: "Bank Nifty" }, { value: "none", label: "None" }];

export function Portfolio() {
  const [bench, setBench] = useState("NIFTYBEES");
  const [days, setDays] = useState(365);
  const [text, setText] = useState("");
  const [fname, setFname] = useState<string | undefined>();
  const fileRef = useRef<HTMLInputElement>(null);
  const q = usePortfolio(bench, days);
  const imp = useImportHoldings();
  const s = q.data;
  const h = s?.history;

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    if (f.size > 1_000_000) { setText(""); return; }
    setFname(f.name);
    setText(await f.text());
  };
  const submit = () => imp.mutate({ csv_text: text, filename: fname }, { onSuccess: () => { setText(""); setFname(undefined); if (fileRef.current) fileRef.current.value = ""; } });

  return (
    <>
      <PageHead title="My portfolio" sub="Import your holdings to see allocation, concentration, P&L and how much the portfolio has fallen from its peak." right={<Evidence kind="diagnostic" />} />
      <div className="notice">
        Your holdings stay in your own account: they are visible only to you, nothing is sent anywhere, and nothing here can place an order. P&amp;L is unrealised and uses your file&apos;s average cost.
        The history is <strong>pro-forma</strong> (today&apos;s quantities held constant), not your actual past value.
      </div>

      <div className="card">
        <header><h2>{s ? "Replace holdings" : "Import holdings"}</h2></header>
        <p className="muted" style={{ marginTop: 0, fontSize: "0.9rem" }}>
          CSV from Kite (<code>Instrument, Qty., Avg. cost</code>), Zerodha console (<code>Symbol, ISIN, Quantity Available, Average Price</code>) or a simple <code>symbol, quantity, avg_cost</code> file.
          NSE-listed equities and ETFs are supported; mutual funds, bonds and BSE-only names are reported and skipped.
        </p>
        <div className="field">
          <label htmlFor="hf">Holdings file</label>
          <input id="hf" ref={fileRef} className="input" type="file" accept=".csv,text/csv,text/plain" onChange={(e) => void onFile(e.target.files?.[0])} />
        </div>
        <div className="field">
          <label htmlFor="ht">…or paste CSV text</label>
          <textarea id="ht" className="input" rows={4} value={text} onChange={(e) => setText(e.target.value)} placeholder={"symbol,quantity,avg_cost\nTCS,10,3500"} style={{ fontFamily: "var(--font-mono)" }} />
        </div>
        <button className="btn primary" disabled={!text.trim() || imp.isPending} onClick={submit}>{imp.isPending ? "Importing…" : "Import"}</button>
        {imp.isError && <p role="alert" style={{ color: "var(--down)" }}>{imp.error instanceof Error ? imp.error.message : "Import failed"}</p>}
        {imp.data && (
          <div role="status" style={{ marginTop: 12 }}>
            <strong>{imp.data.n_accepted} holdings imported</strong>{imp.data.n_rejected > 0 && `, ${imp.data.n_rejected} rejected`}.
            {imp.data.issues.length > 0 && (
              <ul style={{ margin: "6px 0 0", paddingLeft: "1.2em" }}>
                {imp.data.issues.map((i, k) => <li key={k}><Chip tone={i.severity === "rejected" ? "bad" : "warn"}>{i.severity}</Chip> {i.symbol && <strong>{i.symbol}: </strong>}{i.reason} <span className="muted">(row {i.row})</span></li>)}
              </ul>
            )}
          </div>
        )}
      </div>

      {q.isLoading && <Skeleton rows={6} />}
      {q.isError && <ErrorState error={q.error} onRetry={() => q.refetch()} />}
      {q.isSuccess && !s && <Empty>No holdings imported yet. Import a file above to see your portfolio.</Empty>}
      {s && (
        <>
          <section className="grid tiles" aria-label="Portfolio summary">
            <Tile label="Value" value={inr(s.totals.value)} sub={`${s.totals.n_holdings} holdings · prices as of ${shortDate(s.as_of)}`} />
            <Tile label="Unrealised P&L" value={sinr(s.totals.pnl)} tone={s.totals.pnl < 0 ? "bad" : "ok"} sub={`${signedPct(s.totals.pnl_pct)} on cost ${inr(s.totals.cost)} · ${s.totals.winners} up, ${s.totals.losers} down`} />
            <Tile label="Effective positions" value={s.concentration.effective_n == null ? dash : num(s.concentration.effective_n, 1)} sub={`largest ${pct(s.concentration.top1)} · top 5 ${pct(s.concentration.top5)}`} />
            <Tile label="Now vs 1-year peak" value={h ? signedPct(h.current_drawdown) : dash} tone={h && h.current_drawdown < -0.1 ? "bad" : ""} sub={h ? `worst fall in window ${signedPct(h.max_drawdown)}` : "needs price history"} />
            <Tile label="Typical bad day (1-in-20)" value={h ? `-${pct(h.var95_1d, 2)}` : dash} sub={h ? `worst day in window ${signedPct(h.worst_day, 2)}` : undefined} />
          </section>

          {s.notes.length > 0 && <div className="notice" role="status"><ul style={{ margin: 0, paddingLeft: "1.2em" }}>{s.notes.map((n) => <li key={n}>{n}</li>)}</ul></div>}

          <div className="card">
            <header>
              <h2>Pro-forma history</h2>
              <Segmented label="Window" value={days} onChange={setDays} options={LOOKBACKS} />
              <Segmented label="Benchmark" value={bench} onChange={setBench} options={BENCH} />
            </header>
            {!h ? <Empty>Not enough overlapping price history to draw this.</Empty> : (
              <div className="grid two">
                <div>
                  <h3 style={{ marginTop: 0 }}>Value, rebased to 100</h3>
                  <LineChart label="Pro-forma portfolio value rebased to 100, with the benchmark" dates={h.series.date} format={(v) => v.toFixed(0)}
                    series={[{ name: "Your holdings", values: h.series.value.map((v) => v * 100), color: "var(--brand)" },
                      ...(h.benchmark ? [{ name: "Benchmark", values: h.benchmark.series.value.map((v) => v * 100), color: "var(--muted)" }] : [])]} />
                </div>
                <div>
                  <h3 style={{ marginTop: 0 }}>Fall from the running peak</h3>
                  <LineChart label="Pro-forma drawdown from the running peak" dates={h.series.date} zero format={(v) => `${(v * 100).toFixed(0)}%`}
                    series={[{ name: "Your holdings", values: h.series.drawdown, color: "var(--down)" }]} />
                </div>
              </div>
            )}
            {h && (
              <p className="muted" style={{ fontSize: "0.85rem" }}>
                {shortDate(h.start)} to {shortDate(h.end)} ({h.sessions} sessions, {h.names_priced} names). Your holdings: {signedPct(h.total_return)}, volatility {pct(h.ann_volatility)} a year, worst fall {signedPct(h.max_drawdown)}.
                {h.benchmark && ` ${s.benchmark_label}: ${signedPct(h.benchmark.total_return)}, volatility ${pct(h.benchmark.ann_volatility)}, worst fall ${signedPct(h.benchmark.max_drawdown)}; beta ${h.benchmark.beta == null ? dash : num(h.benchmark.beta)}, correlation ${h.benchmark.correlation == null ? dash : num(h.benchmark.correlation)}.`}
              </p>
            )}
          </div>

          <div className="card">
            <header><h2>Holdings</h2><Chip>{`import #${s.import_id} · ${shortDate(s.imported_at)}`}</Chip></header>
            <div className="table-wrap" style={{ maxHeight: 520 }}>
              <table>
                <thead><tr><th>Symbol</th><th className="num">Qty</th><th className="num">Avg cost</th><th className="num">Last</th><th className="num">Value</th><th className="num">Weight</th><th className="num">P&amp;L</th><th>Defensive bucket</th></tr></thead>
                <tbody>
                  {s.holdings.map((r) => (
                    <tr key={r.symbol}>
                      <td><strong>{r.symbol}</strong>{r.stale && <> <Chip tone="warn" title={`last price ${r.price_date}`}>stale price</Chip></>}</td>
                      <td className="num">{r.quantity.toLocaleString("en-IN")}</td>
                      <td className="num">{num(r.avg_cost)}</td>
                      <td className="num">{num(r.last_price)}</td>
                      <td className="num">{inr(r.value)}</td>
                      <td className="num">{pct(r.weight)}{s.concentration.above_limit.some((a) => a.symbol === r.symbol) && <> <Chip tone="warn" title={`above the ${pct(s.concentration.limit, 0)} single-name cap used by the decision cards`}>&gt;{pct(s.concentration.limit, 0)}</Chip></>}</td>
                      <td className="num" style={{ color: (r.pnl ?? 0) < 0 ? "var(--down)" : "var(--up)" }}>{r.pnl == null ? dash : `${sinr(r.pnl)} (${signedPct(r.pnl_pct)})`}</td>
                      <td>{r.defensive_bucket == null ? <span className="muted">not scored</span> : `${r.defensive_bucket} · ${BUCKET_LABEL[r.defensive_bucket]}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid two">
            <div className="card">
              <header><h2>Concentration</h2></header>
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
                <li>{s.concentration.n_positions} positions; the largest is {pct(s.concentration.top1)}, the top 3 are {pct(s.concentration.top3)}, the top 10 are {pct(s.concentration.top10)}.</li>
                <li>Effective number of equal-sized positions: {s.concentration.effective_n == null ? dash : num(s.concentration.effective_n, 1)} (a lower number means fewer names carry the risk).</li>
                <li>{s.concentration.above_limit.length === 0 ? `No name is above ${pct(s.concentration.limit, 0)}.` : `${s.concentration.above_limit.length} name(s) above the ${pct(s.concentration.limit, 0)} single-name cap the decision cards use: ${s.concentration.above_limit.map((a) => `${a.symbol} ${pct(a.weight)}`).join(", ")}.`}</li>
              </ul>
            </div>
            <div className="card">
              <header><h2>Defensive profile</h2></header>
              {!s.defensive.available ? <Empty>No defensive signal computed yet.</Empty> : (
                <>
                  <table>
                    <thead><tr><th>Bucket</th><th className="num">Share of value</th><th>Names</th></tr></thead>
                    <tbody>
                      {Object.entries(s.defensive.buckets).sort((a, b) => (a[0] === "not scored" ? -1 : b[0] === "not scored" ? 1 : Number(b[0]) - Number(a[0]))).map(([b, v]) => (
                        <tr key={b}><td>{b === "not scored" ? "Not scored" : `${b} · ${BUCKET_LABEL[Number(b)]}`}</td><td className="num">{pct(v.share)}</td><td className="wrap">{v.names.join(", ")}</td></tr>
                      ))}
                    </tbody>
                  </table>
                  <p className="muted" style={{ fontSize: "0.82rem" }}>{pct(s.defensive.scored_share, 0)} of the portfolio is inside the signal&apos;s universe (top 500 NSE equities by traded value). Bucket 5 = lowest recent risk; it is a risk label, not a forecast.</p>
                </>
              )}
            </div>
          </div>
          <p className="muted" style={{ fontSize: "0.82rem" }}>{s.disclaimer}</p>
        </>
      )}
    </>
  );
}
