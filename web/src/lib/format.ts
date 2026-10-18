// Number formatting. Indian conventions where they matter (lakh / crore); everything null-safe because the API returns nulls.

export const dash = "—";

export function pct(x: number | null | undefined, digits = 1): string {
  return x == null || !Number.isFinite(x) ? dash : `${(x * 100).toFixed(digits)}%`;
}

export function signedPct(x: number | null | undefined, digits = 1): string {
  if (x == null || !Number.isFinite(x)) return dash;
  const v = (x * 100).toFixed(digits);
  return `${x > 0 ? "+" : ""}${v}%`;
}

export function num(x: number | null | undefined, digits = 2): string {
  return x == null || !Number.isFinite(x) ? dash : x.toFixed(digits);
}

export function signed(x: number | null | undefined, digits = 2): string {
  if (x == null || !Number.isFinite(x)) return dash;
  return `${x > 0 ? "+" : ""}${x.toFixed(digits)}`;
}

/** Rupees as lakh / crore ("₹ 1.44 Cr" means 1.44 crore = 14.4 million). */
export function rupees(x: number | null | undefined): string {
  if (x == null || !Number.isFinite(x)) return dash;
  const a = Math.abs(x);
  const s = x < 0 ? "-" : "";
  if (a >= 1e7) return `${s}₹${(a / 1e7).toFixed(a >= 1e9 ? 0 : 1)} Cr`;
  if (a >= 1e5) return `${s}₹${(a / 1e5).toFixed(1)} L`;
  return `${s}₹${Math.round(a).toLocaleString("en-IN")}`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return dash;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

export const REGIME_LABEL: Record<string, string> = {
  risk_on: "Risk-on", neutral: "Neutral", risk_off: "Risk-off", unknown: "Unknown",
};
export function regimeTone(r: string | null | undefined): "ok" | "warn" | "bad" | "" {
  return r === "risk_on" ? "ok" : r === "risk_off" ? "bad" : r === "neutral" ? "warn" : "";
}
