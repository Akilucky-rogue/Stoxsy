import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, post, ApiError, type ImportResult, type PortfolioSummary, type AdminUser, type DecisionBook, type FundList, type FundFacets, type FundDetail, type AuditRow, type DefensiveLatest, type ReportBody, type ReportItem, type SystemStatus } from "./api";

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

export const usePortfolio = (benchmark: string, lookbackDays: number) =>
  useQuery({
    queryKey: ["portfolio", benchmark, lookbackDays],
    queryFn: () => api<PortfolioSummary | null>(`/api/portfolio/summary?benchmark=${benchmark}&lookback_days=${lookbackDays}`).catch((e) => { if (e instanceof ApiError && e.status === 404) return null; throw e; }),
    staleTime: stale, retry: false,
  });

export const useImportHoldings = () => {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (b: { csv_text: string; filename?: string }) => post<ImportResult>("/api/portfolio/import", b),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["portfolio"] }); },
  });
};
