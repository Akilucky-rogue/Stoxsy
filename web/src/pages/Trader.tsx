import { useState } from "react";
import { Chip, Empty, ErrorState, Evidence, PageHead, Skeleton, Tile, type Tone } from "../components/ui";
import type { TraderCandidate, TraderIdea, TraderMeters, TraderOpen } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dash, num, pct, rupees, shortDate, signed } from "../lib/format";
import { useCloseIdea, useLogIdea, useTraderDesk, useTraderIdeas, useTraderPerformance } from "../lib/queries";

/** How full a limit is: under 60% fine, 60-100% close, 100%+ reached. Pure so it can be tested. */
export function meterTone(used: number): Tone {
  return used >= 1 ? "bad" : used >= 0.6 ? "warn" : "ok";
}
export const meterWidth = (used: number) => `${Math.round(Math.max(0, Math.min(1, used)) * 100)}%`;

function Meter({ label, used, text, hint }: { label: string; used: number; text: string; hint: string }) {
  const tone = meterTone(used);
  const color = tone === "bad" ? "var(--down)" : tone === "warn" ? "var(--warn)" : "var(--up)";
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value" style={{ fontSize: "1.25rem" }}>{text}</div>
      <div role="progressbar" aria-label={`${label}: ${Math.round(used * 100)}% of the limit used`} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(Math.min(1, used) * 100)}
        style={{ height: 8, borderRadius: 4, background: "var(--line)", marginTop: 8, overflow: "hidden" }}>
        <div style={{ width: meterWidth(used), height: "100%", background: color }} />
      </div>
      <div className="sub">{hint}</div>
    </div>
  );
}

function Meters({ m }: { m: TraderMeters }) {
  return (
    <div className="grid tiles">
      <Meter label="Open risk" used={m.open_risk_limit_pct ? m.open_risk_pct / m.open_risk_limit_pct : 0} text={`${num(m.open_risk_pct, 2)}% of ${m.open_risk_limit_pct}%`} hint={`${rupees(m.open_risk)} at risk if every stop is hit`} />
      <Meter label="Today" used={m.daily_limit_used} text={`${signed(m.daily_pnl_pct, 2)}%`} hint={`new ideas stop at -${m.daily_loss_limit_pct}% · ${rupees(m.daily_pnl)}`} />
      <Meter label="This week" used={m.weekly_limit_used} text={`${signed(m.weekly_pnl_pct, 2)}%`} hint={`limit -${m.weekly_loss_limit_pct}% · a breach halves next week's size`} />
      <Meter label="Positions" used={m.max_positions ? m.positions / m.max_positions : 0} text={`${m.positions} of ${m.max_positions}`} hint={`${rupees(m.free_cash)} of the pot is free`} />
    </div>
  );
}

