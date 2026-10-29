import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { actionTone, meterTone, meterWidth, Trader } from "./Trader";

vi.mock("../lib/auth", () => ({ useAuth: () => ({ status: "authed", me: { role: "trader" }, mustChange: false }) }));

const meters = {
  capital: 1e6, open_risk: 9000, open_risk_pct: 0.9, open_risk_limit_pct: 3, daily_pnl: -6000, daily_pnl_pct: -0.6, daily_loss_limit_pct: 1.5, daily_limit_used: 0.4,
  weekly_pnl: 0, weekly_pnl_pct: 0, weekly_loss_limit_pct: 4, weekly_limit_used: 0, positions: 1, max_positions: 8, size_multiplier: 0.5, size_multiplier_reasons: ["losing streak: 3 stops in a row, so the next 5 trade(s) are half size"],
  streak: { stops_in_row: 3, reduced_trades_left: 5 }, invested: 100000, free_cash: 900000,
};
const card = {
  symbol: "CYIENT", venue: "NSE cash", setup: "donchian20_breakout", direction: "long", horizon_id: "swing_5d", evidence: "RESEARCH", evidence_note: "RESEARCH: a candidate from a rule baseline. No engine is approved and no edge has been shown.",
  entry: 1233, entry_rule: "buy at or just below the planned entry", stop: 1184.2, stop_pct: 0.0396, stop_in_daily_moves: 1.8, target: 1314.33, target_r: 1.67, time_stop: "2026-10-30", till_when: "exit by the time stop (5 sessions)",
  qty: 36, position_value: 44388, risk_rupees: 2448, risk_pct_of_trading_capital: 0.245, expected_cost_rupees: 100, loss_per_unit: 68, sizing: { binding_limit: "risk_per_trade", multiplier: 1 }, invalidated_if: ["the price closes below 1184.20"], as_of: "2026-10-23",
};
const desk = {
  book: [
    { source: "imported", id: null, symbol: "ZEEL", qty: 1500, entry: 148, stop: null, emergency_stop: 124.2, target: null, last: 126.5, risk_rupees: 36000, protected: false, unrealised: -32250, time_stop: null, action: "SET_STOP_NOW", why: "no stop is recorded: the risk of this position is unbounded" },
    { source: "paper idea", id: 3, symbol: "TRENT", qty: 25, entry: 7420, stop: 7420, target: 7900, last: 7810, risk_rupees: 1000, protected: true, unrealised: 9750, time_stop: "2026-11-05", action: "HOLD_AS_PLANNED", why: "inside its stop, before its time stop" },
  ], unprotected: ["ZEEL"],
  disclaimer: "Research and decision support, not investment advice.", engine_approved: false, trading_capital: 1e6, meters, limits: {}, open: [], mode: "trader", needs: [],
  headline: "1 research candidate(s) pass your risk rules today (of 2 breakouts checked). They are baselines with no shown edge.",
  candidates: [
    { symbol: "CYIENT", breakout_pct: 0.007, volume_x_median: 43, risk_bucket: 4, allowed: true, card, reasons: [], notes: [] },
    { symbol: "BADCO", breakout_pct: 0.02, volume_x_median: 2, risk_bucket: 3, allowed: false, card: null, reasons: ["correlation with open position ABC is 0.71; the maximum is 0.55"], notes: [] },
  ],
};

function mock(payload: unknown) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(url.includes("/trader/desk") ? payload : url.includes("/trader/performance") ? { n_closed: 0, note: "no closed ideas yet: nothing to measure", min_for_judgement: 60, disclaimer: "" } : []), { status: 200, headers: { "content-type": "application/json" } })));
}
const wrap = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Trader /></MemoryRouter></QueryClientProvider>);
afterEach(() => vi.unstubAllGlobals());

describe("meter helpers", () => {
  it("tones: fine under 60%, close to 100%, bad once the limit is reached", () => {
    expect(meterTone(0.3)).toBe("ok");
    expect(meterTone(0.6)).toBe("warn");
    expect(meterTone(0.99)).toBe("warn");
    expect(meterTone(1)).toBe("bad");
    expect(meterTone(2.4)).toBe("bad");
  });
  it("urgent commands are red, trailing is amber, holding is green", () => {
    expect(actionTone("SET_STOP_NOW")).toBe("bad");
    expect(actionTone("EXIT_STOP_BREACHED")).toBe("bad");
    expect(actionTone("RAISE_STOP_TO_COST")).toBe("warn");
    expect(actionTone("HOLD_AS_PLANNED")).toBe("ok");
  });
  it("width is clamped to the track", () => {
    expect(meterWidth(-1)).toBe("0%");
    expect(meterWidth(0.456)).toBe("46%");
    expect(meterWidth(3)).toBe("100%");
  });
});

describe("Trader desk", () => {
  it("shows the limits, the sized card with its evidence label, a refusal with its reason, and the reduced-size notice", async () => {
    mock(desk);
    wrap();
    expect(await screen.findByText(/pass your risk rules today/)).toBeTruthy();
    expect(screen.getByText("CYIENT")).toBeTruthy();
    expect(screen.getAllByText("RESEARCH ONLY").length).toBeGreaterThan(0);
    expect(screen.getByText(/36 sh/)).toBeTruthy();
    expect(screen.getByText(/correlation with open position ABC is 0.71/)).toBeTruthy();
    expect(screen.getByText(/Sizes are reduced \(0.5x\)/)).toBeTruthy();
    expect(screen.getByText(/This idea is wrong if:/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Log as a paper idea/ })).toBeTruthy();
    expect(screen.getByRole("progressbar", { name: /Open risk: 30% of the limit used/ })).toBeTruthy();
  });

  it("lists the existing book first, most urgent command at the top, with a stop field for the unprotected import", async () => {
    mock(desk);
    wrap();
    expect(await screen.findByText(/Manage what you already hold/)).toBeTruthy();
    expect(screen.getByText("SET A STOP NOW")).toBeTruthy();
    expect(screen.getByText("HOLD AS PLANNED")).toBeTruthy();
    expect(screen.getByLabelText("Stop for ZEEL")).toBeTruthy();
    expect(screen.getByText(/Suggested emergency stop: 124.2/)).toBeTruthy();
    const rows = screen.getAllByRole("row").map((r) => r.textContent ?? "");
    expect(rows.findIndex((t) => t.includes("ZEEL"))).toBeLessThan(rows.findIndex((t) => t.includes("TRENT")));
  });

  it("asks for the trading pot and sizes nothing when it is not set", async () => {
    mock({ ...desk, trading_capital: null, needs: ["trading_capital"], candidates: [], headline: "Set your trading pot on the Profile page to see sized ideas: nothing is assumed." });
    wrap();
    expect(await screen.findByText(/Enter your trading pot on the Profile page/)).toBeTruthy();
    expect(screen.queryByText(/Log as a paper idea/)).toBeNull();
  });
});
