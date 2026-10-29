import { useEffect, useState } from "react";
import { Chip, Empty, ErrorState, Evidence, PageHead, Segmented, Skeleton, Tile } from "../components/ui";
import type { AlertRow } from "../lib/api";
import { useAcknowledgeAlert, useAlertSummary, useAlerts, useRefreshAlerts } from "../lib/queries";

const TONE = { critical: "bad", warning: "warn", info: "" } as const;
const RULE_LABEL: Record<string, string> = {
  data_staleness: "Data freshness", source_health: "Source health", ca_unreviewed: "Corporate actions", ca_conflicts: "Corporate actions", ca_upcoming: "Corporate actions",
  model_kill_switch: "Model", model_degradation: "Model", model_drift: "Model", model_log_missed: "Model logging", model_log_due: "Model logging", kite_token: "Kite session",
  portfolio_concentration: "Portfolio", portfolio_correlation: "Portfolio", portfolio_drawdown: "Portfolio", portfolio_exposure: "Portfolio", alert_rule_error: "Alert engine",
};
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "—");

function Row({ a, canAck, onAck, busy }: { a: AlertRow; canAck: boolean; onAck: (id: number) => void; busy: boolean }) {
  return (
    <li style={{ listStyle: "none", borderTop: "1px solid var(--line)", padding: "12px 0", opacity: a.state === "resolved" ? 0.7 : 1 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
        <Chip tone={TONE[a.severity]}>{a.severity}</Chip>
        <strong>{a.title}</strong>
        <span className="muted" style={{ fontSize: "0.8rem" }}>{RULE_LABEL[a.rule_id] ?? a.rule_id} · {a.scope === "you" ? "your portfolio" : "system"}</span>
        <span style={{ marginLeft: "auto" }} />
        {a.state === "open" && canAck && <button className="btn" disabled={busy} onClick={() => onAck(a.id)}>Acknowledge</button>}
        {a.state === "acknowledged" && <Chip title={`by ${a.acked_by ?? "?"} at ${when(a.acked_at)}`}>acknowledged</Chip>}
        {a.state === "resolved" && <Chip tone="ok" title={a.resolved_why ?? undefined}>resolved</Chip>}
      </div>
      <p style={{ margin: "6px 0 0" }}>{a.detail}</p>
      <p className="muted" style={{ fontSize: "0.78rem", margin: "4px 0 0" }}>
        first seen {when(a.first_seen)} · last checked {when(a.last_seen)}{a.occurrences > 1 && ` · still true on ${a.occurrences} checks`}{a.resolved_at && ` · cleared ${when(a.resolved_at)}`}
      </p>
    </li>
  );
}

export function Alerts() {
  const [state, setState] = useState("live");
  const [sev, setSev] = useState("");
  const list = useAlerts(state, sev);
  const sum = useAlertSummary();
  const refresh = useRefreshAlerts();
  const ack = useAcknowledgeAlert();
  const { mutate } = refresh;
  useEffect(() => { mutate(); }, [mutate]);                      // evaluate on arrival: the system-wide rules are skipped if they ran in the last minute
  const o = sum.data?.open;

  return (
    <>
      <PageHead title="Alerts" sub="What needs your attention: stale or failing data, corporate-action conflicts, model health, portfolio risk and the Kite session."
        right={<><Evidence kind="diagnostic" /> <button className="btn" onClick={() => refresh.mutate()} disabled={refresh.isPending}>{refresh.isPending ? "Checking…" : "Check now"}</button></>} />
      <div className="notice">
        Alerts are recomputed from current data each time they are checked: a condition that keeps holding is one alert, and it resolves itself when the condition clears.
        Acknowledging only marks that you have seen it. Alerts are shown here only; nothing is sent outside this application, and nothing can place or change an order.
      </div>
      <section className="grid tiles" aria-label="Open alerts">
        <Tile label="Critical" value={String(o?.critical ?? "—")} tone={o && o.critical > 0 ? "bad" : "ok"} sub="open, needs action" />
        <Tile label="Warning" value={String(o?.warning ?? "—")} sub="open, worth a look" />
        <Tile label="Info" value={String(o?.info ?? "—")} sub="open, for awareness" />
        <Tile label="Acknowledged" value={String(sum.data?.acknowledged ?? "—")} sub={sum.data?.last_run ? `last full check ${when(sum.data.last_run)}` : "no check yet"} />
      </section>
      {refresh.isError && <p role="alert" style={{ color: "var(--down)" }}>The check failed: {refresh.error instanceof Error ? refresh.error.message : "unknown error"}</p>}
      {refresh.data && refresh.data.errors.length > 0 && <p role="alert" className="muted">{refresh.data.errors.length} check(s) could not run; they appear below as alerts.</p>}

      <div className="card">
        <header>
          <h2 style={{ marginRight: "auto" }}>{state === "resolved" ? "Resolved alerts" : "Alerts"}</h2>
          <Segmented label="State" value={state} onChange={setState} options={[{ value: "live", label: "Current" }, { value: "open", label: "Not yet acknowledged" }, { value: "resolved", label: "Resolved" }, { value: "all", label: "All" }]} />
          <Segmented label="Severity" value={sev} onChange={setSev} options={[{ value: "", label: "Any" }, { value: "critical", label: "Critical" }, { value: "warning", label: "Warning" }, { value: "info", label: "Info" }]} />
        </header>
        {list.isLoading && <Skeleton rows={4} />}
        {list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} />}
        {list.data && (list.data.rows.length === 0 ? <Empty>{state === "live" ? "Nothing needs attention right now." : "No alerts match."}</Empty> : (
          <ul style={{ margin: 0, padding: 0 }}>
            {list.data.rows.map((a) => <Row key={a.id} a={a} canAck onAck={(id) => ack.mutate(id)} busy={ack.isPending} />)}
          </ul>
        ))}
        {ack.isError && <p role="alert" style={{ color: "var(--down)" }}>{ack.error instanceof Error ? ack.error.message : "Could not acknowledge"}</p>}
      </div>
    </>
  );
}
