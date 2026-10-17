// Single source of truth for the backend API base.
//
// Override per-environment with NEXT_PUBLIC_API_BASE (Next inlines any
// NEXT_PUBLIC_* var into the browser bundle at build time). Falls back to the
// local dev backend so `npm run dev` works with zero config.
export const API_BASE: string =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8000/api";

/**
 * Thin GET helper that THROWS on a non-2xx response, so callers can render a
 * real error state instead of silently treating an error-JSON body (or a 500)
 * as data. Use in a try/catch and surface `err.message` to the user.
 */
export async function apiGet<T = any>(path: string): Promise<T> {
  const url = path.startsWith("http")
    ? path
    : `${API_BASE}${path.startsWith("/") ? path : `/${path}`}`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`API ${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}
