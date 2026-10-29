import { useState } from "react";
import { AllocationBars, FanChart, inrShort } from "../components/PlanCharts";
import { Chip, Empty, ErrorState, Evidence, PageHead, Skeleton, Tile, type Tone } from "../components/ui";
import type { GoalIn, GoalPlan, InvestorPlan, Mix, Profile } from "../lib/api";
import { dash, pct, shortDate } from "../lib/format";
import { useAddBalance, useAddGoal, useArchiveBalance, useArchiveGoal, usePlan, useProfile, useUpdateProfile } from "../lib/queries";

const REACTIONS = [
  { v: "add", t: "I would buy more: a fall is a discount" },
  { v: "hold", t: "I would hold and wait" },
  { v: "sell_some", t: "I would sell some to sleep better" },
  { v: "sell_all", t: "I would sell everything" },
] as const;
const ACTION_TONE: Record<string, Tone> = { no_action: "ok", direct_sip: "ok", rebalance: "warn", data_incomplete: "warn", no_data: "" };
const ACTION_LABEL: Record<string, string> = { no_action: "NO ACTION NEEDED", direct_sip: "DIRECT NEW SIP", rebalance: "REBALANCE", data_incomplete: "DATA INCOMPLETE", no_data: "NO DATA YET" };
const PROFILE_LABEL: Record<string, string> = { emergency: "Emergency fund", conservative: "Conservative", moderately_conservative: "Moderately conservative", balanced: "Balanced", growth: "Growth", aggressive: "Aggressive" };
const num = (s: string) => { const t = s.replace(/[₹,\s]/g, ""); const v = Number(t); return t !== "" && Number.isFinite(v) && v >= 0 ? v : NaN; };

function Questions({ p }: { p: Profile }) {
  const upd = useUpdateProfile();
  const [r, setR] = useState(p.drawdown_reaction ?? "");
  const [inc, setInc] = useState(p.income_stability ?? "");
  const [dep, setDep] = useState(p.dependents?.toString() ?? "");
  const depN = dep === "" ? NaN : Number(dep);
  const ok = r !== "" && inc !== "" && Number.isInteger(depN) && depN >= 0;
  return (
    <div className="card">
      <header><h2>Your answers</h2><Chip title="These set the ceiling on how much equity the plan may use.">risk is a limit, not a goal</Chip></header>
      <fieldset style={{ border: 0, padding: 0, margin: "0 0 12px" }}>
        <legend style={{ fontWeight: 600, marginBottom: 6 }}>Markets fall 30% in a year. What would you do?</legend>
        {REACTIONS.map((o) => (
          <label key={o.v} style={{ display: "block", padding: "4px 0" }}><input type="radio" name="reaction" checked={r === o.v} onChange={() => setR(o.v)} /> {o.t}</label>
        ))}
      </fieldset>
      <div className="grid two">
        <div className="field"><label htmlFor="inc">How steady is your income?</label>
          <select id="inc" className="input" value={inc} onChange={(e) => setInc(e.target.value)}><option value="">Choose…</option><option value="stable">Stable</option><option value="variable">Variable</option><option value="uncertain">Uncertain</option></select></div>
        <div className="field"><label htmlFor="dep">People who rely on your income</label><input id="dep" className="input" inputMode="numeric" value={dep} onChange={(e) => setDep(e.target.value)} /></div>
      </div>
      <button className="btn primary" disabled={!ok || upd.isPending} onClick={() => upd.mutate({ drawdown_reaction: r as never, income_stability: inc as never, dependents: depN })}>{upd.isPending ? "Saving…" : "Save answers"}</button>
      {upd.isError && <p role="alert" style={{ color: "var(--down)" }}>{upd.error instanceof Error ? upd.error.message : "Not saved"}</p>}
    </div>
  );
}

