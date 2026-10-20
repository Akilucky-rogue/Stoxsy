import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { QualityBadge, RetCell } from "./FundDetail";

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
