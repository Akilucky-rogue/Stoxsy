import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TaxLot } from "../lib/api";
import { classShares, Lots, statusTone } from "./Lots";

vi.mock("../lib/auth", () => ({ useAuth: () => ({ status: "authed", me: { role: "trader" }, mustChange: false }) }));

const lot = (o: Partial<TaxLot>): TaxLot => ({
  account: "choice", asset: "equity", symbol: "AAA", isin: null, folio: null, scheme_name: null, plan_type: null, option_type: null, pot: "trading", buy_date: "2025-01-10", quantity: 10, unit_cost: 100,
  last_price: 120, price_date: "2026-10-23", value: 1200, unrealised: 200, unrealised_pct: 0.2, tax_class: "LONG_TERM", long_term_from: "2026-01-11", days_to_long_term: 0, tax_reason: "", status: "RECONCILED",
  status_text: "matches your holdings", origin: "buy", date_basis: "trade", note: null, ...o,
});

const report = {
  as_of: "2026-10-25", policy: { version: "tax_policy_v1", status: "UNVERIFIED. Confirm with your accountant.", not_computed: ["tax amounts at any rate"], not_classified: ["debt funds"] }, accounts: [{ account: "choice", computed_at: "2026-10-25T10:00:00" }],
  lots: [lot({}), lot({ symbol: "BBB", tax_class: "NEAR_LONG_TERM", value: 800, days_to_long_term: 12, long_term_from: "2026-11-06", buy_date: "2025-11-05" }), lot({ symbol: "CCC", buy_date: null, tax_class: "UNKNOWN_DATE", status: "DATE_PENDING", status_text: "purchase date not loaded yet", value: 400, long_term_from: null, days_to_long_term: null, pot: null })],
  securities: [{ account: "choice", asset: "equity", symbol: "DDD", isin: null, status: "UNRECONCILED_LOT_GAP", source: "tradebook_fifo", gap_qty: 15, notes: ["load an older tradebook"], n_lots: 2 }],
  classes: { LONG_TERM: 1, NEAR_LONG_TERM: 1, UNKNOWN_DATE: 1 }, statuses: {}, notes: [],
  near_long_term: [lot({ symbol: "BBB", tax_class: "NEAR_LONG_TERM", days_to_long_term: 12, long_term_from: "2026-11-06", buy_date: "2025-11-05" })],
};
const triage = { accounts: [{ account: "choice", holdings: [{ symbol: "CCC", quantity: 5, avg_cost: 50, pot: null }, { symbol: "AAA", quantity: 10, avg_cost: 100, pot: "trading" }] }], unassigned: ["CCC"], rule: "Every holding belongs to one pot." };

function mock(lots: unknown, tri: unknown, calls: { url: string; method: string; body?: string }[] = []) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, method: init?.method ?? "GET", body: init?.body as string | undefined });
    const body = url.includes("/portfolio/triage") ? tri : url.includes("/portfolio/lots") ? lots : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  }));
}
const wrap = () => render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><MemoryRouter><Lots /></MemoryRouter></QueryClientProvider>);
afterEach(() => vi.unstubAllGlobals());

describe("classShares", () => {
  it("splits priced value by holding period in a fixed order and ignores unpriced lots", () => {
    const s = classShares([lot({ value: 600 }), lot({ tax_class: "SHORT_TERM", value: 300 }), lot({ tax_class: "UNKNOWN_DATE", value: 100 }), lot({ tax_class: "LONG_TERM", value: null })]);
    expect(s.map((x) => x.key)).toEqual(["SHORT_TERM", "LONG_TERM", "UNKNOWN_DATE"]);
    expect(s.map((x) => Math.round(x.share * 100))).toEqual([30, 60, 10]);
    expect(classShares([])).toEqual([]);
  });
  it("status tones: reconciled is fine, gaps and review are bad, pending is a warning", () => {
    expect(statusTone("RECONCILED")).toBe("ok");
    expect(statusTone("UNRECONCILED_LOT_GAP")).toBe("bad");
    expect(statusTone("NEEDS_REVIEW")).toBe("bad");
    expect(statusTone("DATE_PENDING")).toBe("warn");
  });
});

describe("Lots page", () => {
  it("shows lots with pot, class and status; says plainly when a date or pot is missing; carries the rules-unverified warning", async () => {
    mock(report, { accounts: [], unassigned: [], rule: "" });
    wrap();
    expect(await screen.findByText("Tax lots")).toBeTruthy();
    const table = await screen.findByRole("table", { name: "Open tax lots" });
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(within(table).getByText("NOT ASSIGNED")).toBeTruthy();
    expect(within(table).getByText("Purchase date not loaded")).toBeTruthy();
    expect(screen.getByText("RULES UNVERIFIED")).toBeTruthy();
    expect(screen.getByRole("img", { name: /Value by holding period: Turning long-term soon 33%, Long-term 50%, Purchase date not loaded 17%/ })).toBeTruthy();
    expect(screen.getByText(/15 shares have no purchase record/)).toBeTruthy();
  });

  it("offers no preselected pot and records the owner's choice for the unassigned holding", async () => {
    const calls: { url: string; method: string; body?: string }[] = [];
    mock(report, triage, calls);
    wrap();
    const panel = await screen.findByRole("region", { name: "Assign holdings to a pot" });
    expect(within(panel).getAllByRole("row")).toHaveLength(2);                       // header + the one unassigned holding; AAA already has a pot
    await userEvent.click(within(panel).getByRole("button", { name: "Trading pot" }));
    const put = calls.find((c) => c.method === "PUT");
    expect(put?.url).toBe("/api/portfolio/triage");
    expect(JSON.parse(put!.body!)).toEqual({ account: "choice", assignments: { CCC: "trading" } });
  });

  it("an empty book explains how to bring files in", async () => {
    mock({ ...report, lots: [], notes: ["no tax lots yet"] }, { accounts: [], unassigned: [], rule: "" });
    wrap();
    expect(await screen.findByText(/stoxsy import holdings/)).toBeTruthy();
  });
});