function GoalsEditor({ plan }: { plan: InvestorPlan | undefined }) {
  const add = useAddGoal();
  const arch = useArchiveGoal();
  const [f, setF] = useState({ name: "", kind: "", target: "", date: "", current: "", sip: "", test: false });
  const vals = { t: num(f.target), c: num(f.current), s: num(f.sip) };
  const ok = f.name.trim() !== "" && f.kind !== "" && vals.t > 0 && Number.isFinite(vals.c) && Number.isFinite(vals.s) && /^\d{4}-\d{2}-\d{2}$/.test(f.date);
  const submit = () => {
    const body: GoalIn = { name: f.name.trim(), kind: f.kind as "goal" | "emergency", target_amount: vals.t, target_date: f.date, current_amount: vals.c, monthly_sip: vals.s, is_test: f.test };
    add.mutate(body, { onSuccess: () => setF({ name: "", kind: "", target: "", date: "", current: "", sip: "", test: false }) });
  };
  return (
    <div className="card">
      <header><h2>Goals</h2></header>
      {plan && plan.goals.length > 0 && (
        <div className="table-wrap"><table>
          <thead><tr><th>Goal</th><th className="num">Target</th><th>By</th><th className="num">Saved</th><th className="num">SIP / month</th><th /></tr></thead>
          <tbody>{plan.goals.map((g) => (
            <tr key={g.id}><td><strong>{g.name}</strong> {g.kind === "emergency" && <Chip>emergency</Chip>} {g.is_test && <Chip tone="research" title="Marked as test data">test</Chip>}</td>
              <td className="num">{inrShort(g.target_amount)}</td><td>{shortDate(g.target_date)}</td><td className="num">{inrShort(g.current_amount)}</td><td className="num">{inrShort(g.monthly_sip)}</td>
              <td><button className="btn" aria-label={`Remove ${g.name}`} onClick={() => arch.mutate(g.id)}>Remove</button></td></tr>
          ))}</tbody></table></div>
      )}
      <h3>Add a goal</h3>
      <div className="grid three">
        <div className="field"><label htmlFor="gn">Name</label><input id="gn" className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
        <div className="field"><label htmlFor="gk">Type</label><select id="gk" className="input" value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}><option value="">Choose…</option><option value="goal">Goal</option><option value="emergency">Emergency fund</option></select></div>
        <div className="field"><label htmlFor="gt">Target amount (₹)</label><input id="gt" className="input" inputMode="decimal" value={f.target} onChange={(e) => setF({ ...f, target: e.target.value })} /></div>
        <div className="field"><label htmlFor="gd">Target date</label><input id="gd" className="input" type="date" value={f.date} onChange={(e) => setF({ ...f, date: e.target.value })} /></div>
        <div className="field"><label htmlFor="gc">Saved for it today (₹)</label><input id="gc" className="input" inputMode="decimal" value={f.current} onChange={(e) => setF({ ...f, current: e.target.value })} /></div>
        <div className="field"><label htmlFor="gs">Monthly SIP (₹)</label><input id="gs" className="input" inputMode="decimal" value={f.sip} onChange={(e) => setF({ ...f, sip: e.target.value })} /></div>
      </div>
      <label style={{ display: "block", marginBottom: 10 }}><input type="checkbox" checked={f.test} onChange={(e) => setF({ ...f, test: e.target.checked })} /> This is test data (not my real goal)</label>
      <button className="btn primary" disabled={!ok || add.isPending} onClick={submit}>{add.isPending ? "Adding…" : "Add goal"}</button>
      {add.isError && <p role="alert" style={{ color: "var(--down)" }}>{add.error instanceof Error ? add.error.message : "Not added"}</p>}
    </div>
  );
}

