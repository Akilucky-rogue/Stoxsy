"use client";

import { useEffect, useMemo, useState } from "react";
import { API_BASE, apiGet } from "@/lib/api";

type Signal = {
  symbol: string;
  date: string;
  horizon: string;
  strategy: string | null;
  direction: number;
  confidence: number;
  predicted_return: number | null;
  ranking_score: number;
  rank_pct: number | null;
  top_factors: Array<{ name: string; contribution: number }> | null;
  model_version: string | null;
  company_name: string | null;
  sector: string | null;
  industry: string | null;
  // Trade plan (computed server-side by qsde.risk.trade_levels).
  entry_price?: number | null;
  target_price?: number | null;
  stop_price?: number | null;
  risk_reward?: number | null;
  atr_pct?: number | null;
  trade_quality?: "good" | "low" | null;
};

type StrategyInfo = {
  strategy: string;
  label: string;
  proven: boolean;
  note: string;
  n: number;
};

type SortKey = "ranking_score" | "confidence" | "rank_pct" | "symbol" | "sector";

const HORIZONS = [
  { key: "swing", label: "SWING (5d)" },
  { key: "long", label: "LONG (20d)" },
];

type Horizon = "swing" | "long";

const HORIZON_LABEL: Record<Horizon, string> = {
  swing: "5-day",
  long: "20-day",
};

