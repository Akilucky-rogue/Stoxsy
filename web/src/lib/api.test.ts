import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, getAccessToken, onSessionChange, setAccessToken } from "./api";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("api client", () => {
  beforeEach(() => setAccessToken("old"));
  afterEach(() => vi.restoreAllMocks());

  it("sends the bearer token and returns JSON", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(200, { ok: 1 }));
    expect(await api<{ ok: number }>("/api/x")).toEqual({ ok: 1 });
    expect(new Headers((f.mock.calls[0][1] as RequestInit).headers).get("Authorization")).toBe("Bearer old");
  });

  it("refreshes once on 401, shares the refresh between concurrent calls, and retries with the new token", async () => {
    let refreshes = 0;
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url, init) => {
      const u = String(url);
      if (u === "/api/auth/refresh") { refreshes++; return json(200, { access_token: "new" }); }
      const auth = new Headers((init as RequestInit).headers).get("Authorization");
      return auth === "Bearer new" ? json(200, { path: u }) : json(401, { detail: "expired" });
    });
    const [a, b, c] = await Promise.all([api<{ path: string }>("/api/a"), api<{ path: string }>("/api/b"), api<{ path: string }>("/api/c")]);
    expect([a.path, b.path, c.path]).toEqual(["/api/a", "/api/b", "/api/c"]);
    expect(refreshes).toBe(1);
    expect(getAccessToken()).toBe("new");
  });

  it("signs out (notifies listeners) when the refresh fails, and raises the 401", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => String(url) === "/api/auth/refresh" ? json(401, { detail: "no" }) : json(401, { detail: "expired" }));
    const seen: boolean[] = [];
    const off = onSessionChange((s) => seen.push(s));
    await expect(api("/api/a")).rejects.toMatchObject({ status: 401 });
    off();
    expect(seen).toEqual([false]);
    expect(getAccessToken()).toBeNull();
  });

  it("never tries to refresh for the auth endpoints themselves and surfaces the server's message", async () => {
    const f = vi.spyOn(globalThis, "fetch").mockResolvedValue(json(401, { detail: "invalid email or password" }));
    await expect(api("/api/auth/login", { method: "POST", body: "{}" })).rejects.toThrow("invalid email or password");
    expect(f).toHaveBeenCalledTimes(1);
  });

  it("turns validation arrays into one readable message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(json(422, { detail: [{ msg: "field required" }, { msg: "bad role" }] }));
    const e = (await api("/api/y").catch((x: unknown) => x)) as ApiError;
    expect(e).toBeInstanceOf(ApiError);
    expect(e.message).toBe("field required; bad role");
  });
});
