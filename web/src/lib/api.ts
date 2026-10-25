// Typed API client. The access token lives in memory only (never localStorage); the refresh token is an httpOnly cookie the browser
// sends to /api/auth. On a 401 the client refreshes ONCE (single-flight, shared by concurrent requests) and retries the call.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

let accessToken: string | null = null;
let inflight: Promise<boolean> | null = null;
const listeners = new Set<(signedIn: boolean) => void>();

export function setAccessToken(t: string | null) {
  accessToken = t;
}
export function getAccessToken() {
  return accessToken;
}
export function onSessionChange(fn: (signedIn: boolean) => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}
function emit(signedIn: boolean) {
  listeners.forEach((l) => l(signedIn));
}

const AUTH_PATHS = ["/api/auth/login", "/api/auth/refresh", "/api/auth/logout"];

async function send(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  return fetch(path, { ...init, headers, credentials: "include" });
}

/** Exchange the refresh cookie for a new access token. Concurrent callers share one request. */
export function refreshSession(): Promise<boolean> {
  if (!inflight) {
    inflight = (async () => {
      try {
        const r = await fetch("/api/auth/refresh", { method: "POST", credentials: "include", headers: { "X-Stoxsy-CSRF": "1" } });
        if (!r.ok) {
          accessToken = null;
          emit(false);
          return false;
        }
        const body = await r.json();
        accessToken = body.access_token;
        emit(true);
        return true;
      } catch {
        return false;
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}

async function detail(r: Response): Promise<string> {
  try {
    const j = await r.json();
    if (typeof j.detail === "string") return j.detail;
    if (Array.isArray(j.detail)) return j.detail.map((d: string | { msg?: string }) => (typeof d === "string" ? d : d.msg)).filter(Boolean).join("; ") || r.statusText;
  } catch {
    /* not JSON */
  }
  return r.statusText || `HTTP ${r.status}`;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  let r = await send(path, init);
  if (r.status === 401 && !AUTH_PATHS.includes(path) && (await refreshSession())) r = await send(path, init);
  if (!r.ok) throw new ApiError(r.status, await detail(r));
  return (await r.json()) as T;
}

export const post = <T>(path: string, body?: unknown) => api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });

// ── response shapes (mirror api/routes/*.py) ───────────────────────

export interface SessionUser { id: string; email: string; name: string; role: "admin" | "trader" | "viewer"; status: string; must_change_password: boolean }
export interface Session { access_token: string; expires_in: number; user: SessionUser; must_change_password: boolean }
export interface Me { id: string; email: string; role: "admin" | "trader" | "viewer"; authenticated: boolean; must_change_password: boolean; auth_enforced: boolean }

export interface DefensiveRow {
  symbol: string; defensive_score: number; risk_bucket: number | null; atr_pct: number | null; vol60: number | null;
  downside60: number | null; dd120: number | null; liq: number | null; warnings: string;
}
export interface SchemeStats { ann_return: number; ann_vol: number; sharpe: number; max_dd: number; calmar: number; downside_dev: number; worst_month: number; n_days: number }
export interface DefensiveEval {
  period: [string, string]; sessions: number; one_way_cost_bps: number; non_equity_excluded?: string;
  schemes: Record<string, SchemeStats>;
  vs_equal_weight: Record<string, { sharpe_diff: number; sharpe_diff_ci: [number, number]; maxdd_diff: number; maxdd_diff_ci: [number, number]; vol_ratio: number; vol_ratio_ci: [number, number] }>;
}
export interface DefensiveLatest { as_of: string | null; regime: string | null; n: number; bucket_counts: Record<string, number>; bucket_profile: Record<string, { n: number; median_atr_pct: number; median_vol60: number; median_dd120: number; median_liq: number }>; rows: DefensiveRow[]; evaluation: DefensiveEval | null; disclaimer: string }
export interface SystemStatus {
  database: string; ohlcv_latest: string | null; eod_raw_latest: string | null;
  current_snapshots: { snapshot_id: string; factor_version: string; universe_version: string; n_rows: number; n_symbols: number; date_max: string; built_at: string }[];
  series_labels?: Record<string, string>;
  adjustment_series: { version: string; kind: string; validation_status: string; n_events: number | null; rows: number; built_at: string }[];
  auth_enforced: boolean; promotion_enabled: boolean; scheduled_downloads_enabled: boolean; notes: string[];
  exchanges: Record<string, { prices: string; in_universe_signals_cards: boolean; last_session?: string | null; sessions?: number; cross_listing?: Record<string, number>; reason?: string }>;
}
export interface ReportItem { group: string; name: string; title: string; bytes: number; modified: string; superseded: boolean }
export interface ReportBody { group: string; name: string; markdown: string }
export interface AdminUser { id: string; email: string; name: string; role: string; status: string; must_change_pw: boolean; last_login_at: string | null; created_at: string }
export interface AuditRow { id: number; ts: string; user_id: string | null; email: string | null; action: string; ok: boolean; ip: string | null }

export interface DecisionCard {
  symbol: string; instrument_type: string; venues: string[]; defensive_score: number; risk_bucket: number; last_close: number; daily_vol: number;
  quantity: number; value: number; weight: number; entry: string; entry_date: string; stop: number; stop_distance: number; loss_if_stopped: number;
  loss_pct_of_capital: number; sized_by: string; review_by: string; exit_by: string; triggers: string[]; reasons: string[]; warnings: string[]; evidence: string;
}
export interface DecisionSummary {
  as_of: string; regime: string; regime_risk_multiplier: number; exposure_cap: number; horizon: string; horizon_sessions: number; entry_date: string; exit_by: string;
  dates_are_estimates: boolean; capital: number; positions: number; invested: number; invested_pct: number; cash: number; largest_weight: number;
  average_pairwise_correlation: number | null; loss_if_all_stopped: number; loss_if_all_stopped_pct: number; candidates_considered: number;
  skipped: [string, string][]; evidence: string; notes: string[];
}
export interface DecisionTransparency {
  risk_inputs: { portfolio: { capital: number; risk_per_position: number; max_position: number }; regime: string | null; regime_risk_multiplier: number | null; exposure_cap: number | null;
    stop: { horizon: string; sessions: number; stop_multiple_of_daily_vol: number; daily_vol_span: number }; sizing_rule: string; signal_components: string[]; signal_rule: string };
  exclusions: { signal_universe: number; dropped_not_defensive_enough: number; dropped_below_liquidity_floor: number; dropped_too_far_below_120d_high: number;
    eligible_after_filters: number; dropped_during_construction: Record<string, number> };
  universe: { method: string; description: string; non_equity_exclusion: string; non_equity_symbols_excluded: number; signal_rows: number; exchange: string };
  not_covered?: { bse_active_equities: number; reason: string; unlinked_nse_bse_pairs: number; other_asset_classes: string };
  corporate_actions: { policy: string; series?: string | null; validation?: string | null; events_applied?: number | null; built_at?: string | null;
    ohlcv_version?: { retained_snapshot: string; created_at: string; reason: string; rows: number; sha256: string } | null;
    bucket_a?: { total: number | null; confirm: number; needs_more_evidence: number; reject: number; pending: number | null; decided: number;
      held?: { symbol: string; ex_date: string; reason: string; residual?: number; sigma?: number; in_universe?: boolean; best_rank?: number | null }[];
      rejected?: { symbol: string; ex_date: string; reason: string; raw_gap?: number | null }[]; note?: string };
    governed_registry?: { label?: string; version: string | null; events: number; applied_in_ohlcv_not_in_registry: number; in_registry_not_applied_in_ohlcv: number } };
  data_as_of: { signal_date: string; prices_last_date?: string | null; eod_raw_latest?: string | null; ohlcv_latest?: string | null; generated_at: string };
  notes: string[];
}
export interface DecisionBook { cards: DecisionCard[]; summary: DecisionSummary; transparency?: DecisionTransparency; disclaimer: string }

export type ReturnCell = { status: "ok"; cumulative: number; annualised: number | null; start: string; end: string } | { status: "insufficient_history" | "stale_start" | "nav_discontinuity"; detail: string };
export interface RiskCell { status: string; detail?: string; ann_volatility?: number; max_drawdown?: number; current_drawdown?: number; downside_deviation?: number; observations?: number; start?: string; end?: string }
export interface FundQuality { level: "ok" | "caution" | "poor"; reasons: string[]; flags: Record<string, number | boolean> }
export interface FundPeers { key: string; group_size: number; horizons: Record<string, { percentile: number | null; median: number; n: number }> }
export interface FundRow {
  scheme_code: number; name: string; amc: string | null; category: string | null; asset_class: string | null; fund_group: string | null; plan: string | null; option_type: string | null;
  is_etf: boolean; is_index: boolean; is_direct: boolean | null; is_growth: boolean | null; lifecycle: string | null; last_nav: number | null; last_nav_date: string | null;
  basis: string | null; basis_text: string | null; returns: Record<string, ReturnCell>; risk: RiskCell; quality: FundQuality | null; peers: FundPeers | null; snapshot_as_of: string | null;
}
export interface FundList { n_matching: number; offset: number; rows: FundRow[]; methodology: string; disclaimer: string }
export interface FundFacets { asset_classes: { name: string; n: number }[]; fund_groups: { name: string; n: number }[]; amcs: { name: string; n: number }[]; categories: { asset_class: string; name: string; n: number }[] }
export interface FundDetail {
  fund: FundRow; window: string; methodology: string; disclaimer: string;
  series: { dates: string[]; value: number[]; drawdown: number[] } | null;
  benchmark: { label: string; dates: string[]; value: number[] } | null;
  events: { unit_changes: { date: string; move: number }[]; gaps_over_30d: { from: string; to: string; days: number }[] };
}

export interface HoldingRow {
  symbol: string; quantity: number; avg_cost: number; last_price: number | null; price_date: string | null; value: number | null; weight: number | null;
  pnl: number | null; pnl_pct: number | null; stale: boolean; defensive_bucket: number | null;
}
export interface PortfolioHistory {
  start: string; end: string; sessions: number; names_priced: number; names_missing: string[]; total_return: number; ann_volatility: number; max_drawdown: number;
  current_drawdown: number; var95_1d: number; worst_day: number; series: { date: string[]; value: number[]; drawdown: number[] };
  benchmark?: { total_return: number; ann_volatility: number; max_drawdown: number; beta: number | null; correlation: number | null; series: { date: string[]; value: number[] } };
}
export interface PortfolioSummary {
  import_id: number; pot: Pot | null; imported_at: string; as_of: string | null; holdings: HoldingRow[]; benchmark_label: string | null; notes: string[]; disclaimer: string; history: PortfolioHistory | null;
  totals: { value: number; cost: number; pnl: number; pnl_pct: number | null; n_holdings: number; n_unpriced: number; n_stale: number; winners: number; losers: number };
  concentration: { n_positions: number; top1: number | null; top3: number | null; top5: number | null; top10: number | null; hhi: number; effective_n: number | null; limit: number; above_limit: { symbol: string; weight: number }[] };
  defensive: { available: boolean; scored_share: number; buckets: Record<string, { share: number; names: string[] }> };
}
export interface ImportIssue { row: number; symbol: string; severity: string; reason: string }
export type Pot = "investment" | "trading";
export type Mode = "investor" | "trader";
export interface ImportResult { import_id: number; pot: Pot; n_rows: number; n_accepted: number; n_rejected: number; sha256: string; issues: ImportIssue[]; replaced_previous: boolean }

export interface LiveModel { model_version: string; state: "research" | "paper" | "approved"; description: string | null; predictions: number; resolved: number; pending: number; first_as_of: string | null; last_as_of: string | null; horizons: string[] }
export interface LiveModels { models: LiveModel[]; ledger_rules: string[]; disclaimer: string }
export interface LiveHealth {
  n_resolved: number; n_dates: number; notes: string[]; kill_switch: string[]; retrain: { trigger: boolean; kinds: string[]; reasons: string[]; suppressed: string | null; action: string } | null;
  ranking?: { ic_mean: number; ic_hit: number; ic_rolling: number; precision_at_q: number; base_rate: number; lift: number; n_dates: number };
  economics?: { n_dates: number; net_mean_top?: number; net_mean_universe?: number; excess_mean?: number; hit_rate?: number; profit_factor?: number; max_drawdown_cum?: number };
  calibration?: { n: number; brier: number; brier_base: number; log_loss: number; slope: number; intercept: number };
  reliability?: { bin: number; mean_p: number; observed: number; n: number }[];
  ic_series?: { dates: string[]; values: (number | null)[]; rolling60: (number | null)[] } | null;
  score_drift?: { psi: number | null; reference: string; current: string; flag: boolean };
}
export interface LiveSummary {
  model_version: string; horizon_id: string; state: string; notes: string[]; disclaimer: string; health: LiveHealth;
  counts: { logged: number; resolved: number; pending: number; excluded: number; dates: number; first_as_of: string | null; last_as_of: string | null };
  thresholds: { min_resolved: number; min_dates: number; kill_slope_low: number; kill_drawdown_cum: number; slope_band: number[]; psi_feature: number };
}
export interface LiveOutcome {
  status: "WIN" | "LOSS" | "TIME"; entry_date: string | null; entry_price: number | null; exit_date: string; exit_price: number | null; stop_price: number | null; target_price: number | null;
  gap_exit: boolean | null; ret_gross: number | null; ret_net: number | null; cost_bps: number | null; excluded: boolean; data_through: string; expected_return: number | null; realized_minus_expected: number | null;
}
export interface LivePrediction {
  prediction_id: string; symbol: string; model_version: string; horizon_id: string; side: number; as_of_date: string; prediction_time: string; state: "pending" | "resolved" | "excluded";
  score: number | null; rank_pct: number | null; p_win: number | null; expected_return: number | null; ci_low: number | null; ci_high: number | null; position_size: number | null; regime: string | null;
  ref_price: number | null; sigma: number | null; entry_rule: string; exit_rule: string; cost_model: string; model_state: string;
  window: { entry_date: string; exit_by: string; sessions: number; estimate: boolean };
  barriers: { stop_sigmas: number; target_sigmas: number; stop_distance: number | null; target_distance: number | null };
  lineage: Record<string, string | null>; outcome: LiveOutcome | null;
}
export interface LivePredictionList { n_matching: number; offset: number; rows: LivePrediction[]; disclaimer: string }

export interface AlertRow {
  id: number; scope: "system" | "you"; rule_id: string; severity: "info" | "warning" | "critical"; state: "open" | "acknowledged" | "resolved"; title: string; detail: string;
  entity_type: string | null; entity_id: string | null; first_seen: string; last_seen: string; occurrences: number; acked_by: string | null; acked_at: string | null;
  resolved_at: string | null; resolved_why: string | null; payload: Record<string, unknown>;
}
export interface AlertList { n_matching: number; offset: number; rows: AlertRow[]; last_run: string | null }
export interface AlertSummary { open: { critical: number; warning: number; info: number }; acknowledged: number; last_run: string | null; stale: boolean }

export interface Profile {
  user_id: string; mode: Mode | null; lens_pot: Pot | null; risk_profile: string | null; income_stability: string | null; dependents: number | null; drawdown_reaction: string | null; investment_capital: number | null; trading_capital: number | null; emergency_fund: number | null;
  trading_share_of_investable: number | null; effective_limits: Record<string, number>; limit_defaults: Record<string, number>; limit_ceilings: Record<string, number>;
  loosened_limits: string[]; engine_approved: boolean; warnings: string[]; policy_version: string; disclaimer: string;
  rules: { trader: { losing_streak: { stops: number; half_size_next_trades: number; multiplier: number }; time_stop_sessions: number[]; averaging_down: boolean }; pots: { trading_max_share_of_investable: number }; guardrails: string[] };
}
export interface ProfilePut {
  income_stability?: "stable" | "variable" | "uncertain"; dependents?: number; drawdown_reaction?: "add" | "hold" | "sell_some" | "sell_all"; risk_profile?: string;
  mode?: Mode; investment_capital?: number | null; trading_capital?: number | null; emergency_fund?: number | null; limits?: Record<string, number>;
  approval_note?: string; trading_override_note?: string;
}
export interface ModeAuditRow { ts: string; action: string; actor_role: string | null; payload: Record<string, unknown> }

export type Mix = { equity: number; gold: number; debt: number };
export interface Goal { id: number; name: string; kind: "goal" | "emergency"; target_amount: number; target_date: string; current_amount: number; monthly_sip: number; priority: number; is_test: boolean }
export interface GoalIn { name: string; kind: "goal" | "emergency"; target_amount: number; target_date: string; current_amount: number; monthly_sip: number; priority?: number; is_test?: boolean }
export interface Balance { id: number; asset_class: "equity" | "gold" | "debt"; label: string; amount: number; as_of: string }
export interface Stress { cagr: number; vol: number; max_drawdown: number; peak: string; trough: string; recovery_days: number | null; worst_12m: number | null; start: string; end: string; years: number }
export interface Projection {
  run_id: string; n_blocks_in_history: number; label: string; contributed: number; prob_below_contributed: number;
  inputs: { months: number; paths: number; block_months: number; seed: number; inflation: number; history_start: string; history_end: string; history_months: number; contribution_timing: string; return_basis: string };
  percentiles: Record<string, { nominal: number; real: number }>; prob_reach_target: { nominal: number; real: number };
  fan: { months: number[]; [q: string]: number[] };
}
export interface Tradeoffs { want_probability: number; current_probability: number; on_track: boolean; monthly_contribution_needed?: number; extra_months_needed?: number | null; target_reachable_at_probability?: number; note?: string }
export interface GoalPlan {
  id: number; name: string; kind: "goal" | "emergency"; target_amount: number; target_date: string; current_amount: number; monthly_sip: number; years_left: number; is_test: boolean;
  status: "on_track" | "off_track" | "past_due"; headline: string; profile?: string; profile_reasons?: string[]; mix?: Mix; glide?: boolean; stress?: Stress; projection?: Projection;
  gate?: { binding: boolean; met: boolean; tolerated_fall: number; final_max_drawdown: number; steps: { equity: number; max_drawdown: number }[] } | null;
  sensitivity?: { block_months: number; prob_reach_target_nominal: number; median_nominal: number }[]; haircut?: { points_per_year: number; prob_reach_target_nominal: number; median_nominal: number }[]; tradeoffs?: Tradeoffs;
}
export interface BandRow { class: keyof Mix; current: number; target: number; drift_points: number; breach: boolean; rule: string | null }
export interface Rebalance {
  action: "no_action" | "direct_sip" | "rebalance" | "data_incomplete" | "no_data"; headline: string; bands: BandRow[]; sip_split: Partial<Mix>; trades: { class: keyof Mix; side: "buy" | "sell"; amount: number }[];
  next_review: string; tax_note: string; band_rule: string; months_to_fix?: number; unclassified_share?: number;
}
export interface InvestorPlan {
  status: "needs_input" | "needs_goal" | "ok"; headline: string; as_of: string; label: string; needs?: string[]; notes: string[]; policy_version: string; emergency_fund: number | null;
  history: { start: string; end: string; months: number; proxies: Record<string, { series: string; basis: string }>; notes: string[] };
  current: { values: Mix; parts: Record<keyof Mix, { label: string; value: number; source: string }[]>; unclassified: { symbol: string; value: number; reason: string }[]; total_classified: number };
  goals: GoalPlan[]; target?: Mix | null; rebalance?: Rebalance; stress?: Stress; holdings_priced_at?: string | null; balances?: Balance[];
}
