import { useEffect, useMemo, useState } from "react";
import { Chip, Empty, ErrorState, Evidence, PageHead, Skeleton, Tile } from "../components/ui";
import type { Profile as ProfileT } from "../lib/api";
import { dash, pct, shortDate } from "../lib/format";
import { useModeAudit, useProfile, useUpdateProfile } from "../lib/queries";

const inr = (x: number | null | undefined) => (x == null ? dash : `₹${Math.round(x).toLocaleString("en-IN")}`);
const toNum = (s: string): number | null => {
  const t = s.replace(/[₹,\s]/g, "");
  if (t === "") return null;
  const v = Number(t);
  return Number.isFinite(v) && v >= 0 ? v : NaN;
};

const LIMITS: { key: string; label: string; unit: string; help: string }[] = [
  { key: "risk_per_trade_pct", label: "Risk per trade", unit: "% of trading capital", help: "The most one idea may lose if its stop is hit, costs included. Sets position size." },
  { key: "open_risk_pct", label: "Open risk", unit: "% of trading capital", help: "Risk at the stop, added up over every open idea." },
  { key: "daily_loss_pct", label: "Daily loss limit", unit: "% of trading capital", help: "Reaching it stops new ideas for the day." },
  { key: "weekly_loss_pct", label: "Weekly loss limit", unit: "% of trading capital", help: "Reaching it halves position size the next week." },
  { key: "max_positions", label: "Open ideas", unit: "count", help: "The most ideas open at once." },
  { key: "max_pair_correlation", label: "Pairwise correlation", unit: "60-session returns", help: "Two open ideas may not move together more than this." },
  { key: "liquidity_cap_pct", label: "Liquidity cap", unit: "% of average daily traded value", help: "A position may not exceed this share of a typical day's trading." },
];

/** Where the money sits. The tick marks the guard: the trading pot should stay small next to the investment pot. Widths animate when the numbers change. */
export function PotBar({ investment, trading, emergency, cap }: { investment: number; trading: number; emergency: number | null; cap: number }) {
  const total = investment + trading;
  const W = 560;
  const iw = total > 0 ? (investment / total) * W : 0;
  const tw = total > 0 ? (trading / total) * W : 0;
  const guardX = (1 - cap) * W;
  const over = total > 0 && trading / total > cap + 1e-9;
  const label = total > 0
    ? `Investable capital ${inr(total)}: investment pot ${inr(investment)} (${pct(investment / total, 1)}), trading pot ${inr(trading)} (${pct(trading / total, 1)}). Guard: trading at most ${pct(cap, 0)}.`
    : "No capital recorded yet.";
  return (
    <figure style={{ margin: 0 }}>
      <svg role="img" aria-label={label} viewBox={`0 0 ${W} 96`} style={{ width: "100%", maxWidth: 640, height: "auto" }}>
        <rect x={0} y={14} width={W} height={30} rx={8} fill="var(--surface-2)" stroke="var(--line)" />
        <rect x={0} y={14} width={iw} height={30} rx={8} fill="var(--brand)" style={{ transition: "width .6s ease" }} />
        <rect x={iw} y={14} width={tw} height={30} fill={over ? "var(--down)" : "var(--accent)"} style={{ transition: "width .6s ease, x .6s ease" }} />
        <line x1={guardX} x2={guardX} y1={8} y2={50} stroke="var(--ink)" strokeDasharray="3 3" />
        <text x={W} y={62} textAnchor="end" fontSize="11" fill="var(--muted)">dashed line = guard: trading at most {pct(cap, 0)} of investable</text>
        <text x={4} y={80} fontSize="11" fill="var(--ink-2)">■ investment {total > 0 ? pct(investment / total, 1) : dash}</text>
        <text x={190} y={80} fontSize="11" fill="var(--ink-2)">■ trading {total > 0 ? pct(trading / total, 1) : dash}</text>
        <text x={W} y={80} textAnchor="end" fontSize="11" fill="var(--muted)">emergency fund {emergency == null ? "not recorded" : inr(emergency)} (not investable)</text>
      </svg>
      <figcaption className="muted" style={{ fontSize: "0.85rem" }}>
        Two separate pots. A holding stays in the pot it was imported into; switching mode only changes which pot you are looking at. The emergency fund sits outside both.
      </figcaption>
    </figure>
  );
}

