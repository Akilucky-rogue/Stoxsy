import type { ReactNode } from "react";

// ── icons: tiny inline SVG set (24px grid, stroke) ──────────────────
const PATHS: Record<string, string> = {
  home: "M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z",
  flask: "M9 3h6M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3",
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  users: "M16 20v-1a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v1M10 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM20 20v-1a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8",
  menu: "M4 6h16M4 12h16M4 18h16",
  sun: "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z",
  system: "M3 5h18v11H3zM8 21h8M12 16v5",
  chevron: "M15 6l-6 6 6 6",
  out: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9",
  lock: "M6 11V8a6 6 0 0 1 12 0v3M5 11h14v10H5z",
  layers: "M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17.5l9 5 9-5",
  wallet: "M3 7a2 2 0 0 1 2-2h13v4M3 7v10a2 2 0 0 0 2 2h14a1 1 0 0 0 1-1V9a1 1 0 0 0-1-1H5a2 2 0 0 1-2-1zM16 14h.01",
  target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 12h.01",
};

export function Icon({ name, size = 20 }: { name: string; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false">
      <path d={PATHS[name] ?? PATHS.home} />
    </svg>
  );
}

export function Logo({ size = 34 }: { size?: number }) {
  return (
    <span className="logo" style={{ width: size, height: size }} aria-hidden="true">
      <svg width={size * 0.7} height={size * 0.7} viewBox="0 0 32 32" fill="none" stroke="#2fd4a0" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round"><path d="M7 21l6-7 4 4 8-10" /></svg>
    </span>
  );
}

// ── evidence chips: every statistic says how much to trust it ───────
export type Tone = "ok" | "warn" | "bad" | "research" | "";
export function Chip({ tone = "", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`chip ${tone}`.trim()} title={title}>{children}</span>;
}

export function Evidence({ kind }: { kind: "research" | "paper" | "approved" | "diagnostic" }) {
  const map = {
    research: ["research", "RESEARCH ONLY", "A research result: not shown to be a tradable edge."],
    diagnostic: ["research", "DIAGNOSTIC", "Diagnostic output; not advice."],
    paper: ["warn", "PAPER", "Tracked on paper; not approved."],
    approved: ["ok", "APPROVED", "Approved by the owner after the offline gate and live-paper evidence."],
  } as const;
  const [tone, label, title] = map[kind];
  return <Chip tone={tone as Tone} title={title}>{label}</Chip>;
}

export function Tile({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value" style={tone === "bad" ? { color: "var(--down)" } : tone === "ok" ? { color: "var(--up)" } : undefined}>{value}</div>
      {sub != null && <div className="sub">{sub}</div>}
    </div>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return <div aria-busy="true" aria-label="Loading" style={{ display: "grid", gap: 10 }}>{Array.from({ length: rows }, (_, i) => <div className="skeleton" key={i} style={{ width: `${90 - i * 12}%` }} />)}</div>;
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const msg = error instanceof Error ? error.message : "Something went wrong";
  return (
    <div role="alert" className="error">
      <strong>Could not load this.</strong> {msg}{" "}
      {onRetry && <button className="btn" onClick={onRetry} style={{ marginLeft: 8, padding: "4px 10px" }}>Retry</button>}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function PageHead({ title, sub, right }: { title: string; sub?: ReactNode; right?: ReactNode }) {
  return (
    <div className="page-head">
      <div><h1>{title}</h1>{sub && <p>{sub}</p>}</div>
      {right}
    </div>
  );
}

export function Segmented<T extends string | number>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div className="seg" role="group" aria-label={label}>
      {options.map((o) => <button key={String(o.value)} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>{o.label}</button>)}
    </div>
  );
}

export const BUCKET_LABEL: Record<number, string> = { 1: "Highest risk", 2: "High", 3: "Middle", 4: "Low", 5: "Most defensive" };

export function BucketProfile({ profile }: { profile: Record<string, { n: number; median_atr_pct: number; median_vol60: number; median_dd120: number; median_liq: number }> }) {
  const pc = (x: number, d = 1) => `${(x * 100).toFixed(d)}%`;
  return (
    <div className="table-wrap">
      <table aria-label="What each defensive bucket looks like">
        <thead><tr><th>Bucket</th><th className="num">Names</th><th className="num">Median daily range</th><th className="num">Median vol (60d)</th><th className="num">Median from 120d high</th></tr></thead>
        <tbody>
          {[5, 4, 3, 2, 1].map((b) => {
            const p = profile[String(b)];
            return p ? (
              <tr key={b}>
                <td><span aria-hidden="true" style={{ display: "inline-block", width: 10, height: 10, borderRadius: 3, marginRight: 8, background: `var(--bucket-${b})` }} />{b} · {BUCKET_LABEL[b]}</td>
                <td className="num">{p.n}</td><td className="num">{pc(p.median_atr_pct)}</td><td className="num">{pc(p.median_vol60)}</td><td className="num">{pc(p.median_dd120, 0)}</td>
              </tr>
            ) : null;
          })}
        </tbody>
      </table>
    </div>
  );
}
