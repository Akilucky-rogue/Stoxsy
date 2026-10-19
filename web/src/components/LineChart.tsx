import { useId } from "react";

export interface Series { name: string; values: number[]; color: string }

/** Dependency-free SVG line chart with an accessible label and a table equivalent for the first/last/min/max points. */
export function LineChart({ dates, series, label, format = (v: number) => v.toFixed(1), height = 220, zero = false }:
  { dates: string[]; series: Series[]; label: string; format?: (v: number) => string; height?: number; zero?: boolean }) {
  const id = useId();
  const W = 720, H = height, P = { l: 48, r: 12, t: 10, b: 22 };
  const all = series.flatMap((s) => s.values).filter(Number.isFinite);
  if (dates.length < 2 || all.length === 0) return <p className="muted">Not enough data to draw this chart.</p>;
  let lo = Math.min(...all), hi = Math.max(...all);
  if (zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (hi === lo) { hi += 1; lo -= 1; }
  const x = (i: number) => P.l + (i / (dates.length - 1)) * (W - P.l - P.r);
  const y = (v: number) => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
  const path = (vs: number[]) => vs.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const ticks = [lo, (lo + hi) / 2, hi];
  return (
    <figure style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-labelledby={`${id}-t`} style={{ display: "block", maxWidth: "100%" }}>
        <title id={`${id}-t`}>{label}</title>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={P.l} x2={W - P.r} y1={y(t)} y2={y(t)} stroke="var(--line)" strokeWidth="1" />
            <text x={P.l - 6} y={y(t) + 4} textAnchor="end" fontSize="11" fill="var(--muted)">{format(t)}</text>
          </g>
        ))}
        {zero && <line x1={P.l} x2={W - P.r} y1={y(0)} y2={y(0)} stroke="var(--muted)" strokeWidth="1" strokeDasharray="3 3" />}
        <text x={P.l} y={H - 4} fontSize="11" fill="var(--muted)">{dates[0]}</text>
        <text x={W - P.r} y={H - 4} fontSize="11" fill="var(--muted)" textAnchor="end">{dates[dates.length - 1]}</text>
        {series.map((s) => <path key={s.name} d={path(s.values)} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round" />)}
      </svg>
      <figcaption style={{ display: "flex", gap: 14, flexWrap: "wrap", fontSize: "0.82rem", color: "var(--muted)" }}>
        {series.map((s) => <span key={s.name}><span aria-hidden="true" style={{ color: s.color }}>●</span> {s.name}: {format(s.values[s.values.length - 1])} now, low {format(Math.min(...s.values))}, high {format(Math.max(...s.values))}</span>)}
      </figcaption>
    </figure>
  );
}
