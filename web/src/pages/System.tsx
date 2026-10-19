import { Chip, ErrorState, PageHead, Skeleton, Tile, type Tone } from "../components/ui";
import { shortDate } from "../lib/format";
import { useSystem } from "../lib/queries";

const statusTone = (s: string): Tone => (s === "validated" ? "ok" : s === "failed" ? "bad" : "warn");

export function System() {
  const s = useSystem();
  return (
    <>
      <PageHead title="Data & system" sub="What the platform is reading from and what is switched on." />
      {s.isLoading && <Skeleton rows={5} />}
      {s.isError && <ErrorState error={s.error} onRetry={() => s.refetch()} />}
      {s.data && (
        <>
          <section className="grid tiles">
            <Tile label="Database" value={s.data.database === "ok" ? "Healthy" : "Error"} tone={s.data.database === "ok" ? "ok" : "bad"} sub={s.data.database !== "ok" ? s.data.database : undefined} />
            <Tile label="Prices through" value={shortDate(s.data.ohlcv_latest)} sub={`raw archive through ${shortDate(s.data.eod_raw_latest)}`} />
            <Tile label="Sign-in" value={s.data.auth_enforced ? "Required" : "Local mode"} tone={s.data.auth_enforced ? "ok" : undefined} sub={s.data.auth_enforced ? undefined : "AUTH_ENFORCED is off"} />
            <Tile label="Scheduled downloads" value={s.data.scheduled_downloads_enabled ? "On" : "Off"} sub="any network source needs owner approval" />
          </section>

          <div className="card">
            <header><h2>Exchanges</h2></header>
            <div className="table-wrap"><table>
              <thead><tr><th>Exchange</th><th>Prices</th><th>In universe / signals / cards</th><th>Last session</th><th>Notes</th></tr></thead>
              <tbody>{Object.entries(s.data.exchanges).map(([k, v]) => (
                <tr key={k}><td><strong>{k}</strong></td><td><Chip tone={v.prices === "raw_prices_only" ? "warn" : "ok"}>{v.prices}</Chip></td><td>{v.in_universe_signals_cards ? "yes" : "no"}</td><td>{shortDate(v.last_session)}</td>
                  <td className="wrap">{v.reason ?? ""}{v.cross_listing ? ` Cross-listing: ${Object.entries(v.cross_listing).map(([a, b]) => `${a.replace(/_/g, " ")} ${b}`).join(", ")}.` : ""}</td></tr>
              ))}</tbody>
            </table></div>
          </div>

          <div className="card">
            <header><h2>Current feature snapshots</h2></header>
            <div className="table-wrap"><table>
              <thead><tr><th>Snapshot</th><th>Factor version</th><th>Universe</th><th className="num">Rows</th><th className="num">Symbols</th><th>Through</th></tr></thead>
              <tbody>{s.data.current_snapshots.map((x) => (
                <tr key={x.snapshot_id}><td className="mono">{x.snapshot_id}</td><td>{x.factor_version}</td><td className="wrap">{x.universe_version}</td><td className="num">{x.n_rows.toLocaleString("en-IN")}</td><td className="num">{x.n_symbols.toLocaleString("en-IN")}</td><td>{shortDate(x.date_max)}</td></tr>
              ))}</tbody>
            </table></div>
          </div>

          <div className="card">
            <header><h2>Corporate-action adjusted series</h2><Chip>kept separate from the official price table</Chip></header>
            <div className="table-wrap"><table>
              <thead><tr><th>Version</th><th>Kind</th><th>Validation</th><th className="num">Events</th><th className="num">Rows</th><th>Built</th></tr></thead>
              <tbody>{s.data.adjustment_series.map((x) => (
                <tr key={x.version}><td className="mono">{x.version}</td><td>{x.kind}</td><td><Chip tone={statusTone(x.validation_status)}>{x.validation_status}</Chip></td><td className="num">{x.n_events ?? "—"}</td><td className="num">{x.rows.toLocaleString("en-IN")}</td><td>{shortDate(x.built_at)}</td></tr>
              ))}</tbody>
            </table></div>
          </div>
        </>
      )}
    </>
  );
}
