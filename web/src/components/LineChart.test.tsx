import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LineChart } from "./LineChart";

describe("LineChart", () => {
  it("exposes an accessible label and a text equivalent of the series", () => {
    render(<LineChart label="Value rebased to 100" dates={["2026-01-01", "2026-01-02", "2026-01-03"]} series={[{ name: "Mine", values: [100, 90, 110], color: "red" }]} format={(v) => v.toFixed(0)} />);
    expect(screen.getByRole("img", { name: "Value rebased to 100" })).toBeTruthy();
    expect(screen.getByText(/Mine: 110 now, low 90, high 110/)).toBeTruthy();
  });
  it("says so instead of drawing nothing when there is too little data", () => {
    render(<LineChart label="x" dates={["2026-01-01"]} series={[{ name: "Mine", values: [1], color: "red" }]} />);
    expect(screen.getByText(/Not enough data/)).toBeTruthy();
  });
});
