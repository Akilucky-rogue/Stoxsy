import { Link } from "react-router-dom";
import { Chip, Empty, ErrorState, PageHead, Skeleton, Tile, type Tone } from "../components/ui";
import type { LotSecurity, TaxClass, TaxLot, TriageHolding } from "../lib/api";
import { useAuth } from "../lib/auth";
import { dash, num, rupees, shortDate, signedPct } from "../lib/format";
import { useLots, useSetTriage, useTriage } from "../lib/queries";

export const CLASS_LABEL: Record<TaxClass, string> = {
  SHORT_TERM: "Short-term", NEAR_LONG_TERM: "Turning long-term soon", LONG_TERM: "Long-term", UNKNOWN_DATE: "Purchase date not loaded", NOT_CLASSIFIED: "Not classified",
};
const CLASS_COLOR: Record<TaxClass, string> = {
  SHORT_TERM: "var(--warn)", NEAR_LONG_TERM: "var(--accent)", LONG_TERM: "var(--up)", UNKNOWN_DATE: "var(--muted)", NOT_CLASSIFIED: "var(--line)",
};
const CLASS_ORDER: TaxClass[] = ["SHORT_TERM", "NEAR_LONG_TERM", "LONG_TERM", "UNKNOWN_DATE", "NOT_CLASSIFIED"];

/** Share of the priced value in each holding-period class, in a fixed order; lots without a price are left out and counted separately. Pure so it can be tested. */
export function classShares(lots: TaxLot[]): { key: TaxClass; label: string; value: number; share: number }[] {
  const by = new Map<TaxClass, number>();
  for (const l of lots) if (l.value != null && l.value > 0) by.set(l.tax_class, (by.get(l.tax_class) ?? 0) + l.value);
  const total = [...by.values()].reduce((a, b) => a + b, 0);
  return CLASS_ORDER.filter((k) => by.has(k)).map((k) => ({ key: k, label: CLASS_LABEL[k], value: by.get(k)!, share: total ? by.get(k)! / total : 0 }));
}

export const statusTone = (s: string): Tone => (s === "RECONCILED" ? "ok" : s === "NEEDS_REVIEW" || s.startsWith("UNRECONCILED") ? "bad" : "warn");
const classTone = (c: TaxClass): Tone => (c === "LONG_TERM" ? "ok" : c === "NEAR_LONG_TERM" || c === "SHORT_TERM" ? "warn" : "");

function ClassBar({ lots }: { lots: TaxLot[] }) {
  const parts = classShares(lots);
  if (!parts.length) return <Empty>No lot has a price yet, so there is no value split to draw.</Empty>;
  const text = parts.map((p) => `${p.label} ${Math.round(p.share * 100)}%`).join(", ");
  return (
    <div>
      <div role="img" aria-label={`Value by holding period: ${text}`} style={{ display: "flex", height: 18, borderRadius: 6, overflow: "hidden", background: "var(--line)" }}>
        {parts.map((p) => <div key={p.key} title={`${p.label}: ${rupees(p.value)}`} style={{ width: `${p.share * 100}%`, background: CLASS_COLOR[p.key] }} />)}
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "6px 16px", marginTop: 8, fontSize: "0.85rem" }}>
        {parts.map((p) => <span key={p.key}><span aria-hidden style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: CLASS_COLOR[p.key], marginRight: 6 }} />{p.label} · {rupees(p.value)} ({Math.round(p.share * 100)}%)</span>)}
      </div>
    </div>
  );
}