function BalancesEditor({ plan }: { plan: InvestorPlan | undefined }) {
  const add = useAddBalance();
  const arch = useArchiveBalance();
  const [f, setF] = useState({ cls: "", label: "", amount: "", asOf: "" });
  const amt = num(f.amount);
  return (
    <div className="card">
      <header><h2>Other holdings</h2><Chip title="Mutual funds, bonds, deposits and anything else that cannot be imported">added by hand</Chip></header>
      <p className="muted" style={{ marginTop: 0, fontSize: "0.9rem" }}>Mutual funds, bonds, deposits, provident fund and gold held elsewhere cannot be imported. Without them the plan sees only your stocks and ETFs and may call you almost fully in equity. Add each as a dated balance.</p>
      {plan?.balances && plan.balances.length > 0 && (
        <div className="table-wrap"><table><thead><tr><th>What</th><th>Class</th><th className="num">Amount</th><th>As of</th><th /></tr></thead>
          <tbody>{plan.balances.map((b) => <tr key={b.id}><td>{b.label}</td><td>{b.asset_class}</td><td className="num">{inrShort(b.amount)}</td><td>{shortDate(b.as_of)}</td><td><button className="btn" aria-label={`Remove ${b.label}`} onClick={() => arch.mutate(b.id)}>Remove</button></td></tr>)}</tbody></table></div>
      )}
      <div className="grid three" style={{ marginTop: 10 }}>
        <div className="field"><label htmlFor="bl">What is it?</label><input id="bl" className="input" value={f.label} onChange={(e) => setF({ ...f, label: e.target.value })} placeholder="e.g. liquid fund, PPF" /></div>
        <div className="field"><label htmlFor="bc">Counts as</label><select id="bc" className="input" value={f.cls} onChange={(e) => setF({ ...f, cls: e.target.value })}><option value="">Choose…</option><option value="equity">Equity</option><option value="gold">Gold</option><option value="debt">Debt (incl. cash-like)</option></select></div>
        <div className="field"><label htmlFor="ba">Amount (₹)</label><input id="ba" className="input" inputMode="decimal" value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} /></div>
        <div className="field"><label htmlFor="bd">Value as of</label><input id="bd" className="input" type="date" value={f.asOf} onChange={(e) => setF({ ...f, asOf: e.target.value })} /></div>
      </div>
      <button className="btn primary" disabled={f.label.trim() === "" || f.cls === "" || f.asOf === "" || !(amt >= 0) || Number.isNaN(amt) || add.isPending}
        onClick={() => add.mutate({ asset_class: f.cls as never, label: f.label.trim(), amount: amt, as_of: f.asOf }, { onSuccess: () => setF({ cls: "", label: "", amount: "", asOf: "" }) })}>{add.isPending ? "Adding…" : "Add balance"}</button>
      {add.isError && <p role="alert" style={{ color: "var(--down)" }}>{add.error instanceof Error ? add.error.message : "Not added"}</p>}
    </div>
  );
}

const mixText = (m: Mix) => `${pct(m.equity, 0)} equity · ${pct(m.gold, 1)} gold · ${pct(m.debt, 1)} debt`;

