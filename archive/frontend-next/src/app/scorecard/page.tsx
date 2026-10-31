"use client";

import { useEffect, useState } from "react";

import { API_BASE } from "@/lib/api";

// Scorecard page — the Reality Engine read-out. Shows each engine's MEASURED
// net-of-cost edge from the mechanically-resolved idea-log, plus the idea-log
// itself. Accuracy is reported here, never promised.
//   * GET  /api/scorecard         -> per-engine expectancy / hit / payoff / brier
//   * GET  /api/scorecard/ideas   -> the idea-log (open + resolved)
//   * POST /api/scorecard/resolve -> settle matured ideas (cash barriers + condors)

type Block = {
  n: number;
  hit_rate: number;
  expectancy: number;
  payoff_ratio: number;
  avg_win: number;
  avg_loss: number;
  n_win: number;
  n_loss: number;
  brier?: number;
};

type Report = {
  n_resolved: number;
  overall: Block | null;
  by_engine: Record<string, Block>;
  message?: string;
};

type Idea = {
  idea_id: string;
  engine: string;
  symbol: string;
  structure: string;
  horizon: string;
  status: string;
  max_loss_rupees: number | null;
  confidence: number | null;
  realized_ret_net: number | null;
  expiry: string | null;
  created_at: string;
};

const pct = (x: number | null | undefined) =>
  x == null ? "—" : `${(x * 100).toFixed(1)}%`;
