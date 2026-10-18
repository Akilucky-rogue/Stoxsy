import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, onSessionChange, post, setAccessToken, type Me, type Session } from "./api";

// status: "loading" until the first check completes; "open" = backend runs in local mode (AUTH_ENFORCED=false) so no sign-in is needed;
// "authed" = signed-in account; "anon" = sign-in required.
export type AuthStatus = "loading" | "open" | "authed" | "anon";

interface AuthValue {
  status: AuthStatus;
  me: Me | null;
  mustChange: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>;
  reload: () => Promise<void>;
}

const Ctx = createContext<AuthValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [me, setMe] = useState<Me | null>(null);

  const load = useCallback(async () => {
    try {
      let m = await api<Me>("/api/auth/me");
      if (m.authenticated) {
        setMe(m);
        setStatus("authed");
        return;
      }
      if (!m.auth_enforced) {
        setMe(m);
        setStatus("open");
        return;
      }
    } catch (e) {
      // api() already tried the refresh cookie once on a 401 (page reload with a live session signs straight back in)
      if (!(e instanceof ApiError)) console.warn("auth check failed", e);
    }
    setMe(null);
    setStatus("anon");
  }, []);

  useEffect(() => {
    void load();
    return onSessionChange((signedIn) => {
      if (!signedIn) {
        setMe(null);
        setStatus("anon");
      }
    });
  }, [load]);

  const adopt = useCallback(async (s: Session) => {
    setAccessToken(s.access_token);
    await load();
  }, [load]);

  const value = useMemo<AuthValue>(() => ({
    status, me, mustChange: !!me?.must_change_password,
    login: async (email, password) => adopt(await post<Session>("/api/auth/login", { email, password })),
    logout: async () => {
      try { await post("/api/auth/logout"); } catch { /* already signed out */ }
      setAccessToken(null);
      setMe(null);
      setStatus("anon");
    },
    changePassword: async (o, n) => adopt(await post<Session>("/api/auth/change-password", { old_password: o, new_password: n })),
    reload: load,
  }), [status, me, adopt, load]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
