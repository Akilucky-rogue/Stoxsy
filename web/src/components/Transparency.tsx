import { Chip } from "./ui";
import { num, pct } from "../lib/format";
import type { DecisionTransparency } from "../lib/api";

const inr = (x: number) => `₹${Math.round(x).toLocaleString("en-IN")}`;

/** What a card book rests on: universe version, risk inputs, exclusions, corporate-action policy, data as-of. */
export function Transparency({ t }: { t: DecisionTransparency }) {
  const ex = t.exclusions;
  const rows: [string, number][] = [
    ["Not defensive enough (bucket below the minimum)", ex.dropped_not_defensive_enough],
    ["Below the liquidity floor", ex.dropped_below_liquidity_floor],
    ["More than 35% below the 120-session high", ex.dropped_too_far_below_120d_high],
  ];
  const construction = Object.entries(ex.dropped_during_construction);
  return (
    <div className="card" aria-label="What this plan rests on">
      <header>
        <h2>What this plan rests on</h2>
        <Chip>{`signal ${t.data_as_of.signal_date}`}</Chip>
      </header>
      <div className="grid two">
        <section>
          <h3>Universe</h3>
          <ul>
            <li>{t.universe.description} ({t.universe.method}); exchange: {t.universe.exchange}.</li>
            <li>Non-equity instruments removed (ETFs, liquid funds): {t.universe.non_equity_symbols_excluded}; list version <code>{t.universe.non_equity_exclusion}</code>.</li>
            <li>{t.universe.signal_rows} names carry a defensive score today.</li>
          </ul>
          <h3>Not covered</h3>
          <ul>
            {t.not_covered && <li>BSE: {t.not_covered.bse_active_equities.toLocaleString("en-IN")} active equities are not used. {t.not_covered.reason}.</li>}
            {t.not_covered && <li>{t.not_covered.unlinked_nse_bse_pairs} NSE/BSE pairs are unlinked or under review; {t.not_covered.other_asset_classes}.</li>}
          </ul>
          <h3>Corporate actions</h3>
          <p style={{ marginTop: 0 }}>{t.corporate_actions.policy}</p>
          {t.corporate_actions.series && (
            <p className="muted" style={{ fontSize: "0.85rem" }}>
              Series in use: <code>{t.corporate_actions.series}</code> ({t.corporate_actions.events_applied} events applied).
              {t.corporate_actions.governed_registry && (
                <> Governed registry <code>{t.corporate_actions.governed_registry.version}</code> has {t.corporate_actions.governed_registry.events} events; {t.corporate_actions.governed_registry.applied_in_ohlcv_not_in_registry} events
                  are adjusted in the series in use but not in the registry, and {t.corporate_actions.governed_registry.in_registry_not_applied_in_ohlcv} registry events are not applied in it.</>
              )}
            </p>
          )}
          {t.corporate_actions.bucket_a && t.corporate_actions.bucket_a.total != null && (
            <p className="muted" style={{ fontSize: "0.85rem" }}>
              Bucket A review: {t.corporate_actions.bucket_a.decided} of {t.corporate_actions.bucket_a.total} events adjudicated ({t.corporate_actions.bucket_a.confirm} confirmed, {t.corporate_actions.bucket_a.needs_more_evidence} held,
              {" "}{t.corporate_actions.bucket_a.pending} pending). The official series is rebuilt once after all are decided.
            </p>
          )}
        </section>
        <section>
          <h3>Risk inputs</h3>
          <ul>
            <li>Capital {inr(t.risk_inputs.portfolio.capital)}; risk per position {pct(t.risk_inputs.portfolio.risk_per_position, 2)}; max position {pct(t.risk_inputs.portfolio.max_position, 0)}.</li>
            <li>Regime <strong>{t.risk_inputs.regime}</strong>: risk multiplier {num(t.risk_inputs.regime_risk_multiplier ?? 0)}, exposure cap {pct(t.risk_inputs.exposure_cap ?? 0, 0)}.</li>
            <li>Stop at {num(t.risk_inputs.stop.stop_multiple_of_daily_vol, 1)}x daily volatility (span {t.risk_inputs.stop.daily_vol_span}); time exit after {t.risk_inputs.stop.sessions} sessions.</li>
            <li>{t.risk_inputs.sizing_rule}.</li>
            <li>Signal: {t.risk_inputs.signal_components.join(", ")}. {t.risk_inputs.signal_rule}</li>
          </ul>
          <h3>Exclusions on the way to the cards</h3>
          <table>
            <tbody>
              <tr><td>Names with a defensive score</td><td className="num">{ex.signal_universe}</td></tr>
              {rows.map(([k, v]) => <tr key={k}><td>{k}</td><td className="num">-{v}</td></tr>)}
              <tr><td><strong>Eligible after filters</strong></td><td className="num"><strong>{ex.eligible_after_filters}</strong></td></tr>
              {construction.map(([k, v]) => <tr key={k}><td>Dropped while building: {k}</td><td className="num">-{v}</td></tr>)}
            </tbody>
          </table>
        </section>
      </div>
      <p className="muted" style={{ fontSize: "0.82rem" }}>
        Data as of: signal {t.data_as_of.signal_date}, prices {t.data_as_of.prices_last_date ?? "n/a"}, NSE end-of-day {t.data_as_of.eod_raw_latest ?? "n/a"}; generated {t.data_as_of.generated_at}.
        {t.notes.length > 0 && ` Missing facts: ${t.notes.join("; ")}.`}
      </p>
    </div>
  );
}