function CapitalForm({ p }: { p: ProfileT }) {
  const upd = useUpdateProfile();
  const [inv, setInv] = useState(p.investment_capital?.toString() ?? "");
  const [trd, setTrd] = useState(p.trading_capital?.toString() ?? "");
  const [emg, setEmg] = useState(p.emergency_fund?.toString() ?? "");
  const [note, setNote] = useState("");
  useEffect(() => { setInv(p.investment_capital?.toString() ?? ""); setTrd(p.trading_capital?.toString() ?? ""); setEmg(p.emergency_fund?.toString() ?? ""); }, [p.investment_capital, p.trading_capital, p.emergency_fund]);
  const vals = { i: toNum(inv), t: toNum(trd), e: toNum(emg) };
  const bad = [vals.i, vals.t, vals.e].some((v) => Number.isNaN(v));
  const cap = p.rules.pots.trading_max_share_of_investable;
  const total = (vals.i ?? 0) + (vals.t ?? 0);
  const share = total > 0 ? (vals.t ?? 0) / total : 0;
  const needsOverride = share > cap + 1e-9;
  const dirty = vals.i !== p.investment_capital || vals.t !== p.trading_capital || vals.e !== p.emergency_fund;
  const save = () => upd.mutate({ investment_capital: vals.i, trading_capital: vals.t, emergency_fund: vals.e, ...(needsOverride && note.trim() ? { trading_override_note: note.trim() } : {}) });
  return (
    <div className="card">
      <header><h2>Capital</h2><Chip title="Amounts are stored only in your own profile in the database, never in the code or the repository.">private to you</Chip></header>
      <PotBar investment={vals.i ?? 0} trading={vals.t ?? 0} emergency={vals.e} cap={cap} />
      <div className="grid three" style={{ marginTop: 12 }}>
        <div className="field"><label htmlFor="inv">Investment pot (₹)</label><input id="inv" className="input" inputMode="decimal" value={inv} onChange={(e) => setInv(e.target.value)} placeholder="money not needed for 5+ years" /></div>
        <div className="field"><label htmlFor="trd">Trading pot (₹)</label><input id="trd" className="input" inputMode="decimal" value={trd} onChange={(e) => setTrd(e.target.value)} placeholder="money you can afford to lose" /></div>
        <div className="field"><label htmlFor="emg">Emergency fund (₹)</label><input id="emg" className="input" inputMode="decimal" value={emg} onChange={(e) => setEmg(e.target.value)} placeholder="a constraint, not a pot" /></div>
      </div>
      {bad && <p role="alert" style={{ color: "var(--down)" }}>Amounts must be zero or positive numbers.</p>}
      {needsOverride && !bad && (
        <div className="field">
          <label htmlFor="ovr">Trading pot is {pct(share, 1)} of investable capital; the guard is {pct(cap, 0)}. To keep it, an admin records why:</label>
          <input id="ovr" className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="reason (at least 8 characters; audited)" />
        </div>
      )}
      <button className="btn primary" disabled={bad || !dirty || upd.isPending} onClick={save}>{upd.isPending ? "Saving…" : "Save capital"}</button>
      {upd.isError && <p role="alert" style={{ color: "var(--down)" }}>{upd.error instanceof Error ? upd.error.message : "Not saved"}</p>}
      {upd.isSuccess && !dirty && <p role="status" className="muted">Saved and recorded in the audit trail.</p>}
    </div>
  );
}

