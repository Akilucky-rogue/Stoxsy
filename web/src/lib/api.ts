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
    if (Array.isArray(j.detail)) return j.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ") || r.statusText;
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
  corporate_actions: { policy: string; series?: string | null; validation?: string | null; events_applied?: number | null; built_at?: string | null };
  data_as_of: { signal_date: string; prices_last_date?: string | null; eod_raw_latest?: string | null; ohlcv_latest?: string | null; generated_at: string };
  notes: string[];
}
export interface DecisionBook { cards: DecisionCard[]; summary: DecisionSummary; transparency?: DecisionTransparency; disclaimer: string }

export interface FundRow {
  scheme_code: number; name: string; amc: string | null; category: string | null; asset_class: string | null; plan: string | null; option_type: string | null;
  is_etf: boolean; is_index: boolean; is_liquid: boolean; is_direct: boolean | null; is_growth: boolean | null; last_nav: number | null; last_nav_date: string | null;
  returns: Record<string, { cumulative: number; annualised: number | null; start: string; end: string }>; return_basis: string;
}
export interface FundList { n_matching: number; rows: FundRow[]; methodology: string; disclaimer: string }

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
  import_id: number; imported_at: string; as_of: string | null; holdings: HoldingRow[]; benchmark_label: string | null; notes: string[]; disclaimer: string; history: PortfolioHistory | null;
  totals: { value: number; cost: number; pnl: number; pnl_pct: number | null; n_holdings: number; n_unpriced: number; n_stale: number; winners: number; losers: number };
  concentration: { n_positions: number; top1: number | null; top3: number | null; top5: number | null; top10: number | null; hhi: number; effective_n: number | null; limit: number; above_limit: { symbol: string; weight: number }[] };
  defensive: { available: boolean; scored_share: number; buckets: Record<string, { share: number; names: string[] }> };
}
export interface ImportIssue { row: number; symbol: string; severity: string; reason: string }
export interface ImportResult { import_id: number; n_rows: number; n_accepted: number; n_rejected: number; sha256: string; issues: ImportIssue[]; replaced_previous: boolean }
