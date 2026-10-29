import { useEffect, useState } from "react";
import { LineChart } from "../components/LineChart";
import { Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton, Tile } from "../components/ui";
import type { LivePrediction } from "../lib/api";
import { dash, num, pct, shortDate, signedPct } from "../lib/format";
import { LIVE_PAGE, useLiveModels, useLivePredictions, useLiveSummary } from "../lib/queries";

const HORIZONS = [{ value: "position_20d", label: "20 sessions" }, { value: "swing_5d", label: "5 sessions" }];
const STATE_TONE = { research: "research", paper: "warn", approved: "ok" } as const;
const STATE_HELP = {
  research: "Research: logged and measured, never traded, not shown to be an edge.",
  paper: "Paper: tracked on paper by the owner's decision; not approved.",
  approved: "Approved by the owner after the offline gate and live-paper evidence.",
} as const;
const OUT_TONE = { WIN: "ok", LOSS: "bad", TIME: "warn" } as const;
const OUT_TEXT = { WIN: "target hit", LOSS: "stop hit", TIME: "time exit" } as const;

function Outcome({ p }: { p: LivePrediction }) {
  const o = p.outcome;
  if (!o) {
    return (
      <span className="muted" title="No outcome exists until the window has elapsed: it is never computed early.">
        pending<div style={{ fontSize: "0.75rem" }}>resolves by ~{shortDate(p.window.exit_by)} (estimate)</div>
      </span>
    );
  }
  return (
    <span>
      <Chip tone={OUT_TONE[o.status]}>{OUT_TEXT[o.status]}</Chip>{o.excluded && <> <Chip tone="warn" title="The window touches an unexplained corporate action: excluded from the statistics.">excluded</Chip></>}
      <div style={{ fontSize: "0.78rem" }}>
        {shortDate(o.entry_date)} → {shortDate(o.exit_date)}{o.gap_exit ? " (gap fill)" : ""}<br />
        net {signedPct(o.ret_net, 2)} (gross {signedPct(o.ret_gross, 2)}, costs {o.cost_bps == null ? dash : `${num(o.cost_bps, 1)} bps`})
        {o.realized_minus_expected != null && <><br />vs expected {signedPct(p.expected_return, 2)}: {signedPct(o.realized_minus_expected, 2)}</>}
      </div>
    </span>
  );
}

