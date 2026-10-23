import type { BandRow, Mix, Projection } from "../lib/api";
import { pct } from "../lib/format";

const inr = (x: number) => (Math.abs(x) >= 1e7 ? `₹${(x / 1e7).toFixed(2)} Cr` : Math.abs(x) >= 1e5 ? `₹${(x / 1e5).toFixed(1)} L` : `₹${Math.round(x).toLocaleString("en-IN")}`);
export { inr as inrShort };

const COLORS: Record<keyof Mix, string> = { equity: "var(--brand)", gold: "var(--warn)", debt: "var(--accent)" };
const LABEL: Record<keyof Mix, string> = { equity: "Equity", gold: "Gold", debt: "Debt" };

/** Half-width of a class's no-action band, in percentage points: the tighter of 5 points absolute and 20% of the target. */
export function bandHalfWidth(target: number, absPoints = 5, rel = 0.2): number {
  return Math.min(absPoints, target * 100 * rel);
}

/** Geometry of the fan: x and y scales over months and value, with the target kept in range. Pure so it can be tested. */
export function fanGeometry(fan: Projection["fan"], target: number, W = 640, H = 260, L = 64, R = 14, T = 12, B = 30) {
  const n = fan.months.length;
  const maxM = fan.months[n - 1] || 1;
  const lo = Math.min(0, ...fan["10"]);
  const hi = Math.max(target * 1.05, ...fan["90"]);
  const X = (m: number) => L + (m / maxM) * (W - L - R);
  const Y = (v: number) => T + (1 - (v - lo) / (hi - lo || 1)) * (H - T - B);
  const line = (q: string) => fan.months.map((m, i) => `${X(m).toFixed(1)},${Y(fan[q][i]).toFixed(1)}`).join(" ");
  const band = `${fan.months.map((m, i) => `${X(m).toFixed(1)},${Y(fan["90"][i]).toFixed(1)}`).join(" ")} ${[...fan.months].reverse().map((m, i) => `${X(m).toFixed(1)},${Y(fan["10"][n - 1 - i]).toFixed(1)}`).join(" ")}`;
  return { X, Y, line, band, lo, hi, maxM, W, H, L, R, T, B };
}

/** The range of outcomes for one goal: a shaded 10th to 90th percentile band, the middle line, and the target. The lines draw in on first view. */
export function FanChart({ projection, target, startDate, label }: { projection: Projection; target: number; startDate: Date; label: string }) {
  const g = fanGeometry(projection.fan, target);
  const p = projection.percentiles;
  const years = projection.inputs.months / 12;
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => g.lo + (g.hi - g.lo) * f);
  const endYear = new Date(startDate.getTime() + projection.inputs.months * 30.44 * 86400000).getFullYear();
  return (
    <figure style={{ margin: 0 }}>
      <svg role="img" aria-label={`${label}. Over ${years.toFixed(1)} years the 10th, 50th and 90th percentile outcomes are ${inr(p["10"].nominal)}, ${inr(p["50"].nominal)} and ${inr(p["90"].nominal)} against a target of ${inr(target)}. A scenario, not a forecast.`}
        viewBox={`0 0 ${g.W} ${g.H}`} style={{ width: "100%", height: "auto" }}>
        <style>{`.fan-draw{stroke-dasharray:1400;stroke-dashoffset:1400;animation:fandraw 1.4s ease forwards}@keyframes fandraw{to{stroke-dashoffset:0}}@media (prefers-reduced-motion:reduce){.fan-draw{animation:none;stroke-dashoffset:0}}`}</style>
        {ticks.map((v, i) => (
          <g key={i}><line x1={g.L} x2={g.W - g.R} y1={g.Y(v)} y2={g.Y(v)} stroke="currentColor" opacity={0.12} /><text x={g.L - 6} y={g.Y(v) + 4} textAnchor="end" fontSize="11" fill="currentColor">{inr(v)}</text></g>
        ))}
        <polygon points={g.band} fill="var(--brand)" opacity={0.16} />
        <polyline className="fan-draw" points={g.line("90")} fill="none" stroke="var(--up)" strokeWidth={1.5} />
        <polyline className="fan-draw" points={g.line("10")} fill="none" stroke="var(--down)" strokeWidth={1.5} />
        <polyline className="fan-draw" points={g.line("50")} fill="none" stroke="var(--brand)" strokeWidth={2.5} />
        <line x1={g.L} x2={g.W - g.R} y1={g.Y(target)} y2={g.Y(target)} stroke="var(--ink)" strokeDasharray="5 4" />
        <text x={g.W - g.R} y={g.Y(target) - 5} textAnchor="end" fontSize="11" fill="currentColor">target {inr(target)}</text>
        <text x={g.L} y={g.H - 8} fontSize="11" fill="currentColor">now</text>
        <text x={g.W - g.R} y={g.H - 8} textAnchor="end" fontSize="11" fill="currentColor">{endYear}</text>
        <text x={g.W - g.R - 4} y={g.Y(p["90"].nominal) - 4} textAnchor="end" fontSize="11" fill="var(--up)">best 1 in 10: {inr(p["90"].nominal)}</text>
        <text x={g.W - g.R - 4} y={g.Y(p["10"].nominal) + 14} textAnchor="end" fontSize="11" fill="var(--down)">worst 1 in 10: {inr(p["10"].nominal)}</text>
      </svg>
      <figcaption className="muted" style={{ fontSize: "0.82rem" }}>
        The shaded band holds the middle 80% of {projection.inputs.paths.toLocaleString("en-IN")} resampled scenarios; the thick line is the middle one. Built from {projection.inputs.history_months} months of real history
        ({projection.inputs.history_start.slice(0, 7)} to {projection.inputs.history_end.slice(0, 7)}) in {projection.inputs.block_months}-month blocks, so only about {projection.n_blocks_in_history} independent blocks: treat it as a rough range.
      </figcaption>
    </figure>
  );
}