function LimitsForm({ p }: { p: ProfileT }) {
  const upd = useUpdateProfile();
  const [vals, setVals] = useState<Record<string, string>>(() => Object.fromEntries(LIMITS.map((l) => [l.key, String(p.effective_limits[l.key])])));
  const [note, setNote] = useState("");
  useEffect(() => setVals(Object.fromEntries(LIMITS.map((l) => [l.key, String(p.effective_limits[l.key])]))), [p.effective_limits]);
  const changed = useMemo(() => LIMITS.filter((l) => Number(vals[l.key]) !== p.effective_limits[l.key]), [vals, p.effective_limits]);
  const loosening = changed.filter((l) => Number(vals[l.key]) > p.limit_defaults[l.key]);
  const overCeiling = changed.filter((l) => Number(vals[l.key]) > p.limit_ceilings[l.key]);
  const invalid = changed.some((l) => !(Number(vals[l.key]) > 0));
  const save = () => upd.mutate({ limits: Object.fromEntries(changed.map((l) => [l.key, Number(vals[l.key])])), ...(loosening.length ? { approval_note: note.trim() } : {}) });
  return (
    <div className="card">
      <header><h2>Trader limits</h2><Evidence kind="paper" /></header>
      <p className="muted" style={{ marginTop: 0, fontSize: "0.9rem" }}>
        These are enforced by the system, not just shown. Tightening is always allowed. Loosening past the default needs an admin note and stays under the ceiling.
        {!p.engine_approved && " No engine is approved, so the open-risk ceiling stays at its default."}
      </p>
      <div className="table-wrap">
        <table>
          <thead><tr><th>Limit</th><th className="num">Default</th><th className="num">Ceiling</th><th className="num">Yours</th><th>What it does</th></tr></thead>
          <tbody>
            {LIMITS.map((l) => (
              <tr key={l.key}>
                <td><strong>{l.label}</strong><div className="muted" style={{ fontSize: "0.78rem" }}>{l.unit}</div></td>
                <td className="num">{p.limit_defaults[l.key]}</td>
                <td className="num">{p.limit_ceilings[l.key]}</td>
                <td className="num">
                  <input aria-label={`${l.label} (${l.unit})`} className="input" style={{ width: 90, textAlign: "right" }} inputMode="decimal" value={vals[l.key]} onChange={(e) => setVals({ ...vals, [l.key]: e.target.value })} />
                  {p.loosened_limits.includes(l.key) && <> <Chip tone="warn" title="Above the default, by an audited approval">loosened</Chip></>}
                </td>
                <td className="wrap muted" style={{ fontSize: "0.85rem" }}>{l.help}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {loosening.length > 0 && overCeiling.length === 0 && (
        <div className="field" style={{ marginTop: 12 }}>
          <label htmlFor="appr">Approval note for loosening {loosening.map((l) => l.label.toLowerCase()).join(", ")} (admin; audited):</label>
          <input id="appr" className="input" value={note} onChange={(e) => setNote(e.target.value)} placeholder="why this is now acceptable (at least 8 characters)" />
        </div>
      )}
      {overCeiling.length > 0 && <p role="alert" style={{ color: "var(--down)" }}>{overCeiling.map((l) => `${l.label} cannot exceed ${p.limit_ceilings[l.key]}`).join("; ")}.</p>}
      <button className="btn primary" style={{ marginTop: 12 }} disabled={changed.length === 0 || invalid || overCeiling.length > 0 || upd.isPending} onClick={save}>{upd.isPending ? "Saving…" : "Save limits"}</button>
      {upd.isError && <p role="alert" style={{ color: "var(--down)" }}>{upd.error instanceof Error ? upd.error.message : "Not saved"}</p>}
      <h3>Fixed rules</h3>
      <ul style={{ margin: 0, paddingLeft: "1.2em" }}>
        <li>After {p.rules.trader.losing_streak.stops} stopped trades in a row, size is halved for the next {p.rules.trader.losing_streak.half_size_next_trades} trades.</li>
        <li>Every idea has a time stop at its registered horizon ({p.rules.trader.time_stop_sessions.join(" or ")} sessions).</li>
        <li>Averaging down is not offered. Futures and options: defined-risk structures only, research and paper only; no order can be placed from this product.</li>
      </ul>
    </div>
  );
}

export function Profile() {
  const q = useProfile();
  const audit = useModeAudit();
  const p = q.data;
  return (
    <>
      <PageHead title="Profile & capital" sub="Your mode, your two capital pots and the trader's risk limits. Everything here is saved to your own profile and every change is recorded." right={<Evidence kind="research" />} />
      {q.isLoading && <Skeleton rows={5} />}
      {q.isError && <ErrorState error={q.error} onRetry={() => q.refetch()} />}
      {p && (
        <>
          <section className="grid tiles" aria-label="Profile summary">
            <Tile label="Mode" value={p.mode === null ? "Not chosen" : p.mode === "trader" ? "Trader" : "Investor"} sub={p.lens_pot ? `looking at the ${p.lens_pot} pot` : "pick Investor or Trader in the top bar"} />
            <Tile label="Investment pot" value={inr(p.investment_capital)} sub="money not needed for 5+ years" />
            <Tile label="Trading pot" value={inr(p.trading_capital)} sub={p.trading_share_of_investable == null ? "no capital recorded" : `${pct(p.trading_share_of_investable, 1)} of investable`} tone={p.trading_share_of_investable != null && p.trading_share_of_investable > p.rules.pots.trading_max_share_of_investable ? "bad" : undefined} />
            <Tile label="Emergency fund" value={inr(p.emergency_fund)} sub={p.emergency_fund == null ? "not recorded: plans will say so" : "kept outside both pots"} />
          </section>
          {p.warnings.length > 0 && <div className="notice" role="status"><ul style={{ margin: 0, paddingLeft: "1.2em" }}>{p.warnings.map((w) => <li key={w}>{w}</li>)}</ul></div>}
          <div className="notice">{p.rules.guardrails.join(" ")}</div>
          <CapitalForm p={p} />
          <LimitsForm p={p} />
          <div className="card">
            <header><h2>Audit trail</h2><Chip title="Append-only: entries cannot be edited or removed.">append-only</Chip></header>
            {audit.isLoading && <Skeleton rows={3} />}
            {audit.data && audit.data.length === 0 && <Empty>No changes recorded yet.</Empty>}
            {audit.data && audit.data.length > 0 && (
              <div className="table-wrap" style={{ maxHeight: 320 }}>
                <table>
                  <thead><tr><th>When</th><th>What</th><th>By</th></tr></thead>
                  <tbody>{audit.data.map((a, i) => <tr key={i}><td>{shortDate(a.ts)}</td><td><strong>{a.action}</strong> <span className="muted" style={{ fontSize: "0.8rem" }}>{JSON.stringify(a.payload).slice(0, 140)}</span></td><td>{a.actor_role ?? dash}</td></tr>)}</tbody>
                </table>
              </div>
            )}
          </div>
          <p className="muted" style={{ fontSize: "0.82rem" }}>{p.disclaimer} Policy {p.policy_version}.</p>
        </>
      )}
    </>
  );
}