export function LiveCompare() {
  const models = useLiveModels();
  const [model, setModel] = useState<string | null>(null);
  const [horizon, setHorizon] = useState("position_20d");
  const [state, setState] = useState("all");
  const [selectedOnly, setSel] = useState(true);
  const [symbol, setSymbol] = useState("");
  const [offset, setOffset] = useState(0);
  useEffect(() => { if (!model && models.data?.models.length) setModel(models.data.models[0].model_version); }, [models.data, model]);
  const sum = useLiveSummary(model, horizon);
  const preds = useLivePredictions({ model, horizon, state, selectedOnly, symbol: symbol.trim(), offset });
  const m = models.data?.models.find((x) => x.model_version === model);
  const h = sum.data?.health;
  const c = sum.data?.counts;

  return (
    <>
      <PageHead title="Live compare" sub="What each model predicted, what the market then did, and whether the evidence says the model is working." right={m ? <Chip tone={STATE_TONE[m.state]} title={STATE_HELP[m.state]}>{m.state.toUpperCase()}</Chip> : <Evidence kind="research" />} />
      <div className="notice">
        <strong>How to read this.</strong> Every prediction is logged before the outcome exists and cannot be edited. An outcome appears only after its window has fully elapsed, resolved by the same barrier rules and cost model used in training.
        Nothing here is guaranteed, and no model in this system has been shown to have a net edge.
        {models.data && <ul style={{ margin: "6px 0 0", paddingLeft: "1.2em" }}>{models.data.ledger_rules.map((r) => <li key={r}>{r}</li>)}</ul>}
      </div>

      {models.isLoading && <Skeleton rows={4} />}
      {models.isError && <ErrorState error={models.error} onRetry={() => models.refetch()} />}
      {models.data && models.data.models.length === 0 && <Empty>No model has logged any prediction yet.</Empty>}

      {m && (
        <>
          <div className="card">
            <header>
              <h2 style={{ marginRight: "auto" }}>{m.model_version}</h2>
              {models.data!.models.length > 1 && (
                <select className="input" style={{ maxWidth: 260 }} aria-label="Model" value={model ?? ""} onChange={(e) => { setModel(e.target.value); setOffset(0); }}>
                  {models.data!.models.map((x) => <option key={x.model_version} value={x.model_version}>{x.model_version} ({x.state})</option>)}
                </select>
              )}
              <Segmented label="Horizon" value={horizon} onChange={(v) => { setHorizon(v); setOffset(0); }} options={HORIZONS} />
            </header>
            {m.description && <p style={{ marginTop: 0 }}>{m.description}</p>}
            <p className="muted" style={{ fontSize: "0.85rem", margin: 0 }}>{STATE_HELP[m.state]}</p>
          </div>

          {sum.isLoading && <Skeleton rows={3} />}
          {sum.isError && <ErrorState error={sum.error} onRetry={() => sum.refetch()} />}
          {sum.data && c && h && (
            <>
              <section className="grid tiles" aria-label="Loop status">
                <Tile label="Logged" value={c.logged.toLocaleString("en-IN")} sub={c.first_as_of ? `${c.dates} sessions, ${shortDate(c.first_as_of)} to ${shortDate(c.last_as_of)}` : "nothing logged for this horizon"} />
                <Tile label="Resolved" value={c.resolved.toLocaleString("en-IN")} sub={`${c.pending.toLocaleString("en-IN")} waiting for their window${c.excluded ? ` · ${c.excluded} excluded` : ""}`} />
                <Tile label="Rank IC (recent)" value={h.ranking ? num(h.ranking.ic_rolling, 3) : dash} sub={h.ranking ? `mean ${num(h.ranking.ic_mean, 3)} · positive on ${pct(h.ranking.ic_hit, 0)} of ${h.n_dates} dates` : "needs resolved outcomes"} />
                <Tile label="Top fifth, net of costs" value={h.economics?.net_mean_top == null ? dash : signedPct(h.economics.net_mean_top, 2)}
                  sub={h.economics?.excess_mean == null ? "needs resolved outcomes" : `vs universe ${signedPct(h.economics.net_mean_universe, 2)} · excess ${signedPct(h.economics.excess_mean, 2)}`} tone={h.economics?.excess_mean != null && h.economics.excess_mean < 0 ? "bad" : undefined} />
                <Tile label="Calibration" value={h.calibration ? `slope ${num(h.calibration.slope, 2)}` : "n/a"} sub={h.calibration ? `Brier ${num(h.calibration.brier, 3)} vs base ${num(h.calibration.brier_base, 3)}` : "no win probability is logged by this model"} />
                <Tile label="Drift" value={h.score_drift ? (h.score_drift.flag ? "flagged" : "stable") : "not yet"} tone={h.score_drift?.flag ? "bad" : undefined} sub={h.score_drift?.psi != null ? `score PSI ${num(h.score_drift.psi, 2)}` : "needs 40 resolved dates"} />
                <Tile label="Kill switch" value={h.kill_switch.length ? "TRIPPED" : "clear"} tone={h.kill_switch.length ? "bad" : "ok"} sub={h.kill_switch.length ? h.kill_switch.join("; ") : `acts on ${sum.data.thresholds.min_resolved}+ resolved / ${sum.data.thresholds.min_dates}+ dates`} />
              </section>

              {(sum.data.notes.length > 0 || h.notes.length > 0) && (
                <div className="notice" role="status"><ul style={{ margin: 0, paddingLeft: "1.2em" }}>{[...sum.data.notes, ...h.notes].map((n) => <li key={n}>{n}</li>)}</ul></div>
              )}

              {h.ic_series && h.ic_series.dates.length > 1 && (
                <div className="grid two">
                  <div className="card">
                    <header><h2>Rank IC by date</h2></header>
                    <LineChart label="Rank information coefficient by prediction date with its 60-date average" dates={h.ic_series.dates} zero format={(v) => v.toFixed(2)}
                      series={[{ name: "Daily", values: h.ic_series.values.map((v) => v ?? 0), color: "var(--muted)" }, { name: "60-date average", values: h.ic_series.rolling60.map((v) => v ?? 0), color: "var(--brand)" }]} />
                    <p className="muted" style={{ fontSize: "0.82rem" }}>Spearman correlation between the model's score and the realised net result, per prediction date. Windows overlap, so dates are not independent.</p>
                  </div>
                  <div className="card">
                    <header><h2>Calibration</h2></header>
                    {h.reliability ? (
                      <table><thead><tr><th>Predicted</th><th className="num">Observed win rate</th><th className="num">n</th></tr></thead>
                        <tbody>{h.reliability.map((r) => <tr key={r.bin}><td>{pct(r.mean_p, 0)}</td><td className="num">{pct(r.observed, 0)}</td><td className="num">{r.n}</td></tr>)}</tbody></table>
                    ) : <Empty>This model logs no win probability, so there is nothing to calibrate.</Empty>}
                  </div>
                </div>
              )}

              {h.retrain && (
                <div className="card">
                  <header><h2>Retrain recommendation</h2><Chip>{h.retrain.trigger ? "candidate suggested" : "none"}</Chip></header>
                  <p style={{ marginTop: 0 }}>{h.retrain.action}. A retrain only ever produces a <strong>candidate</strong>; promotion needs the owner and the offline gate.</p>
                  {h.retrain.suppressed && <p className="muted" style={{ fontSize: "0.85rem" }}>{h.retrain.suppressed}</p>}
                  {h.retrain.reasons.length > 0 && <ul style={{ margin: 0, paddingLeft: "1.2em" }}>{h.retrain.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
                </div>
              )}
            </>
          )}

          <div className="card">
            <header>
              <h2 style={{ marginRight: "auto" }}>Predictions</h2>
              <Segmented label="State" value={state} onChange={(v) => { setState(v); setOffset(0); }} options={[{ value: "all", label: "All" }, { value: "pending", label: "Pending" }, { value: "resolved", label: "Resolved" }]} />
              <Segmented label="Names" value={selectedOnly ? "sel" : "all"} onChange={(v) => { setSel(v === "sel"); setOffset(0); }} options={[{ value: "sel", label: "Card picks" }, { value: "all", label: "Whole universe" }]} />
              <label className="sr-only" htmlFor="lsym">Symbol</label>
              <input id="lsym" className="input" style={{ maxWidth: 140 }} placeholder="Symbol" value={symbol} onChange={(e) => { setSymbol(e.target.value); setOffset(0); }} />
            </header>
            {preds.isLoading && <Skeleton rows={5} />}
            {preds.isError && <ErrorState error={preds.error} onRetry={() => preds.refetch()} />}
            {preds.data && (preds.data.rows.length === 0 ? <Empty>No predictions match. Predictions appear here once the daily logger has run (after the end-of-day load, before the next open).</Empty> : (
              <>
                <p className="muted" style={{ fontSize: "0.85rem", marginTop: 0 }}>{preds.data.n_matching.toLocaleString("en-IN")} predictions; showing {offset + 1}–{Math.min(offset + LIVE_PAGE, preds.data.n_matching)}.</p>
                <div className="table-wrap" style={{ maxHeight: 640 }}>
                  <table>
                    <thead><tr><th>Prediction</th><th>Signal</th><th>Forecast</th><th>Entry and window</th><th className="num">Size</th><th>Outcome</th></tr></thead>
                    <tbody>
                      {preds.data.rows.map((r) => (
                        <tr key={r.prediction_id}>
                          <td><strong>{r.symbol}</strong> <Chip>{r.horizon_id.replace("_", " ")}</Chip>
                            <div className="muted" style={{ fontSize: "0.75rem" }}>as of {shortDate(r.as_of_date)} close · logged {new Date(r.prediction_time).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" })}<br />{r.model_version}</div>
                            <details><summary style={{ fontSize: "0.75rem", cursor: "pointer" }}>lineage</summary>
                              <ul style={{ fontSize: "0.75rem", margin: 0, paddingLeft: "1.1em" }}>
                                {Object.entries(r.lineage).map(([k, v]) => <li key={k}>{k.replace(/_/g, " ")}: <code>{v ?? "none"}</code></li>)}
                                <li>cost model: <code>{r.cost_model}</code></li><li>regime: {r.regime ?? "n/a"}</li>
                              </ul></details></td>
                          <td className="num">score {num(r.score, 2)}<div className="muted" style={{ fontSize: "0.75rem" }}>rank {r.rank_pct == null ? dash : pct(r.rank_pct, 0)}</div></td>
                          <td className="wrap" style={{ fontSize: "0.8rem" }}>
                            {r.p_win == null && r.expected_return == null ? <span className="muted">no forecast logged (risk ordering only)</span> : (
                              <>{r.p_win != null && <>P(win) {pct(r.p_win, 0)}<br /></>}{r.expected_return != null && <>expected {signedPct(r.expected_return, 2)}</>}
                                {r.ci_low != null && r.ci_high != null && <><br />interval {signedPct(r.ci_low, 1)} to {signedPct(r.ci_high, 1)}</>}</>)}
                          </td>
                          <td className="wrap" style={{ fontSize: "0.8rem" }}>
                            {r.entry_rule}, {shortDate(r.window.entry_date)}<br />exit by {shortDate(r.window.exit_by)} ({r.window.sessions} sessions){r.window.estimate && " · est."}
                            <div className="muted" style={{ fontSize: "0.75rem" }}>stop {r.barriers.stop_distance == null ? `${r.barriers.stop_sigmas}σ` : `-${pct(r.barriers.stop_distance, 1)}`} · target {r.barriers.target_distance == null ? `${r.barriers.target_sigmas}σ` : `+${pct(r.barriers.target_distance, 1)}`}</div>
                          </td>
                          <td className="num">{r.position_size ? pct(r.position_size, 1) : <span className="muted">not picked</span>}</td>
                          <td className="wrap"><Outcome p={r} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
                  <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - LIVE_PAGE))}>Previous</button>
                  <button className="btn" disabled={offset + LIVE_PAGE >= preds.data.n_matching} onClick={() => setOffset(offset + LIVE_PAGE)}>Next</button>
                </div>
              </>
            ))}
          </div>
          <p className="muted" style={{ fontSize: "0.82rem" }}>{models.data?.disclaimer}</p>
        </>
      )}
    </>
  );
}