function CandidateCard({ c, canWrite }: { c: TraderCandidate; canWrite: boolean }) {
  const log = useLogIdea();
  const k = c.card;
  return (
    <div className="card">
      <header>
        <h2>{c.symbol}</h2>
        <span><Evidence kind="research" /> <Chip title="Breakout above the 20-session high on at least median volume, today">20-day breakout +{pct(c.breakout_pct, 1)}</Chip> {c.risk_bucket != null && <Chip>risk bucket {c.risk_bucket}</Chip>} {c.allowed ? <Chip tone="ok">passes your rules</Chip> : <Chip tone="bad">refused</Chip>}</span>
      </header>
      {!c.allowed || !k ? (
        <ul style={{ margin: 0, paddingLeft: 18 }}>{c.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
      ) : (
        <>
          <div className="grid tiles">
            <Tile label="Buy up to" value={`₹${num(k.entry)}`} sub={k.entry_rule} />
            <Tile label="Stop (exit if it closes below)" value={`₹${num(k.stop)}`} sub={`${pct(k.stop_pct, 1)} below · ${num(k.stop_in_daily_moves, 1)} typical daily moves`} tone="bad" />
            <Tile label="Target" value={`₹${num(k.target)}`} sub={k.target_r != null ? `${num(k.target_r, 1)}x the planned risk` : undefined} tone="ok" />
            <Tile label="Quantity" value={`${k.qty} sh`} sub={`${rupees(k.position_value)} · sized by "${k.sizing.binding_limit.replace(/_/g, " ")}"`} />
            <Tile label="Risk if stopped" value={rupees(k.risk_rupees)} sub={`${num(k.risk_pct_of_trading_capital, 3)}% of your trading pot, including cost and gap allowance`} />
            <Tile label="Till when" value={shortDate(k.time_stop)} sub={k.till_when} />
          </div>
          <p className="muted" style={{ fontSize: "0.88rem" }}>{k.evidence_note}</p>
          <p style={{ fontSize: "0.88rem", marginBottom: 4 }}><strong>This idea is wrong if:</strong> {k.invalidated_if.join("; ")}.</p>
          {c.notes.map((n) => <p key={n} className="muted" style={{ fontSize: "0.88rem", margin: "2px 0" }}>{n}</p>)}
          <button className="btn primary" style={{ marginTop: 8 }} disabled={!canWrite || log.isPending} title={canWrite ? "Records a paper idea. No order is placed anywhere." : "Your role is read-only"}
            onClick={() => log.mutate({ symbol: c.symbol, horizon_id: k.horizon_id, setup: k.setup })}>{log.isPending ? "Logging…" : "Log as a paper idea"}</button>
          {log.isError && <p role="alert" style={{ color: "var(--down)" }}>{log.error instanceof Error ? log.error.message : "Not logged"}</p>}
        </>
      )}
    </div>
  );
}

function OpenBook({ open, ideas, canWrite }: { open: TraderOpen[]; ideas: TraderIdea[]; canWrite: boolean }) {
  const close = useCloseIdea();
  const [px, setPx] = useState<Record<number, string>>({});
  const openIdeas = ideas.filter((i) => i.status === "open");
  if (openIdeas.length === 0) return <Empty>No open paper ideas.</Empty>;
  return (
    <div className="table-wrap"><table aria-label="Open paper ideas">
      <thead><tr><th>Symbol</th><th className="num">Qty</th><th className="num">Entry</th><th className="num">Stop</th><th className="num">Target</th><th className="num">Last</th><th className="num">Unrealised</th><th>Till</th><th>Close at</th></tr></thead>
      <tbody>{openIdeas.map((i) => {
        const o = open.find((x) => x.symbol === i.symbol);
        const v = Number(px[i.id] ?? "");
        return (
          <tr key={i.id}>
            <td><strong>{i.symbol}</strong> <Chip>{i.evidence}</Chip></td><td className="num">{i.qty}</td><td className="num">{num(i.entry)}</td><td className="num">{num(i.stop)}</td><td className="num">{i.target ? num(i.target) : dash}</td>
            <td className="num">{o ? num(o.last) : dash}</td><td className="num" style={{ color: o && o.unrealised < 0 ? "var(--down)" : "var(--up)" }}>{o ? rupees(o.unrealised) : dash}</td><td>{shortDate(i.time_stop)}</td>
            <td style={{ whiteSpace: "nowrap" }}>
              <input className="input" aria-label={`Exit price for ${i.symbol}`} inputMode="decimal" style={{ width: 90 }} value={px[i.id] ?? ""} onChange={(e) => setPx({ ...px, [i.id]: e.target.value })} />{" "}
              {(["stop", "target", "time", "manual"] as const).map((r) => (
                <button key={r} className="btn" style={{ padding: "4px 8px" }} disabled={!canWrite || !(v > 0) || close.isPending} onClick={() => close.mutate({ id: i.id, price: v, reason: r })}>{r}</button>
              ))}
            </td>
          </tr>
        );
      })}</tbody></table>
      {close.isError && <p role="alert" style={{ color: "var(--down)" }}>{close.error instanceof Error ? close.error.message : "Not closed"}</p>}
    </div>
  );
}

function Journal({ ideas }: { ideas: TraderIdea[] }) {
  const closed = ideas.filter((i) => i.status === "closed");
  const perf = useTraderPerformance();
  return (
    <div className="card">
      <header><h2>Journal</h2><Evidence kind="paper" /></header>
      {perf.data && (
        <p style={{ marginTop: 0 }}>
          {perf.data.n_closed === 0 ? perf.data.note : <>
            {perf.data.n_closed} closed · hit rate {pct(perf.data.hit_rate, 0)} · expectancy {signed(perf.data.expectancy_r)}R · P&amp;L {rupees(perf.data.total_pnl_rupees)}. <span className="muted">{perf.data.note}</span></>}
        </p>
      )}
      {closed.length === 0 ? <Empty>No closed ideas yet: nothing to measure.</Empty> : (
        <div className="table-wrap"><table aria-label="Closed paper ideas">
          <thead><tr><th>Symbol</th><th>Closed</th><th>Why</th><th className="num">Entry</th><th className="num">Exit</th><th className="num">P&amp;L</th><th className="num">R</th></tr></thead>
          <tbody>{closed.map((i) => (
            <tr key={i.id}><td>{i.symbol}</td><td>{shortDate(i.closed_at)}</td><td>{i.exit_reason}</td><td className="num">{num(i.entry)}</td><td className="num">{num(i.exit_price)}</td>
              <td className="num" style={{ color: (i.pnl_rupees ?? 0) < 0 ? "var(--down)" : "var(--up)" }}>{rupees(i.pnl_rupees)}</td><td className="num">{signed(i.r_multiple)}</td></tr>
          ))}</tbody></table></div>
      )}
    </div>
  );
}

export function Trader() {
  const desk = useTraderDesk();
  const ideas = useTraderIdeas();
  const { me, status } = useAuth();
  const canWrite = status === "open" || me?.role === "admin" || me?.role === "trader";
  const d = desk.data;
  return (
    <>
      <PageHead title="Trader desk" sub="Research and paper ideas, sized to your own risk limits. Nothing here places an order." right={<Evidence kind="research" />} />
      {desk.isLoading && <Skeleton rows={4} />}
      {desk.isError && <ErrorState error={desk.error} onRetry={() => void desk.refetch()} />}
      {d && (
        <>
          <div className="card" role="note"><strong>{d.headline}</strong>{d.mode !== "trader" && <span className="muted"> You are looking at the trader desk in {d.mode ?? "no"} mode: mode is only a lens, the trading pot is still the only money used here.</span>}
            <div className="muted" style={{ fontSize: "0.86rem", marginTop: 4 }}>{d.disclaimer}</div></div>
          <h2>Your limits today</h2>
          <Meters m={d.meters} />
          {d.meters.size_multiplier < 1 && <div className="card" role="alert" style={{ marginTop: 12 }}><strong>Sizes are reduced ({d.meters.size_multiplier}x):</strong> {d.meters.size_multiplier_reasons.join("; ")}.</div>}
          {d.needs.includes("trading_capital") ? (
            <Empty>Enter your trading pot on the Profile page. Ideas are sized only from that pot; nothing is assumed.</Empty>
          ) : (
            <>
              <h2>Candidates today <Chip tone="research" title="Rule baseline, ordered by lower recent risk, not by any forecast of profit">baseline, no edge shown</Chip></h2>
              {d.candidates.length === 0 ? <Empty>No breakout candidates today. Doing nothing is a normal answer.</Empty> : <div style={{ display: "grid", gap: 12 }}>{d.candidates.map((c) => <CandidateCard key={c.symbol} c={c} canWrite={canWrite} />)}</div>}
            </>
          )}
          <h2>Open paper ideas</h2>
          {ideas.data ? <OpenBook open={d.open} ideas={ideas.data} canWrite={canWrite} /> : <Skeleton rows={2} />}
          {ideas.data && <Journal ideas={ideas.data} />}
        </>
      )}
    </>
  );
}