function TriagePanel({ canWrite }: { canWrite: boolean }) {
  const t = useTriage();
  const set = useSetTriage();
  if (t.isLoading || !t.data) return null;
  const pending: { account: string; h: TriageHolding }[] = t.data.accounts.flatMap((a) => a.holdings.filter((h) => h.pot == null).map((h) => ({ account: a.account, h })));
  if (!pending.length) return null;
  return (
    <section className="card" aria-label="Assign holdings to a pot" style={{ borderColor: "var(--warn)" }}>
      <h2>{pending.length} holding{pending.length === 1 ? " is" : "s are"} not assigned to a pot</h2>
      <p className="muted">{t.data.rule}</p>
      <div className="table-wrap"><table aria-label="Holdings waiting for a pot">
        <thead><tr><th>Account</th><th>Symbol</th><th className="num">Qty</th><th className="num">Avg cost</th><th>Assign to</th></tr></thead>
        <tbody>{pending.map(({ account, h }) => (
          <tr key={`${account}-${h.symbol}`}>
            <td>{account}</td><td><strong>{h.symbol}</strong></td><td className="num">{h.quantity}</td><td className="num">{num(h.avg_cost)}</td>
            <td>{canWrite ? (
              <span style={{ display: "inline-flex", gap: 8 }}>
                <button className="btn" disabled={set.isPending} onClick={() => set.mutate({ account, assignments: { [h.symbol]: "investment" } })}>Investment pot</button>
                <button className="btn" disabled={set.isPending} onClick={() => set.mutate({ account, assignments: { [h.symbol]: "trading" } })}>Trading pot</button>
              </span>) : <span className="muted">needs a trader or admin</span>}</td>
          </tr>))}
        </tbody>
      </table></div>
      {set.isError && <ErrorState error={set.error} />}
      <p className="muted" style={{ marginTop: 8 }}>A holding never changes pot while you hold it, so choose carefully. Nothing is preselected.</p>
    </section>
  );
}

function Securities({ rows }: { rows: LotSecurity[] }) {
  const bad = rows.filter((s) => !["RECONCILED", "UNVERIFIED", "DATE_PENDING"].includes(s.status));
  if (!bad.length) return null;
  return (
    <section className="card" aria-label="Securities that need attention">
      <h2>Needs attention</h2>
      <ul>{bad.map((s) => (
        <li key={`${s.account}-${s.symbol ?? s.isin}-${s.folio ?? ""}`}>
          <strong>{s.symbol ?? s.isin}</strong> <Chip tone={statusTone(s.status)}>{s.status.replace(/_/g, " ")}</Chip>
          {s.gap_qty ? <> {s.gap_qty > 0 ? `${s.gap_qty} shares have no purchase record` : `${-s.gap_qty} more shares in the files than you hold`}</> : null}
          {s.notes.length ? <span className="muted"> {s.notes.join("; ")}</span> : null}
        </li>))}
      </ul>
    </section>
  );
}

