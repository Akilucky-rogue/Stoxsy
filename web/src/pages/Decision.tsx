import { useState } from "react";
import { Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton, Tile } from "../components/ui";
import { num, pct, REGIME_LABEL, shortDate } from "../lib/format";
import { useDecision, type DecisionParams } from "../lib/queries";

const inr = (x: number) => `₹${Math.round(x).toLocaleString("en-IN")}`;

export function Decision() {
  const [capital, setCapital] = useState(1_500_000);
  const [risk, setRisk] = useState(0.5);
  const [horizon, setHorizon] = useState("position_20d");
  const [maxPositions, setMax] = useState(20);
  const [minBucket, setBucket] = useState(4);
  const params: DecisionParams = { capital, risk: risk / 100, horizon, maxPositions, minBucket };
  const q = useDecision(params);
  const s = q.data?.summary;

  return (
    <>
      <PageHead
        title="Decision cards"
        sub="What to hold, where it trades, how much, and until when - sized so a stop-out costs only what you choose to risk."
        right={<Evidence kind="research" />}
      />
      <div className="notice">
        <strong>This is risk-based allocation, not a forecast.</strong> No model in this system has shown a net edge, so nothing here claims a stock will rise.
        The names are the lowest-risk liquid ones today; the size, stop and time limit are mechanical risk controls. Dates are estimates (exchange holidays
        are not known in advance). Nothing is placed for you.
      </div>

      <div className="card">
        <header><h2>Your settings</h2></header>
        <div className="grid tiles" style={{ alignItems: "end" }}>
          <div className="field">
            <label htmlFor="cap">Capital (₹)</label>
            <input id="cap" className="input" type="number" min={50000} step={50000} value={capital} onChange={(e) => setCapital(Math.max(50000, Number(e.target.value) || 50000))} />
          </div>
          <div className="field">
            <label htmlFor="rk">Risk per position (% of capital)</label>
            <input id="rk" className="input" type="number" min={0.1} max={3} step={0.1} value={risk} onChange={(e) => setRisk(Math.min(3, Math.max(0.1, Number(e.target.value) || 0.5)))} />
          </div>
          <div className="field">
            <label htmlFor="mp">Max positions</label>
            <input id="mp" className="input" type="number" min={1} max={60} value={maxPositions} onChange={(e) => setMax(Math.min(60, Math.max(1, Number(e.target.value) || 20)))} />
          </div>
          <div className="field">
            <label>Holding horizon</label>
            <Segmented label="Horizon" value={horizon} onChange={setHorizon} options={[{ value: "swing_5d", label: "5 sessions" }, { value: "position_20d", label: "20 sessions" }]} />
          </div>
          <div className="field">
            <label>Minimum defensive bucket</label>
            <Segmented label="Minimum bucket" value={minBucket} onChange={setBucket} options={[{ value: 3, label: "3+" }, { value: 4, label: "4+" }, { value: 5, label: "5 only" }]} />
          </div>
        </div>
      </div>

      {q.isLoading && <Skeleton rows={6} />}
      {q.isError && <ErrorState error={q.error} onRetry={() => q.refetch()} />}
      {s && q.data && (
        <>
          <section className="grid tiles" aria-label="Plan summary">
            <Tile label="Invested" value={pct(s.invested_pct, 0)} sub={`${inr(s.invested)} of ${inr(s.capital)} (${REGIME_LABEL[s.regime] ?? s.regime} cap ${pct(s.exposure_cap, 0)})`} />
            <Tile label="Positions" value={String(s.positions)} sub={`from ${s.candidates_considered} eligible; largest ${pct(s.largest_weight, 1)}`} />
            <Tile label="If every stop is hit" value={`-${pct(s.loss_if_all_stopped_pct, 1)}`} tone="bad" sub={`${inr(s.loss_if_all_stopped)} (gaps can be worse)`} />
            <Tile label="Hold until" value={shortDate(s.exit_by)} sub={`${s.horizon_sessions} sessions after entry ${shortDate(s.entry_date)} (estimate)`} />
            <Tile label="Average pairwise correlation" value={s.average_pairwise_correlation == null ? "—" : num(s.average_pairwise_correlation)} sub="lower = less doubled-up risk" />
          </section>

          <div className="card">
            <header>
              <h2>Positions as of {shortDate(s.as_of)} close</h2>
              <Chip>{`entry: next session open · exit by ${shortDate(s.exit_by)}`}</Chip>
            </header>
            {q.data.cards.length === 0 ? <Empty>No names pass the filters.</Empty> : (
              <div className="table-wrap" style={{ maxHeight: 640 }}>
                <table>
                  <thead>
                    <tr>
                      <th>What</th><th>Where</th><th className="num">Shares</th><th className="num">Value</th><th className="num">Weight</th><th className="num">Last close</th>
                      <th className="num">Stop</th><th className="num">Loss if stopped</th><th>Review by</th><th>Exit by</th><th>Sized by</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.data.cards.map((c) => (
                      <tr key={c.symbol} title={c.reasons.join(" · ")}>
                        <td><strong>{c.symbol}</strong><div className="muted" style={{ fontSize: "0.78rem" }}>bucket {c.risk_bucket} · vol {pct(c.daily_vol)}/day</div></td>
                        <td>{c.venues.join(", ")} · cash equity</td>
                        <td className="num">{c.quantity.toLocaleString("en-IN")}</td>
                        <td className="num">{inr(c.value)}</td>
                        <td className="num">{pct(c.weight)}</td>
                        <td className="num">{num(c.last_close)}</td>
                        <td className="num">{num(c.stop)}</td>
                        <td className="num">{inr(c.loss_if_stopped)} ({pct(c.loss_pct_of_capital, 2)})</td>
                        <td>{shortDate(c.review_by)}</td>
                        <td>{shortDate(c.exit_by)}</td>
                        <td><Chip>{c.sized_by}</Chip></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="grid two">
            <div className="card">
              <header><h2>When to act on a position</h2></header>
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>{(q.data.cards[0]?.triggers ?? []).map((t) => <li key={t}>{t}</li>)}</ul>
              <p className="muted" style={{ fontSize: "0.85rem", marginTop: 10 }}>Hover a row to see why it was chosen.</p>
            </div>
            <div className="card">
              <header><h2>Limits of this plan</h2></header>
              <ul style={{ margin: 0, paddingLeft: "1.2em" }}>{s.notes.map((n) => <li key={n}>{n}</li>)}</ul>
              {s.skipped.length > 0 && (
                <details style={{ marginTop: 10 }}>
                  <summary>Skipped names ({s.skipped.length})</summary>
                  <ul>{s.skipped.map(([sym, why]) => <li key={sym}><strong>{sym}</strong>: {why}</li>)}</ul>
                </details>
              )}
            </div>
          </div>
          <p className="muted" style={{ fontSize: "0.82rem" }}>{q.data.disclaimer}</p>
        </>
      )}
    </>
  );
}
