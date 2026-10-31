import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { FundExpense, FundRow } from "../lib/api";
import { CostsPanel, ExpenseCell, QualityBadge, RetCell } from "./FundDetail";

describe("fund cells", () => {
  it("shows an explicit reason instead of a number when a horizon is not valid", () => {
    render(<RetCell annual c={{ status: "insufficient_history", detail: "first NAV is 2025-06-01" }} />);
    expect(screen.getByText(/n\/a/)).toBeTruthy();
    expect(screen.getByText(/insufficient history/)).toBeTruthy();
  });
  it("annualises only where asked and shows cumulative otherwise", () => {
    const c = { status: "ok" as const, cumulative: 0.5, annualised: 0.1, start: "2023-01-01", end: "2026-01-01" };
    const { container, rerender } = render(<RetCell annual c={c} />);
    expect(container.textContent).toContain("10.0%");
    rerender(<RetCell annual={false} c={c} />);
    expect(container.textContent).toContain("50.0%");
  });
  it("labels data quality with the reasons available on hover", () => {
    render(<QualityBadge q={{ level: "poor", reasons: ["unit change in 2022"], flags: {} }} />);
    expect(screen.getByText("data poor").getAttribute("title")).toContain("unit change in 2022");
  });
});

// Rendering tests: the numbers are structural placeholders (real AMFI values are not kept in the repository; the live API is checked against the database by hand).
const ok = (over: Partial<FundExpense> = {}): FundExpense => ({
  status: "ok", reason: null, as_of: "2026-10-28", plan: "regular", base: 1.5, brokerage: 0.05, transaction: 0.02, statutory: 0.3, total: 1.87, flags: [], flag_text: [], ...over,
});

describe("expense cell", () => {
  it("shows the base ratio and puts the total as published in the hover text", () => {
    render(<ExpenseCell e={ok()} />);
    const el = screen.getByText("1.50%");
    expect(el.getAttribute("title")).toContain("1.87%");
    expect(el.getAttribute("title")).toContain("as published");
  });
  it("says why there is no figure instead of showing a number or a zero", () => {
    const { rerender } = render(<ExpenseCell e={ok({ status: "not_offered", reason: "AMFI lists no expense ratio for this plan", base: null, total: null })} />);
    expect(screen.getByText("not offered")).toBeTruthy();
    rerender(<ExpenseCell e={ok({ status: "not_covered", reason: "no match", base: null, total: null, as_of: null })} />);
    expect(screen.getByText("no AMFI figure").getAttribute("title")).toContain("no match");
    rerender(<ExpenseCell e={undefined} />);
    expect(screen.getByText("—")).toBeTruthy();
  });
  it("labels an older figure with its date", () => {
    render(<ExpenseCell e={ok({ status: "stale", reason: "older figure: AMFI's latest row for this scheme is from 2026-09-30", as_of: "2026-09-30" })} />);
    expect(screen.getByText(/older/)).toBeTruthy();
    expect(screen.getByText("1.50%")).toBeTruthy();
  });
  it("keeps a flagged figure visible and adds a warning with the reason", () => {
    render(<ExpenseCell e={ok({ base: 3.4, total: 4.1, flags: ["total_gt_3_regular"], flag_text: ["The Regular plan's total expense is above 3% as published by AMFI."] })} />);
    expect(screen.getByText("3.40%")).toBeTruthy();
    expect(screen.getByText("check").getAttribute("title")).toContain("above 3%");
  });
});

describe("costs panel", () => {
  const fund = { expense: ok() } as unknown as FundRow;
  const hist = [
    { month: "2026-09", as_of: "2026-09-30", base: 1.5, brokerage: 0.04, transaction: 0.01, statutory: 0.29, total: 1.84 },
    { month: "2026-10", as_of: "2026-10-28", base: 1.5, brokerage: 0.05, transaction: 0.02, statutory: 0.3, total: 1.87 },
  ];
  it("lists every part as published and says the trading costs reset monthly", () => {
    const { container } = render(<CostsPanel fund={fund} history={hist} />);
    for (const t of ["Base expense ratio", "Brokerage", "Transaction cost", "Statutory levies", "Total (as published)"]) expect(container.textContent).toContain(t);
    expect(container.textContent).toMatch(/reset/i);
    expect(container.textContent).toContain("1.87%");
  });
  it("shows one history row per month", () => {
    render(<CostsPanel fund={fund} history={hist} />);
    expect(screen.getByText("2026-09")).toBeTruthy();
    expect(screen.getByText("2026-10")).toBeTruthy();
  });
  it("renders nothing when the fund has no expense object", () => {
    const { container } = render(<CostsPanel fund={{} as FundRow} history={[]} />);
    expect(container.textContent).toBe("");
  });
});