export function Lots() {
  const { status, me } = useAuth();
  const canWrite = status === "open" || me?.role === "admin" || me?.role === "trader";
  const q = useLots();
  return (
    <>
      <PageHead title="Tax lots" sub="What you hold, by purchase date, with the day each lot turns long-term. Holding-period classes only: no tax amount is computed, nothing here is advice and no order is placed." />
      <TriagePanel canWrite={canWrite} />
      {q.isLoading && <Skeleton rows={4} />}
      {q.isError && <ErrorState error={q.error} onRetry={() => q.refetch()} />}
      {q.data && q.data.lots.length === 0 && (
        <Empty>
          <p style={{ margin: 0 }}>No lots yet. Two steps:</p>
          <ol style={{ textAlign: "left", display: "inline-block", margin: "6px 0" }}>
            <li>Import your holdings on <Link to="/portfolio">My portfolio</Link>.</li>
            <li>Then bring in your tradebook or the broker's lot report with <code>stoxsy import tradebook</code> (dry run first). There is no upload form for this step yet.</li>
          </ol>
          {q.data.notes.filter((n) => !/^no tax lots yet/i.test(n)).map((n) => <p key={n} className="muted" style={{ margin: "4px 0 0" }}>{n}</p>)}
        </Empty>
      )}
      {q.data && q.data.lots.length > 0 && (() => {
        const d = q.data;
        const dated = d.lots.filter((l) => l.buy_date).length;
        const value = d.lots.reduce((a, l) => a + (l.value ?? 0), 0);
        return (
          <>
            <div className="grid tiles">
              <Tile label="Lots" value={d.lots.length} sub={`${dated} with a purchase date`} />
              <Tile label="Priced value" value={rupees(value)} sub={`as of ${shortDate(d.as_of)}`} />
              <Tile label="Long-term" value={d.classes.LONG_TERM ?? 0} sub="lots held more than 12 months" tone="ok" />
              <Tile label="Turning long-term soon" value={d.classes.NEAR_LONG_TERM ?? 0} sub="inside the hold-lock window" />
              <Tile label="Date not loaded" value={d.classes.UNKNOWN_DATE ?? 0} sub="no purchase date in any file" tone={d.classes.UNKNOWN_DATE ? "bad" : undefined} />
            </div>
            <section className="card" aria-label="Value by holding period"><h2>Value by holding period</h2><ClassBar lots={d.lots} /></section>
            {d.near_long_term.length > 0 && (
              <section className="card" aria-label="Turning long-term soon"><h2>Turning long-term soon</h2>
                <ul>{d.near_long_term.slice(0, 12).map((l, i) => <li key={i}><strong>{l.symbol ?? l.scheme_name}</strong>: {l.quantity} from {shortDate(l.buy_date)} become long-term on {shortDate(l.long_term_from)} ({l.days_to_long_term} day{l.days_to_long_term === 1 ? "" : "s"}).</li>)}</ul>
                <p className="muted">Whether to wait is your decision with your accountant; this product only shows the date.</p>
              </section>)}
            <Securities rows={d.securities} />
            <section className="card" aria-label="All lots"><h2>All lots</h2>
              <div className="table-wrap"><table aria-label="Open tax lots">
                <thead><tr><th>Security</th><th>Pot</th><th>Bought</th><th className="num">Qty</th><th className="num">Cost</th><th className="num">Last</th><th className="num">Unrealised</th><th>Holding period</th><th>Long-term from</th><th>Check</th></tr></thead>
                <tbody>{d.lots.map((l, i) => (
                  <tr key={i}>
                    <td><strong>{l.symbol ?? l.scheme_name ?? l.isin}</strong>{l.asset === "mf" && l.plan_type ? <> <Chip>{l.plan_type}{l.option_type ? ` ${l.option_type}` : ""}</Chip></> : null}{l.folio ? <span className="muted"> folio {l.folio}</span> : null}</td>
                    <td>{l.pot ?? <Chip tone="warn">NOT ASSIGNED</Chip>}</td>
                    <td>{l.buy_date ? shortDate(l.buy_date) : dash}{l.date_basis === "bonus_ex_date_proxy" ? <Chip tone="warn" title="The allotment date is not in the NSE feed; the ex-date is used.">ex-date used</Chip> : null}</td>
                    <td className="num">{l.quantity}</td><td className="num">{num(l.unit_cost)}</td><td className="num">{num(l.last_price)}</td>
                    <td className="num" style={{ color: l.unrealised == null ? undefined : l.unrealised < 0 ? "var(--down)" : "var(--up)" }}>{l.unrealised == null ? dash : `${rupees(l.unrealised)} (${signedPct(l.unrealised_pct)})`}</td>
                    <td><Chip tone={classTone(l.tax_class)} title={l.tax_reason || undefined}>{CLASS_LABEL[l.tax_class]}</Chip></td>
                    <td>{l.long_term_from ? shortDate(l.long_term_from) : dash}</td>
                    <td><Chip tone={statusTone(l.status)} title={l.note ?? undefined}>{l.status_text}</Chip></td>
                  </tr>))}
                </tbody>
              </table></div>
            </section>
            <section className="card" aria-label="What is and is not computed">
              <h2>What this does and does not do</h2>
              <p><Chip tone="warn">RULES UNVERIFIED</Chip> {d.policy.status}</p>
              <p className="muted">Not computed: {d.policy.not_computed.join("; ")}. Not classified: {d.policy.not_classified.join("; ")}.</p>
              <p className="muted">Mutual fund statements are brought in with <code>stoxsy import mf</code>; <Link to="/portfolio">My portfolio</Link> shows the holdings view.</p>
            </section>
          </>
        );
      })()}
    </>
  );
}