const signed = (x: number | null | undefined, dp = 4) =>
  x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(dp)}`;

export default function ScorecardPage() {
  const [report, setReport] = useState<Report | null>(null);
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = () => {
    fetch(`${API_BASE}/scorecard`).then((r) => r.json()).then(setReport).catch(() => {});
    fetch(`${API_BASE}/scorecard/ideas?limit=200`)
      .then((r) => r.json())
      .then((d) => setIdeas(d.ideas || []))
      .catch(() => {});
  };

  useEffect(() => { refresh(); }, []);

  const onResolve = async () => {
    setBusy(true);
    setNotice(null);
    try {
      const r = await fetch(`${API_BASE}/scorecard/resolve`, { method: "POST" });
      const d = await r.json();
      setNotice(
        `resolved — cash: ${d.cash?.closed ?? 0} closed · condors: ${d.condors?.closed ?? 0} closed` +
        (d.condors?.no_settlement ? ` (${d.condors.no_settlement} awaiting settlement)` : "")
      );
      refresh();
    } catch {
      setNotice("resolve failed");
    } finally {
      setBusy(false);
    }
  };

  const engines = report ? Object.entries(report.by_engine) : [];
  const edgeColor = (e: number) =>
    e > 0 ? "var(--accent-green)" : e < 0 ? "var(--accent-red)" : "var(--text-muted)";
  const statusColor = (s: string) =>
    s === "WIN" ? "var(--accent-green)" :
    s === "LOSS" ? "var(--accent-red)" :
    s === "OPEN" ? "var(--accent-cyan)" : "var(--text-muted)";

  return (
    <div className="fade-in">
      <div className="page-header">
        <div>
          <h1 className="page-title">▣ SCORECARD</h1>
          <p className="page-subtitle">
            Measured net-of-cost edge per engine · resolved from the idea-log, nothing hand-picked
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <button
            disabled={busy}
            onClick={onResolve}
            style={{
              cursor: busy ? "wait" : "pointer",
              padding: "6px 14px",
              background: "var(--accent-green-glow)",
              border: "1px solid var(--accent-green)",
              color: "var(--accent-green)",
              borderRadius: "var(--radius-sm)",
              fontFamily: "var(--font-mono)",
              fontSize: "0.78rem",
            }}
          >
            Resolve matured ideas
          </button>
          {notice && (
            <span style={{ color: "var(--text-secondary)", fontFamily: "var(--font-mono)", fontSize: "0.72rem" }}>
              {notice}
            </span>
          )}
        </div>
      </div>

      {/* Summary tiles */}
      <div className="metrics-grid" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="metric-card">
          <div className="metric-label">Resolved ideas</div>
          <div className="metric-value" style={{ color: "var(--accent-cyan)" }}>
            {report?.n_resolved ?? "…"}
          </div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            {ideas.length} in the idea-log
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Overall expectancy</div>
          <div className="metric-value" style={{ color: edgeColor(report?.overall?.expectancy ?? 0) }}>
            {report?.overall ? signed(report.overall.expectancy) : "—"}
          </div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            mean per-trade net return · the goal, not accuracy
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Overall hit rate</div>
          <div className="metric-value">{pct(report?.overall?.hit_rate)}</div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            W/L {report?.overall?.n_win ?? 0}/{report?.overall?.n_loss ?? 0}
          </div>
        </div>
        <div className="metric-card">
          <div className="metric-label">Calibration (Brier)</div>
          <div className="metric-value">
            {report?.overall?.brier != null ? report.overall.brier.toFixed(3) : "—"}
          </div>
          <div className="metric-change" style={{ color: "var(--text-muted)" }}>
            0 = perfect · 0.25 = coin-flip
          </div>
        </div>
      </div>

      {/* Per-engine measured edge */}
      <div className="card" style={{ marginBottom: "var(--gap-lg)" }}>
        <div className="card-header">
          <span className="card-title">▸ Measured edge by engine</span>
          <span style={{ color: "var(--text-muted)", fontSize: "0.72rem" }}>
            expectancy is net-of-cost · per-engine return convention
          </span>
        </div>
        {report?.message ? (
          <div style={{ padding: "30px", textAlign: "center", color: "var(--text-muted)" }}>
            {report.message}
          </div>
        ) : (
          <div style={{ overflow: "auto" }}>
            <table style={{ width: "100%", fontFamily: "var(--font-mono)", fontSize: "0.78rem", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                  <th style={{ padding: "8px 12px" }}>Engine</th>
                  <th style={{ padding: "8px 12px" }}>n</th>
                  <th style={{ padding: "8px 12px" }}>Hit</th>
                  <th style={{ padding: "8px 12px" }}>Expectancy</th>
                  <th style={{ padding: "8px 12px" }}>Edge</th>
                  <th style={{ padding: "8px 12px" }}>Payoff</th>
                  <th style={{ padding: "8px 12px" }}>W/L</th>
                  <th style={{ padding: "8px 12px" }}>Brier</th>
                </tr>
              </thead>
              <tbody>
                {engines.map(([name, b]) => (
                  <tr key={name} style={{ borderTop: "1px solid var(--border-color)" }}>
                    <td style={{ padding: "8px 12px", color: "var(--text-primary)" }}><strong>{name}</strong></td>
                    <td style={{ padding: "8px 12px" }}>{b.n}</td>
                    <td style={{ padding: "8px 12px" }}>{pct(b.hit_rate)}</td>
                    <td style={{ padding: "8px 12px", color: edgeColor(b.expectancy) }}>{signed(b.expectancy)}</td>
                    <td style={{ padding: "8px 12px", color: edgeColor(b.expectancy) }}>
                      {b.expectancy > 0 ? "EDGE" : "no edge"}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-cyan)" }}>
                      {isFinite(b.payoff_ratio) ? b.payoff_ratio.toFixed(2) : "∞"}
                    </td>
                    <td style={{ padding: "8px 12px" }}>
                      <span style={{ color: "var(--accent-green)" }}>{b.n_win}</span>
                      /<span style={{ color: "var(--accent-red)" }}>{b.n_loss}</span>
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--text-muted)" }}>
                      {b.brier != null ? b.brier.toFixed(3) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* The idea-log */}
      <div className="card">
        <div className="card-header">
          <span className="card-title">▸ Idea-log</span>
          <span style={{ color: "var(--text-muted)", fontSize: "0.72rem" }}>
            every emitted idea, logged at emit time · the audit trail
          </span>
        </div>
        {ideas.length === 0 ? (
          <div style={{ padding: "30px", textAlign: "center", color: "var(--text-muted)" }}>
            No ideas logged yet. Run the engines (daily EOD or the snapshot scripts).
          </div>
        ) : (
          <div style={{ overflow: "auto" }}>
            <table style={{ width: "100%", fontFamily: "var(--font-mono)", fontSize: "0.74rem", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ color: "var(--text-muted)", textAlign: "left" }}>
                  <th style={{ padding: "8px 12px" }}>Created</th>
                  <th style={{ padding: "8px 12px" }}>Engine</th>
                  <th style={{ padding: "8px 12px" }}>Symbol</th>
                  <th style={{ padding: "8px 12px" }}>Structure</th>
                  <th style={{ padding: "8px 12px" }}>Conf</th>
                  <th style={{ padding: "8px 12px" }}>Max loss</th>
                  <th style={{ padding: "8px 12px" }}>Status</th>
                  <th style={{ padding: "8px 12px" }}>Net return</th>
                </tr>
              </thead>
              <tbody>
                {ideas.slice(0, 100).map((it) => (
                  <tr key={it.idea_id} style={{ borderTop: "1px solid var(--border-color)" }}>
                    <td style={{ padding: "8px 12px", color: "var(--text-secondary)" }}>
                      {String(it.created_at).slice(0, 10)}
                    </td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-cyan)" }}>{it.engine}</td>
                    <td style={{ padding: "8px 12px", color: "var(--text-primary)", fontWeight: 600 }}>{it.symbol}</td>
                    <td style={{ padding: "8px 12px", color: "var(--text-muted)" }}>{it.structure}</td>
                    <td style={{ padding: "8px 12px" }}>{it.confidence != null ? it.confidence.toFixed(2) : "—"}</td>
                    <td style={{ padding: "8px 12px", color: "var(--accent-red)" }}>
                      {it.max_loss_rupees != null ? `₹${it.max_loss_rupees.toFixed(0)}` : "—"}
                    </td>
                    <td style={{ padding: "8px 12px", color: statusColor(it.status) }}>{it.status}</td>
                    <td style={{
                      padding: "8px 12px",
                      color: (it.realized_ret_net ?? 0) >= 0 ? "var(--accent-green)" : "var(--accent-red)",
                    }}>
                      {it.realized_ret_net != null
                        ? `${it.realized_ret_net >= 0 ? "+" : ""}${(it.realized_ret_net * 100).toFixed(2)}%`
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
