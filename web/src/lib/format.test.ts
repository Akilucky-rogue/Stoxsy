import { describe, expect, it } from "vitest";
import { bytes, dash, num, pct, regimeTone, rupees, signed, signedPct } from "./format";

describe("format", () => {
  it("is null-safe", () => {
    for (const f of [pct, signedPct, num, signed, rupees]) { expect(f(null)).toBe(dash); expect(f(undefined)).toBe(dash); expect(f(NaN)).toBe(dash); }
  });
  it("formats percentages with sign", () => {
    expect(pct(0.1234)).toBe("12.3%");
    expect(signedPct(0.05)).toBe("+5.0%");
    expect(signedPct(-0.052)).toBe("-5.2%");
    expect(signedPct(0)).toBe("0.0%");
  });
  it("uses lakh and crore", () => {
    expect(rupees(14_400_000)).toBe("₹1.4 Cr".replace("1.4", "1.4"));
    expect(rupees(250_000)).toBe("₹2.5 L");
    expect(rupees(9_500)).toBe("₹9,500");
    expect(rupees(-2_000_000_000)).toBe("-₹200 Cr");
  });
  it("sizes", () => { expect(bytes(512)).toBe("512 B"); expect(bytes(2048)).toBe("2 KB"); expect(bytes(5 * 1048576)).toBe("5.0 MB"); });
  it("maps regimes to tones", () => { expect(regimeTone("risk_off")).toBe("bad"); expect(regimeTone("risk_on")).toBe("ok"); expect(regimeTone("x")).toBe(""); });
});
