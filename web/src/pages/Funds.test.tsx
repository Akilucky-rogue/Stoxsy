import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Funds } from "./Funds";

vi.mock("../lib/auth", () => ({ useAuth: () => ({ status: "authed", me: { role: "trader" }, mustChange: false }) }));

// Layout test: the numbers and names are structural placeholders (real AMFI values are not kept in the repository).
const row = {
  scheme_code: 1, name: "Example Fund", amc: "Example AMC", category: "Example", asset_class: "equity", fund_group: "x", plan: "direct", option_type: "growth", is_etf: false, is_index: false,
  is_direct: true, is_growth: true, lifecycle: "active", last_nav: 10, last_nav_date: "2026-10-30", basis: "total_return_after_expenses", basis_text: "x", returns: {}, risk: { status: "ok", ann_volatility: 0.1, max_drawdown: -0.1 },
  quality: { level: "ok", reasons: [], flags: {} }, peers: null, snapshot_as_of: "2026-10-30",
  expense: { status: "ok", reason: null, as_of: "2026-10-28", plan: "direct", base: 0.5, brokerage: 0, transaction: 0, statutory: 0, total: 0.5, flags: [], flag_text: [] },
};

function mock() {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const body = url.includes("/funds/facets") ? { asset_classes: [], fund_groups: [], amcs: [], categories: [] } : { n_matching: 1, offset: 0, rows: [row], expense_as_of: "2026-10-28", methodology: "m", disclaimer: "d" };
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }));
}
afterEach(() => vi.unstubAllGlobals());

describe("Funds list", () => {
  it("shows the Expense column next to NAV, before the returns, so it is visible without scrolling", async () => {
    mock();
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Funds /></MemoryRouter></QueryClientProvider>);
    await screen.findByText("Example Fund");
    const heads = screen.getAllByRole("columnheader").map((h) => h.textContent);
    const at = (t: string) => heads.findIndex((h) => h === t);
    expect(at("Expense")).toBe(at("NAV") + 1);
    expect(at("Expense")).toBeLessThan(at("3M"));
  });
});
