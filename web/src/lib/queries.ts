import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, put, ApiError, type ModeAuditRow, type Pot, type Profile, type ProfilePut, type ImportResult, type PortfolioSummary, type AdminUser, type DecisionBook, type AlertList, type AlertSummary, type LiveModels, type LiveSummary, type LivePredictionList, type FundList, type FundFacets, type FundDetail, type AuditRow, type DefensiveLatest, type ReportBody, type ReportItem, type SystemStatus } from "./api";

const stale = 60_000;

export const useDefensive = (p: { bucket?: number; q?: string; sort?: string; limit?: number } = {}) => {
  const qs = new URLSearchParams();
  if (p.bucket) qs.set("bucket", String(p.bucket));
  if (p.q) qs.set("q", p.q);
  if (p.sort) qs.set("sort", p.sort);
  qs.set("limit", String(p.limit ?? 100));
  return useQuery({ queryKey: ["defensive", p], queryFn: () => api<DefensiveLatest>(`/api/defensive/latest?${qs}`), staleTime: stale });
};

export const useSystem = () => useQuery({ queryKey: ["system"], queryFn: () => api<SystemStatus>("/api/system/status"), staleTime: stale });

export const useReports = (group?: string) =>
  useQuery({ queryKey: ["reports", group ?? "all"], queryFn: () => api<ReportItem[]>(`/api/research/reports${group ? `?group=${group}` : ""}`), staleTime: stale });

export const useReport = (group: string, name: string) =>
  useQuery({ queryKey: ["report", group, name], queryFn: () => api<ReportBody>(`/api/research/reports/${group}/${name}`), staleTime: 5 * stale });

export const useUsers = () => useQuery({ queryKey: ["admin-users"], queryFn: () => api<AdminUser[]>("/api/admin/users") });
export const useAudit = () => useQuery({ queryKey: ["admin-audit"], queryFn: () => api<AuditRow[]>("/api/admin/audit?limit=100") });

export interface DecisionParams { capital: number; risk: number; horizon: string; maxPositions: number; minBucket: number }
export const useDecision = (p: DecisionParams) =>
  useQuery({
    queryKey: ["decision", p],
    queryFn: () => api<DecisionBook>(`/api/decision/cards?capital=${p.capital}&risk_per_position=${p.risk}&horizon=${p.horizon}&max_positions=${p.maxPositions}&min_bucket=${p.minBucket}`),
    staleTime: 30_000,
  });

export interface FundParams {
  q: string; assetClass: string; amc: string; fundGroup: string; category: string; plan: string; option: string; status: string; quality: string;
  sort: string; order: string; offset: number;
}
export const FUND_PAGE = 40;
export const useFunds = (p: FundParams) => {
  const qs = new URLSearchParams({ limit: String(FUND_PAGE), offset: String(p.offset), option: p.option, status: p.status, sort: p.sort, order: p.order });
  if (p.q) qs.set("q", p.q);
  if (p.assetClass) qs.set("asset_class", p.assetClass);
  if (p.amc) qs.set("amc", p.amc);
  if (p.fundGroup) qs.set("fund_group", p.fundGroup);
  if (p.category) qs.set("category", p.category);
  if (p.plan) qs.set("plan", p.plan);
  if (p.quality) qs.set("quality", p.quality);
  return useQuery({ queryKey: ["funds", p], queryFn: () => api<FundList>(`/api/funds/schemes?${qs}`), staleTime: 60_000, placeholderData: (prev) => prev });
};
export const useFundFacets = () => useQuery({ queryKey: ["fund-facets"], queryFn: () => api<FundFacets>("/api/funds/facets"), staleTime: 10 * 60_000 });
export const useFundDetail = (code: number | null, window: string) =>
  useQuery({ queryKey: ["fund-detail", code, window], queryFn: () => api<FundDetail>(`/api/funds/schemes/${code}?window=${window}`), enabled: code != null, staleTime: 60_000 });

export const useProfile = () => useQuery({ queryKey: ["profile"], queryFn: () => api<Profile>("/api/me/profile"), staleTime: stale });
export const useModeAudit = () => useQuery({ queryKey: ["mode-audit"], queryFn: () => api<ModeAuditRow[]>("/api/me/audit?limit=30"), staleTime: 30_000 });
export const useUpdateProfile = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: ProfilePut) => put<Profile>("/api/me/profile", b),
    onSuccess: (p) => { qc.setQueryData(["profile"], p); void qc.invalidateQueries({ queryKey: ["mode-audit"] }); void qc.invalidateQueries({ queryKey: ["portfolio"] }); },
  });
};

/** `pot` null = wait until the profile says which pot the current mode looks at. */
export const usePortfolio = (pot: Pot | null, benchmark: string, lookbackDays: number) =>
  useQuery({
    queryKey: ["portfolio", pot, benchmark, lookbackDays],
    enabled: pot != null,
    queryFn: () => api<PortfolioSummary | null>(`/api/portfolio/summary?pot=${pot}&benchmark=${benchmark}&lookback_days=${lookbackDays}`).catch((e) => { if (e instanceof ApiError && e.status === 404) return null; throw e; }),
    staleTime: stale, retry: false,
  });

export const useImportHoldings = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { pot: Pot; csv_text: string; filename?: string }) => post<ImportResult>("/api/portfolio/import", b),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["portfolio"] }); },
  });
};

export const useLiveModels = () => useQuery({ queryKey: ["live-models"], queryFn: () => api<LiveModels>("/api/live/models"), staleTime: stale });
export const useLiveSummary = (model: string | null, horizon: string) =>
  useQuery({ queryKey: ["live-summary", model, horizon], queryFn: () => api<LiveSummary>(`/api/live/summary?model_version=${encodeURIComponent(model ?? "")}&horizon_id=${horizon}`), enabled: !!model, staleTime: stale });
export interface LivePredParams { model: string | null; horizon: string; state: string; selectedOnly: boolean; symbol: string; offset: number }
export const LIVE_PAGE = 25;
export const useLivePredictions = (p: LivePredParams) => {
  const qs = new URLSearchParams({ model_version: p.model ?? "", horizon_id: p.horizon, state: p.state, limit: String(LIVE_PAGE), offset: String(p.offset) });
  if (p.selectedOnly) qs.set("selected_only", "true");
  if (p.symbol) qs.set("symbol", p.symbol);
  return useQuery({ queryKey: ["live-pred", p], queryFn: () => api<LivePredictionList>(`/api/live/predictions?${qs}`), enabled: !!p.model, staleTime: stale, placeholderData: (prev) => prev });
};

export const useAlertSummary = () => useQuery({ queryKey: ["alert-summary"], queryFn: () => api<AlertSummary>("/api/alerts/summary"), staleTime: 60_000, refetchInterval: 5 * 60_000, retry: false });
export const useAlerts = (state: string, severity: string) =>
  useQuery({ queryKey: ["alerts", state, severity], queryFn: () => api<AlertList>(`/api/alerts?state=${state}${severity ? `&severity=${severity}` : ""}&limit=200`), staleTime: 30_000 });
export const useRefreshAlerts = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => post<{ evaluated: string[]; skipped_global: boolean; errors: unknown[] }>("/api/alerts/refresh"),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["alerts"] }); void qc.invalidateQueries({ queryKey: ["alert-summary"] }); },
  });
};
export const useAcknowledgeAlert = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) => post<{ acknowledged: number }>(`/api/alerts/${id}/acknowledge`),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["alerts"] }); void qc.invalidateQueries({ queryKey: ["alert-summary"] }); },
  });
};
