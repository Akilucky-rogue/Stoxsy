import { useQuery } from "@tanstack/react-query";
import { api, type AdminUser, type AuditRow, type DefensiveLatest, type ReportBody, type ReportItem, type SystemStatus } from "./api";

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
