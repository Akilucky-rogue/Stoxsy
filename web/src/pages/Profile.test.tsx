import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PotBar, Profile } from "./Profile";

const profile = {
  user_id: "t", mode: "investor", lens_pot: "investment", risk_profile: null, investment_capital: 1000000, trading_capital: 30000, emergency_fund: null, trading_share_of_investable: 30000 / 1030000,
  effective_limits: { risk_per_trade_pct: 0.25, open_risk_pct: 3, daily_loss_pct: 1.5, weekly_loss_pct: 4, max_positions: 8, max_pair_correlation: 0.55, liquidity_cap_pct: 1 },
  limit_defaults: { risk_per_trade_pct: 0.25, open_risk_pct: 3, daily_loss_pct: 1.5, weekly_loss_pct: 4, max_positions: 8, max_pair_correlation: 0.55, liquidity_cap_pct: 1 },
  limit_ceilings: { risk_per_trade_pct: 0.5, open_risk_pct: 3, daily_loss_pct: 1.5, weekly_loss_pct: 4, max_positions: 8, max_pair_correlation: 0.55, liquidity_cap_pct: 1 },
  loosened_limits: [], engine_approved: false, warnings: ["emergency fund not recorded: investor plans will state that the constraint is unknown"], policy_version: "mode_policy_v1", disclaimer: "Research and decision support, not investment advice.",
  rules: { trader: { losing_streak: { stops: 3, half_size_next_trades: 5, multiplier: 0.5 }, time_stop_sessions: [5, 20], averaging_down: false }, pots: { trading_max_share_of_investable: 0.05 }, guardrails: ["Both modes distinguish RESEARCH, PAPER and APPROVED."] },
};

function mockFetch() {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/audit") ? [] : profile), { status: 200, headers: { "content-type": "application/json" } })));
}
const wrap = (ui: React.ReactNode) => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter>{ui}</MemoryRouter></QueryClientProvider>);

describe("Profile page", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("shows the pots, the unknown emergency fund, the ceilings and that no engine is approved", async () => {
    mockFetch();
    wrap(<Profile />);
    expect(await screen.findByText("Investment pot")).toBeTruthy();
    expect(screen.getAllByText(/emergency fund not recorded/).length).toBeGreaterThan(0);
    expect(screen.getByText(/No engine is approved/)).toBeTruthy();
    expect(screen.getByRole("img", { name: /trading pot ₹30,000 \(2\.9%\)/ })).toBeTruthy();
  });
  it("asks for an approval note only when a limit is loosened, and blocks values above the ceiling", async () => {
    mockFetch();
    wrap(<Profile />);
    const risk = await screen.findByLabelText(/Risk per trade/);
    expect(screen.queryByLabelText(/Approval note/)).toBeNull();
    fireEvent.change(risk, { target: { value: "0.5" } });
    expect(await screen.findByLabelText(/Approval note/)).toBeTruthy();
    fireEvent.change(risk, { target: { value: "1" } });
    expect(await screen.findByText(/cannot exceed 0\.5/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Save limits" })).toBeDisabled();
  });
});

describe("PotBar", () => {
  it("flags a trading pot above the guard", () => {
    render(<PotBar investment={900} trading={100} emergency={null} cap={0.05} />);
    expect(screen.getByRole("img").getAttribute("aria-label")).toMatch(/trading pot ₹100 \(10\.0%\).*Guard: trading at most 5%/);
  });
  it("handles no capital", () => {
    render(<PotBar investment={0} trading={0} emergency={null} cap={0.05} />);
    expect(screen.getByRole("img").getAttribute("aria-label")).toBe("No capital recorded yet.");
  });
});