function GoalCard({ g }: { g: GoalPlan }) {
  if (g.status === "past_due" || !g.projection || !g.mix) return <div className="card"><header><h2>{g.name}</h2><Chip tone="warn">date passed</Chip></header><p>{g.headline}</p></div>;
  const pr = g.projection;
  const t = g.tradeoffs;
  return (
    <div className="card">
      <header>
        <h2>{g.name}</h2>
        <span><Chip tone={g.status === "on_track" ? "ok" : "warn"}>{g.status === "on_track" ? "ON TRACK" : "OFF TRACK"}</Chip> <Chip>{PROFILE_LABEL[g.profile ?? ""] ?? g.profile}</Chip> <Evidence kind="research" /> {g.is_test && <Chip tone="research">test data</Chip>}</span>
      </header>
      <p style={{ marginTop: 0 }}>{g.headline}</p>
      <FanChart projection={pr} target={g.target_amount} startDate={new Date()} label={`${g.name}: range of outcomes`} />
      <div className="grid tiles" style={{ marginTop: 12 }}>
        <Tile label="Reaches the target" value={pct(pr.prob_reach_target.nominal, 0)} sub={`in today's money: ${pct(pr.prob_reach_target.real, 0)} (at ${pct(pr.inputs.inflation, 0)} inflation, an assumption)`} tone={g.status === "on_track" ? "ok" : "bad"} />
        <Tile label="Middle outcome" value={inrShort(pr.percentiles["50"].nominal)} sub={`you put in ${inrShort(pr.contributed)}`} />
        <Tile label="Worst 1 in 10" value={inrShort(pr.percentiles["10"].nominal)} sub={`${pct(pr.prob_below_contributed, 0)} of scenarios end below what you put in`} />
        <Tile label="Worst fall of this mix" value={g.stress ? pct(g.stress.max_drawdown, 0) : dash} sub={g.stress ? `${shortDate(g.stress.peak)} to ${shortDate(g.stress.trough)}; ${g.stress.recovery_days == null ? "not yet recovered" : `recovered in ${g.stress.recovery_days} days`}` : undefined} />
      </div>
      <h3>Mix and why</h3>
      <p style={{ margin: "4px 0" }}><strong>{mixText(g.mix)}</strong>{g.glide ? "; equity eases down by 2 points a year over the last 5 years before the date." : "."}</p>
      <ul style={{ margin: 0, paddingLeft: "1.2em" }}>{g.profile_reasons?.map((r) => <li key={r}>{r}</li>)}</ul>
      {g.sensitivity && (
        <p className="muted" style={{ fontSize: "0.85rem" }}>
          How much the answer depends on how history is cut: {g.sensitivity.map((s) => `${s.block_months}-month blocks → ${pct(s.prob_reach_target_nominal, 0)}`).join(", ")}.
          {Math.max(...g.sensitivity.map((s) => s.prob_reach_target_nominal)) - Math.min(...g.sensitivity.map((s) => s.prob_reach_target_nominal)) > 0.1 && " That spread is large: treat the probability as rough."}
        </p>
      )}
      {g.haircut && g.haircut.length > 0 && (
        <p className="muted" style={{ fontSize: "0.85rem" }}>
          If the next years are less generous than the last {pr.inputs.history_months / 12 | 0}: {g.haircut.map((h) => `${h.points_per_year} points a year lower → ${pct(h.prob_reach_target_nominal, 0)} reach the target`).join(", ")}.
          {pr.prob_reach_target.nominal >= 0.95 && " A very high probability mostly reflects a strong decade for equity and gold, not a certainty."}
        </p>
      )}
      {t && !t.on_track && (
        <div className="notice" style={{ marginTop: 10 }}>
          <strong>Off track. What would change it (your risk level stays the same; more equity is never the answer):</strong>
          <ul style={{ margin: "6px 0 0", paddingLeft: "1.2em" }}>
            {t.monthly_contribution_needed != null && <li>Raise the monthly SIP from {inrShort(g.monthly_sip)} to about <strong>{inrShort(t.monthly_contribution_needed)}</strong>.</li>}
            {t.extra_months_needed != null ? <li>Or move the date later by about <strong>{t.extra_months_needed} months</strong>.</li> : <li>Moving the date later by up to 10 years would not be enough at this SIP.</li>}
            {t.target_reachable_at_probability != null && <li>Or aim for about <strong>{inrShort(t.target_reachable_at_probability)}</strong>, which half of the scenarios reach.</li>}
          </ul>
        </div>
      )}
      <p className="muted" style={{ fontSize: "0.78rem" }}>Run {pr.run_id}: seed {pr.inputs.seed}, {pr.inputs.paths} scenarios, {pr.inputs.contribution_timing} contributions, {pr.inputs.return_basis}.</p>
    </div>
  );
}