const DIRS = [
  { key: null, label: "ALL", color: "var(--text-muted)" },
  { key: 1, label: "BUY", color: "var(--accent-green)" },
  { key: 0, label: "HOLD", color: "var(--accent-amber)" },
  { key: -1, label: "SELL", color: "var(--accent-red)" },
];

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${(v * 100).toFixed(digits)}%`;
}

function fmtNum(v: number | null | undefined, digits = 3): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return v.toFixed(digits);
}

// Honest percentile label from rank_pct in [0,1] (1 = strongest). Mirrors the
// dashboard: "Top X%" / "Bottom X%" — NEVER the raw score dressed up as a return.
function rankLabel(rp: number | null | undefined): string {
  if (rp === null || rp === undefined || Number.isNaN(rp)) return "—";
  return rp >= 0.5
    ? `Top ${Math.max(1, Math.round((1 - rp) * 100))}%`
    : `Bottom ${Math.max(1, Math.round(rp * 100))}%`;
}

function dirLabel(d: number): { label: string; color: string } {
  if (d > 0) return { label: "BUY", color: "var(--accent-green)" };
  if (d < 0) return { label: "SELL", color: "var(--accent-red)" };
  return { label: "HOLD", color: "var(--accent-amber)" };
}

export default function SignalsPage() {
  const [horizon, setHorizon] = useState<Horizon>("swing");
  const [strategy, setStrategy] = useState<string>("tier1_mom");
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [direction, setDirection] = useState<number | null>(null);
  const [sectorFilter, setSectorFilter] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [sortKey, setSortKey] = useState<SortKey>("ranking_score");
  const [sortDesc, setSortDesc] = useState<boolean>(true);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [meta, setMeta] = useState<StrategyInfo | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Which strategies actually have signals for this horizon (proven first).
  useEffect(() => {
    apiGet<{ strategies: StrategyInfo[]; default: string | null }>(
      `/signals/strategies?horizon=${horizon}`,
    )
      .then((d) => {
        setStrategies(d.strategies || []);
        // If the current strategy isn't available for this horizon, fall back
        // to the server's default (the proven one).
        if (d.default && !(d.strategies || []).some((s) => s.strategy === strategy)) {
          setStrategy(d.default);
        }
      })
      .catch(() => setStrategies([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [horizon]);

  useEffect(() => {
    setLoading(true);
    setError(null);
    const url = new URL(`${API_BASE}/signals`);
    url.searchParams.set("horizon", horizon);
    url.searchParams.set("strategy", strategy);
    url.searchParams.set("limit", "500");
    if (direction !== null) url.searchParams.set("direction", String(direction));
    if (sectorFilter) url.searchParams.set("sector", sectorFilter);

    apiGet<{ signals: Signal[]; strategy_meta: StrategyInfo | null; error?: string }>(
      url.toString(),
    )
      .then((d) => {
        if (d.error) throw new Error(d.error);
        setSignals(d.signals || []);
        setMeta(d.strategy_meta || null);
        setLoading(false);
      })
      .catch((e) => {
        setSignals([]);
        setError(e?.message || "failed to load signals");
        setLoading(false);
      });
  }, [horizon, strategy, direction, sectorFilter]);

  const sectors = useMemo(
    () => Array.from(new Set(signals.map((s) => s.sector).filter(Boolean))).sort(),
    [signals],
  );

  const filtered = useMemo(() => {
    const s = search.trim().toUpperCase();
    let rows = signals;
    if (s) {
      rows = rows.filter(
        (r) =>
          r.symbol.toUpperCase().includes(s) ||
          (r.company_name || "").toUpperCase().includes(s),
      );
    }
    return [...rows].sort((a, b) => {
      const va = (a as any)[sortKey];
      const vb = (b as any)[sortKey];
      if (va === null || va === undefined) return 1;
      if (vb === null || vb === undefined) return -1;
      if (typeof va === "string") {
        return sortDesc ? vb.localeCompare(va) : va.localeCompare(vb);
      }
      return sortDesc ? vb - va : va - vb;
    });
  }, [signals, search, sortKey, sortDesc]);

  const counts = useMemo(() => {
    const buy = signals.filter((s) => s.direction > 0).length;
    const sell = signals.filter((s) => s.direction < 0).length;
    const hold = signals.filter((s) => s.direction === 0).length;
    return { buy, sell, hold, total: signals.length };
  }, [signals]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDesc(!sortDesc);
    else {
      setSortKey(key);
      setSortDesc(true);
    }
  }

  const arrow = (key: SortKey) => (sortKey === key ? (sortDesc ? " ▼" : " ▲") : "");

  const proven = meta?.proven ?? strategies.find((s) => s.strategy === strategy)?.proven ?? false;

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">⚡ SIGNALS</h1>
          <p className="page-subtitle">
            {meta?.label || strategy} · {HORIZON_LABEL[horizon]} horizon ·{" "}
            <span style={{ color: proven ? "var(--accent-green)" : "var(--accent-amber)" }}>
              {proven ? "proven net-of-cost edge" : "research / diagnostic only"}
            </span>
          </p>
        </div>
      </div>

      {/* Strategy selector — proven strategies first, dead ones labelled */}
      <div className="card" style={{ marginBottom: "var(--gap-md)" }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: "0.72rem",
              color: "var(--text-muted)",
              marginRight: 4,
            }}
          >
            STRATEGY
          </span>
          {strategies.map((st) => (
            <button
              key={st.strategy}
              onClick={() => setStrategy(st.strategy)}
              title={st.note}
              style={{
                padding: "5px 12px",
                background: strategy === st.strategy ? "var(--bg-surface)" : "transparent",
                border: `1px solid ${
                  strategy === st.strategy
                    ? st.proven
                      ? "var(--accent-green)"
                      : "var(--accent-amber)"
                    : "var(--border-color)"
                }`,
                borderRadius: "var(--radius-sm)",
                color:
                  strategy === st.strategy
                    ? st.proven
                      ? "var(--accent-green)"
                      : "var(--accent-amber)"
                    : "var(--text-muted)",
                fontFamily: "var(--font-mono)",
                fontSize: "0.72rem",
                fontWeight: strategy === st.strategy ? 700 : 400,
                cursor: "pointer",
              }}
            >
              {st.proven ? "✓ " : ""}
              {st.label}
            </button>
          ))}
        </div>
        {meta?.note && (
          <div
            style={{
              marginTop: 8,
              fontFamily: "var(--font-mono)",
              fontSize: "0.7rem",
              color: proven ? "var(--accent-green-dim)" : "var(--accent-amber)",
            }}
          >
            {meta.note}
          </div>
        )}
      </div>

      {/* Summary cards */}
      <div className="metrics-grid" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="metric-card">
          <div className="metric-label">Total Signals</div>
          <div className="metric-value">{counts.total}</div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            in current view
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">BUY</div>
          <div className="metric-value positive">{counts.buy}</div>
        </div>
        <div className="metric-card">
          <div className="metric-label">HOLD</div>
          <div className="metric-value" style={{ color: "var(--accent-amber)" }}>
            {counts.hold}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">SELL</div>
          <div className="metric-value negative">{counts.sell}</div>
        </div>
      </div>

      {/* Controls */}
      <div className="card" style={{ marginBottom: "var(--gap-md)" }}>
        <div style={{ display: "flex", gap: "var(--gap-md)", flexWrap: "wrap", alignItems: "center" }}>
          {/* Horizon toggle */}
          <div style={{ display: "flex", gap: 4 }}>
            {HORIZONS.map((h) => (
              <button
                key={h.key}
                onClick={() => setHorizon(h.key as Horizon)}
                style={{
                  padding: "6px 14px",
                  background: horizon === h.key ? "var(--accent-green-glow)" : "transparent",
                  border:
                    horizon === h.key
                      ? "1px solid rgba(0,255,136,0.3)"
                      : "1px solid var(--border-color)",
                  borderRadius: "var(--radius-sm)",
                  color: horizon === h.key ? "var(--accent-green)" : "var(--text-secondary)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.75rem",
                  cursor: "pointer",
                }}
              >
                {h.label}
              </button>
            ))}
          </div>

          {/* Direction filter */}
          <div style={{ display: "flex", gap: 4 }}>
            {DIRS.map((d) => (
              <button
                key={String(d.key)}
                onClick={() => setDirection(d.key)}
                style={{
                  padding: "6px 12px",
                  background: direction === d.key ? "var(--bg-surface)" : "transparent",
                  border: `1px solid ${direction === d.key ? d.color : "var(--border-color)"}`,
                  borderRadius: "var(--radius-sm)",
                  color: direction === d.key ? d.color : "var(--text-muted)",
                  fontFamily: "var(--font-mono)",
                  fontSize: "0.7rem",
                  fontWeight: direction === d.key ? 700 : 400,
                  cursor: "pointer",
                }}
              >
                {d.label}
              </button>
            ))}
          </div>

          {/* Sector dropdown */}
          <select
            value={sectorFilter}
            onChange={(e) => setSectorFilter(e.target.value)}
            style={{
              padding: "6px 10px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.75rem",
            }}
          >
            <option value="">All sectors</option>
            {sectors.map((s) => (
              <option key={s} value={s || ""}>
                {s}
              </option>
            ))}
          </select>

          {/* Search */}
          <input
            type="text"
            placeholder="Search symbol or company..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{
              padding: "6px 10px",
              background: "var(--bg-surface)",
              border: "1px solid var(--border-color)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.75rem",
              flex: 1,
              minWidth: 180,
            }}
          />
        </div>
      </div>

      {/* Table */}
      <div className="card">
        {loading ? (
          <div className="loading-shimmer" style={{ height: 360 }} />
        ) : error ? (
          <div
            style={{
              padding: 40,
              textAlign: "center",
              color: "var(--accent-red)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.85rem",
            }}
          >
            Couldn&apos;t load signals — {error}. Is the backend running on{" "}
            <code>{API_BASE}</code>?
          </div>
        ) : filtered.length === 0 ? (
          <div
            style={{
              padding: 40,
              textAlign: "center",
              color: "var(--text-muted)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.85rem",
            }}
          >
            No {meta?.label || strategy} signals match the current filters.
          </div>
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th onClick={() => toggleSort("symbol")} style={{ cursor: "pointer" }}>
                    Symbol{arrow("symbol")}
                  </th>
                  <th>Company</th>
                  <th onClick={() => toggleSort("sector")} style={{ cursor: "pointer" }}>
                    Sector{arrow("sector")}
                  </th>
                  <th>Direction</th>
                  <th onClick={() => toggleSort("rank_pct")} style={{ cursor: "pointer" }}>
                    Rank{arrow("rank_pct")}
                  </th>
                  <th onClick={() => toggleSort("confidence")} style={{ cursor: "pointer" }}>
                    Confidence{arrow("confidence")}
                  </th>
                  <th>Entry</th>
                  <th>Target</th>
                  <th>Stop</th>
                  <th>R:R</th>
                  <th
                    onClick={() => toggleSort("ranking_score")}
                    style={{ cursor: "pointer" }}
                    title="Relative cross-sectional factor score (NOT a forecast return)."
                  >
                    Score{arrow("ranking_score")}
                  </th>
                  <th>Top Factors</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((s, i) => {
                  const d = dirLabel(s.direction);
                  const top = (s.top_factors || []).slice(0, 3);
                  return (
                    <tr
                      key={s.symbol}
                      onClick={() => (window.location.href = `/research/${s.symbol}`)}
                      style={{ cursor: "pointer" }}
                    >
                      <td style={{ color: "var(--text-muted)", fontSize: "0.7rem" }}>{i + 1}</td>
                      <td style={{ fontWeight: 600, color: "var(--accent-cyan)" }}>{s.symbol}</td>
                      <td
                        style={{
                          maxWidth: 200,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                          color: "var(--text-secondary)",
                          fontSize: "0.75rem",
                        }}
                      >
                        {s.company_name || "-"}
                      </td>
                      <td style={{ color: "var(--text-secondary)", fontSize: "0.7rem" }}>
                        {s.sector || "-"}
                      </td>
                      <td>
                        <span
                          style={{
                            padding: "2px 8px",
                            borderRadius: "var(--radius-sm)",
                            background: `${d.color}22`,
                            color: d.color,
                            fontFamily: "var(--font-mono)",
                            fontSize: "0.7rem",
                            fontWeight: 700,
                          }}
                        >
                          {d.label}
                        </span>
                      </td>
                      <td
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: "0.72rem",
                          color:
                            (s.rank_pct ?? 0) >= 0.5
                              ? "var(--accent-green)"
                              : "var(--text-secondary)",
                        }}
                        title="Cross-sectional percentile rank within the liquid universe today (100 = strongest). This is a RANK, not a predicted return."
                      >
                        {rankLabel(s.rank_pct)}
                      </td>
                      <td>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <div
                            style={{
                              width: 60,
                              height: 4,
                              background: "var(--bg-surface)",
                              borderRadius: 2,
                              overflow: "hidden",
                            }}
                          >
                            <div
                              style={{
                                width: `${Math.max(0, Math.min(100, (s.confidence || 0) * 100))}%`,
                                height: "100%",
                                background: d.color,
                              }}
                            />
                          </div>
                          <span style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem" }}>
                            {fmtPct(s.confidence)}
                          </span>
                        </div>
                      </td>
                      <td
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: "0.72rem",
                          color: "var(--text-primary)",
                        }}
                      >
                        {s.entry_price != null ? `₹${Number(s.entry_price).toFixed(2)}` : "—"}
                      </td>
                      <td
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: "0.72rem",
                          color: s.direction === 0 ? "var(--text-muted)" : "var(--accent-green)",
                        }}
                      >
                        {s.target_price != null ? `₹${Number(s.target_price).toFixed(2)}` : "—"}
                      </td>
                      <td
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: "0.72rem",
                          color: s.direction === 0 ? "var(--text-muted)" : "var(--accent-red)",
                        }}
                      >
                        {s.stop_price != null ? `₹${Number(s.stop_price).toFixed(2)}` : "—"}
                      </td>
                      <td
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: "0.72rem",
                          color:
                            s.trade_quality === "good"
                              ? "var(--accent-green)"
                              : s.trade_quality === "low"
                              ? "var(--accent-amber)"
                              : "var(--text-muted)",
                        }}
                      >
                        {s.risk_reward != null ? Number(s.risk_reward).toFixed(2) : "—"}
                      </td>
                      <td style={{ fontFamily: "var(--font-mono)", fontSize: "0.75rem" }}>
                        {fmtNum(s.ranking_score, 3)}
                      </td>
                      <td>
                        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
                          {top.length === 0 && (
                            <span style={{ color: "var(--text-muted)", fontSize: "0.7rem" }}>-</span>
                          )}
                          {top.map((f, fi) => (
                            <span
                              key={fi}
                              style={{
                                padding: "1px 6px",
                                background: "var(--bg-surface)",
                                border: "1px solid var(--border-color)",
                                borderRadius: 3,
                                fontFamily: "var(--font-mono)",
                                fontSize: "0.65rem",
                                color:
                                  (f.contribution || 0) > 0
                                    ? "var(--accent-green-dim)"
                                    : "var(--accent-red-dim)",
                              }}
                            >
                              {f.name}
                            </span>
                          ))}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div
        style={{
          marginTop: "var(--gap-md)",
          fontFamily: "var(--font-mono)",
          fontSize: "0.7rem",
          color: "var(--text-muted)",
        }}
      >
        Click any row to drill into research view. Click column headers to sort. &quot;Rank&quot;
        is a cross-sectional percentile, and &quot;Score&quot; is a relative factor score — neither
        is a predicted return. Measured net edge lives on the{" "}
        <a href="/scorecard" style={{ color: "var(--accent-cyan)" }}>
          Scorecard
        </a>
        .
      </div>
    </div>
  );
}
