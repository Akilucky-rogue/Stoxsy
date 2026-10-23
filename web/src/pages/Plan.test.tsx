import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { bandHalfWidth, fanGeometry } from "../components/PlanCharts";
import { Plan } from "./Plan";

const fan = { months: [0, 60, 120], "10": [0, 5e5, 1.2e6], "50": [0, 7e5, 2.1e6], "90": [0, 9e5, 3.4e6] };
const projection = {
  run_id: "abc123", n_blocks_in_history: 21, label: "SCENARIO from resampled history, not a forecast or a guarantee", contributed: 1.2e6, prob_below_contributed: 0.04,
  inputs: { months: 120, paths: 1500, block_months: 6, seed: 20261023, inflation: 0.05, history_start: "2016-01-31", history_end: "2026-09-30", history_months: 129, contribution_timing: "start of each month", return_basis: "nominal monthly proxy returns" },
  percentiles: { "10": { nominal: 1.2e6, real: 0.8e6 }, "50": { nominal: 2.1e6, real: 1.3e6 }, "90": { nominal: 3.4e6, real: 2.1e6 } }, prob_reach_target: { nominal: 0.31, real: 0.08 }, fan,
};
const goal = {
  id: 1, name: "Long-term wealth", kind: "goal", target_amount: 5e6, target_date: "2036-10-01", current_amount: 0, monthly_sip: 10000, years_left: 10, is_test: true, status: "off_track",
  headline: "In 31% of resampled scenarios this goal reaches the target. A scenario, not a forecast.", profile: "growth", profile_reasons: ["10.0 years to the goal allows at most 'growth'"], mix: { equity: 0.7, gold: 0.1, debt: 0.2 }, glide: true,
  stress: { cagr: 0.11, vol: 0.1, max_drawdown: -0.26, peak: "2020-01-17", trough: "2020-03-23", recovery_days: 158, worst_12m: -0.17, start: "2016-02-01", end: "2026-09-30", years: 10.6 }, projection,
  sensitivity: [{ block_months: 3, prob_reach_target_nominal: 0.28, median_nominal: 2e6 }, { block_months: 6, prob_reach_target_nominal: 0.31, median_nominal: 2.1e6 }, { block_months: 12, prob_reach_target_nominal: 0.45, median_nominal: 2.3e6 }],
  tradeoffs: { want_probability: 0.5, current_probability: 0.31, on_track: false, monthly_contribution_needed: 15500, extra_months_needed: 18, target_reachable_at_probability: 3.9e6, note: "not offered" },
};
const base = { as_of: "2026-10-23", label: "SCENARIO and risk framing from resampled history: not a forecast, not investment advice.", policy_version: "mode_policy_v2", emergency_fund: null, notes: ["No manual balances recorded: mutual funds are NOT in this picture."],
  history: { start: "2016-01-31", end: "2026-09-30", months: 129, proxies: { equity: { series: "NIFTYBEES", basis: "price return" } }, notes: [] },
  current: { values: { equity: 6.3e6, gold: 0.9e6, debt: 2.8e6 }, parts: { equity: [], gold: [], debt: [] }, unclassified: [], total_classified: 1e7 }, balances: [] };
const profile = { user_id: "t", mode: "investor", lens_pot: "investment", risk_profile: null, income_stability: "stable", dependents: 1, drawdown_reaction: "hold", investment_capital: null, trading_capital: null, emergency_fund: null,
  trading_share_of_investable: null, effective_limits: {}, limit_defaults: {}, limit_ceilings: {}, loosened_limits: [], engine_approved: false, warnings: [], policy_version: "mode_policy_v2", disclaimer: "", rules: { trader: { losing_streak: { stops: 3, half_size_next_trades: 5, multiplier: 0.5 }, time_stop_sessions: [5, 20], averaging_down: false }, pots: { trading_max_share_of_investable: 0.05 }, guardrails: [] } };

function mock(plan: unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/investor/plan") ? plan : url.includes("/me/profile") ? profile : []), { status: 200, headers: { "content-type": "application/json" } })));
}
const wrap = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Plan /></MemoryRouter></QueryClientProvider>);

describe("pure chart helpers", () => {
  it("the band is the tighter of 5 points and 20% of the target", () => {
    expect(bandHalfWidth(0.55)).toBe(5);
    expect(bandHalfWidth(0.10)).toBeCloseTo(2);
    expect(bandHalfWidth(0.075)).toBeCloseTo(1.5);
    expect(bandHalfWidth(0)).toBe(0);
  });
  it("the fan keeps the target and every percentile inside the plot", () => {
    const g = fanGeometry(fan, 5e6);
    for (const q of ["10", "50", "90"]) for (const v of fan[q as "10"]) { expect(g.Y(v)).toBeGreaterThanOrEqual(g.T - 1e-6); expect(g.Y(v)).toBeLessThanOrEqual(g.H - g.B + 1e-6); }
    expect(g.Y(5e6)).toBeGreaterThanOrEqual(g.T - 1e-6);
    expect(g.X(0)).toBe(g.L);
    expect(g.X(120)).toBe(g.W - g.R);
  });
});

describe("Investor plan page", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("asks for the answers it needs and does not invent a plan", async () => {
    mock({ ...base, status: "needs_input", needs: ["drawdown_reaction"], goals: [], headline: "Answer the profile questions to get a plan: drawdown reaction." });
    wrap();
    expect(await screen.findByText(/Answer the profile questions/)).toBeTruthy();
    expect(screen.getByText(/Markets fall 30% in a year/)).toBeTruthy();
    expect(screen.queryByText(/What to do now/)).toBeNull();
  });
  it("shows an explicit no-action answer, the allocation chart and an off-track goal with trade-offs but no advice to take more equity", async () => {
    mock({ ...base, status: "ok", headline: "No action needed", target: { equity: 0.55, gold: 0.1, debt: 0.35 }, goals: [goal], stress: goal.stress, holdings_priced_at: "2026-10-16",
      rebalance: { action: "no_action", headline: "No action needed: every asset class is inside its band.", bands: [{ class: "equity", current: 0.56, target: 0.55, drift_points: 1, breach: false, rule: null }, { class: "gold", current: 0.1, target: 0.1, drift_points: 0, breach: false, rule: null }, { class: "debt", current: 0.34, target: 0.35, drift_points: -1, breach: false, rule: null }],
        sip_split: { equity: 5000, gold: 1000, debt: 4000 }, trades: [], next_review: "2027-01-23", tax_note: "Tax lots are NOT loaded.", band_rule: "x" } });
    wrap();
    expect(await screen.findByText("NO ACTION NEEDED")).toBeTruthy();
    expect(screen.getByRole("img", { name: /Current and target allocation: Equity 56\.0% against a target of 55\.0%, inside its band/ })).toBeTruthy();
    expect(screen.getByRole("img", { name: /10th, 50th and 90th percentile outcomes are .* A scenario, not a forecast/ })).toBeTruthy();
    expect(screen.getByText("OFF TRACK")).toBeTruthy();
    expect(screen.getByText(/more equity is never the answer/)).toBeTruthy();
    expect(screen.getByText(/Raise the monthly SIP/)).toBeTruthy();
    expect(screen.getByText(/That spread is large/)).toBeTruthy();                       // 28% to 45% across block lengths
    expect(screen.getAllByText(/test data/).length).toBeGreaterThan(0);
  });
});