/** Current against target for each asset class, with the no-action band drawn around the target. A breach is shown in red. */
export function AllocationBars({ bands, label }: { bands: BandRow[]; label: string }) {
  const W = 620, rowH = 46, L = 70, R = 60, H = bands.length * rowH + 28;
  const X = (f: number) => L + Math.min(Math.max(f, 0), 1) * (W - L - R);
  const desc = bands.map((b) => `${LABEL[b.class]} ${pct(b.current, 1)} against a target of ${pct(b.target, 1)}${b.breach ? ", outside its band" : ", inside its band"}`).join("; ");
  return (
    <figure style={{ margin: 0 }}>
      <svg role="img" aria-label={`${label}: ${desc}`} viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "auto" }}>
        {bands.map((b, i) => {
          const y = 12 + i * rowH;
          const hw = bandHalfWidth(b.target) / 100;
          return (
            <g key={b.class}>
              <text x={L - 8} y={y + 18} textAnchor="end" fontSize="12" fill="currentColor">{LABEL[b.class]}</text>
              <rect x={L} y={y + 4} width={W - L - R} height={22} rx={6} fill="var(--surface-2)" stroke="var(--line)" />
              <rect x={X(b.target - hw)} y={y + 1} width={X(b.target + hw) - X(b.target - hw)} height={28} rx={4} fill="var(--up)" opacity={0.18} />
              <rect x={L} y={y + 4} width={Math.max(0, X(b.current) - L)} height={22} rx={6} fill={b.breach ? "var(--down)" : COLORS[b.class]} style={{ transition: "width .7s ease" }} />
              <line x1={X(b.target)} x2={X(b.target)} y1={y - 2} y2={y + 32} stroke="var(--ink)" strokeWidth={2} />
              <text x={W - R + 6} y={y + 12} fontSize="11" fill="currentColor">now {pct(b.current, 1)}</text>
              <text x={W - R + 6} y={y + 26} fontSize="11" fill="var(--muted)">goal {pct(b.target, 1)}</text>
            </g>
          );
        })}
        <text x={L} y={H - 4} fontSize="11" fill="var(--muted)">black line = target · green zone = no action needed · red = outside the zone</text>
      </svg>
    </figure>
  );
}
