import { Link } from "react-router-dom";
import { BucketProfile, Chip, ErrorState, Evidence, PageHead, Skeleton, Tile } from "../components/ui";
import { num, pct, REGIME_LABEL, regimeTone, shortDate, signed } from "../lib/format";
import { useDefensive, useReports, useSystem } from "../lib/queries";

export function Dashboard() {
  const def = useDefensive({ limit: 1 });
  const sys = useSystem();
  const reports = useReports("research");
  const ev = def.data?.evaluation;
  const ew = ev?.schemes.ew;
  const top = ev?.schemes.top_defensive;
  const tilt = ev?.schemes.tilt;

  return (
    <>
      <PageHead title="Dashboard" sub="Where the data, the signal and the research stand today." right={<Evidence kind="research" />} />

      <section className="grid tiles" aria-label="Key figures">
        <Tile label="Market regime" value={def.data?.regime ? REGIME_LABEL[def.data.regime] ?? def.data.regime : "…"} sub="trend + volatility, known at the close" tone={regimeTone(def.data?.regime) === "bad" ? "bad" : regimeTone(def.data?.regime) === "ok" ? "ok" : undefined} />
        <Tile label="Signal as of" value={shortDate(def.data?.as_of)} sub={def.data ? `${Object.values(def.data.bucket_counts).reduce((a, b) => a + b, 0)} names scored` : undefined} />
        <Tile label="Price data through" value={shortDate(sys.data?.ohlcv_latest)} sub="official, corporate-action adjusted" />
        <Tile label="Model promotion" value={sys.data ? (sys.data.promotion_enabled ? "ENABLED" : "Off") : "…"} sub="needs gate + live-paper evidence + owner sign-off" tone={sys.data?.promotion_enabled ? "bad" : "ok"} />
      </section>

      <section className="grid two">
        <div className="card">
          <header><h2>Defensive tilt vs equal-weight</h2><Evidence kind="research" /></header>
          {def.isLoading && <Skeleton rows={4} />}
          {def.isError && <ErrorState error={def.error} onRetry={() => def.refetch()} />}
          {ev && ew && tilt && top && (
            <>
              <div className="table-wrap"><table>
                <thead><tr><th>Portfolio</th><th className="num">Return/yr</th><th className="num">Volatility</th><th className="num">Sharpe</th><th className="num">Max drawdown</th></tr></thead>
                <tbody>
                  {([["Equal-weight", ew], ["Defensive tilt", tilt], ["Top defensive quintile", top]] as const).map(([n, s]) => (
                    <tr key={n}><td>{n}</td><td className="num">{pct(s.ann_return)}</td><td className="num">{pct(s.ann_vol)}</td><td className="num">{num(s.sharpe)}</td><td className="num">{pct(s.max_dd)}</td></tr>
                  ))}
                </tbody>
              </table></div>
              <p className="muted" style={{ marginTop: 10, fontSize: "0.85rem" }}>
                {ev.period[0]} to {ev.period[1]}, monthly rebalance, {ev.one_way_cost_bps} bps one way. The tilt lowers volatility by about {pct(1 - ev.vs_equal_weight.tilt.vol_ratio, 0)} and its Sharpe edge is {signed(ev.vs_equal_weight.tilt.sharpe_diff)} (95% interval {signed(ev.vs_equal_weight.tilt.sharpe_diff_ci[0])} to {signed(ev.vs_equal_weight.tilt.sharpe_diff_ci[1])}).
                For the top quintile the Sharpe difference is {signed(ev.vs_equal_weight.top_defensive.sharpe_diff)} ({signed(ev.vs_equal_weight.top_defensive.sharpe_diff_ci[0])} to {signed(ev.vs_equal_weight.top_defensive.sharpe_diff_ci[1])}): the risk reduction is clear, a return improvement is not established.
              </p>
            </>
          )}
        </div>

        <div className="card">
          <header><h2>What each defensive bucket looks like</h2><Link to="/defensive">Open signal</Link></header>
          {def.isLoading ? <Skeleton rows={5} /> : def.data ? <BucketProfile profile={def.data.bucket_profile} /> : null}
        </div>
      </section>

      <section className="grid two">
        <div className="card">
          <header><h2>What the research has and has not shown</h2><Evidence kind="research" /></header>
          <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
            <li>The predictive model's score was almost entirely a <strong>low-volatility exposure</strong>; it did not beat a simple momentum rule net of costs.</li>
            <li>At a 5-day horizon nothing was profitable after realistic costs; at 20 days the returns were small and regime-dependent.</li>
            <li>So Stoxsy offers a <strong>risk-aware allocation signal</strong>, not a stock-picking forecast. No model is approved.</li>
          </ul>
          <p style={{ marginTop: 10 }}><Link to="/research">Read the reports</Link></p>
        </div>
        <div className="card">
          <header><h2>Latest research reports</h2></header>
          {reports.isLoading ? <Skeleton rows={4} /> : reports.isError ? <ErrorState error={reports.error} onRetry={() => reports.refetch()} /> : (
            <ul style={{ margin: 0, paddingLeft: "1.1em" }}>
              {reports.data?.filter((r) => !r.superseded).slice(0, 5).map((r) => <li key={r.name}><Link to={`/research/${r.group}/${r.name}`}>{r.title.replace(/^RESEARCH_ONLY[:\s]*/, "")}</Link></li>)}
              {reports.data?.length === 0 && <li className="muted">None yet.</li>}
            </ul>
          )}
        </div>
      </section>

      {sys.data && sys.data.notes.length > 0 && (
        <div className="notice" role="status">{sys.data.notes.map((n) => <div key={n}><Chip tone="warn">NOTE</Chip> {n}</div>)}</div>
      )}
    </>
  );
}