export function Plan() {
  const plan = usePlan();
  const profile = useProfile();
  const p = plan.data;
  const needsAnswers = p?.status === "needs_input";
  return (
    <>
      <PageHead title="Investor plan" sub="Where you stand against your goals, what (if anything) to do, and the range of outcomes. Built from your answers, your goals and real history." right={<Evidence kind="research" />} />
      <div className="notice">{p?.label ?? "A scenario from resampled history: not a forecast, not investment advice. No engine is approved."}</div>
      {plan.isLoading && <Skeleton rows={6} />}
      {plan.isError && <ErrorState error={plan.error} onRetry={() => plan.refetch()} />}
      {p && (
        <>
          {p.status === "ok" && p.rebalance && (
            <div className="card" style={{ borderColor: p.rebalance.action === "no_action" || p.rebalance.action === "direct_sip" ? "var(--up)" : "var(--warn)" }}>
              <header><h2>What to do now</h2><Chip tone={ACTION_TONE[p.rebalance.action]}>{ACTION_LABEL[p.rebalance.action]}</Chip></header>
              <p style={{ fontSize: "1.1rem", margin: "4px 0" }}>{p.rebalance.headline}</p>
              {Object.keys(p.rebalance.sip_split).length > 0 && p.rebalance.action !== "rebalance" && (
                <p className="muted" style={{ margin: 0 }}>This month's SIP, split to move toward the target: {(["equity", "gold", "debt"] as const).map((c) => `${c} ${inrShort(p.rebalance?.sip_split[c] ?? 0)}`).join(" · ")}.</p>
              )}
              <p className="muted" style={{ margin: "6px 0 0" }}>Next look: {shortDate(p.rebalance.next_review)}. {p.rebalance.tax_note}</p>
            </div>
          )}
          {p.status !== "ok" && <div className="notice" role="status">{p.headline}</div>}
          {p.status === "ok" && p.rebalance && p.rebalance.bands.length > 0 && (
            <div className="grid two">
              <div className="card">
                <header><h2>Where your money is vs the target</h2></header>
                <AllocationBars bands={p.rebalance.bands} label="Current and target allocation" />
                <p className="muted" style={{ fontSize: "0.82rem" }}>Seen: {inrShort(p.current.total_classified)} across equity, gold and debt (prices as of {p.holdings_priced_at ? shortDate(p.holdings_priced_at) : "n/a"}). The no-action zone is the tighter of 5 points or 20% of the target.</p>
              </div>
              <div className="card">
                <header><h2>If history repeated: the worst fall</h2></header>
                {p.stress && <>
                  <Tile label="Worst fall of the target mix" value={pct(p.stress.max_drawdown, 0)} sub={`${shortDate(p.stress.peak)} to ${shortDate(p.stress.trough)}`} />
                  <p className="muted" style={{ fontSize: "0.85rem" }}>{p.stress.recovery_days == null ? "It had not recovered by the end of the data." : `It took about ${p.stress.recovery_days} days to get back to the old high.`} Daily data {shortDate(p.stress.start)} to {shortDate(p.stress.end)}. History is not an upper bound: a future fall can be larger.</p>
                </>}
              </div>
            </div>
          )}
          {p.notes.length > 0 && <div className="notice" role="status"><ul style={{ margin: 0, paddingLeft: "1.2em" }}>{p.notes.map((n) => <li key={n}>{n}</li>)}</ul></div>}
          {p.goals.map((g) => <GoalCard key={g.id} g={g} />)}
          {p.goals.length === 0 && p.status !== "needs_input" && <Empty>No goals yet. Add one below.</Empty>}
        </>
      )}
      {(needsAnswers || !p) && profile.data && <Questions p={profile.data} />}
      <GoalsEditor plan={p} />
      <BalancesEditor plan={p} />
      {profile.data && !needsAnswers && <details className="card"><summary style={{ cursor: "pointer", fontWeight: 600 }}>Change my answers</summary><Questions p={profile.data} /></details>}
      {p && (
        <div className="card">
          <header><h2>How this was built</h2></header>
          <ul style={{ margin: 0, paddingLeft: "1.2em", fontSize: "0.88rem" }}>
            <li>History: {p.history.months} months, {shortDate(p.history.start)} to {shortDate(p.history.end)}.</li>
            {Object.entries(p.history.proxies).map(([k, v]) => <li key={k}><strong>{k}</strong>: {v.series}. {v.basis}</li>)}
            {p.history.notes.map((n) => <li key={n}>{n}</li>)}
            <li>Rules: {p.policy_version}. The profile is the lower of what your horizon allows and what you said you can tolerate; it is never raised to chase a goal.</li>
            <li>Not covered: tax lots and holding periods, expense ratios, fund holdings overlap, inflation as data (an assumption is used), dividends on the equity proxy.</li>
          </ul>
        </div>
      )}
    </>
  );
}
