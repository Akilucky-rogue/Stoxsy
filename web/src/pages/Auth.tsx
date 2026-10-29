import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { Logo } from "../components/ui";
import { useAuth } from "../lib/auth";

function AuthFrame({ title, sub, children }: { title: string; sub: string; children: React.ReactNode }) {
  return (
    <div className="auth-wrap">
      <div className="card auth-card">
        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 14 }}><Logo /><strong style={{ fontSize: "1.2rem" }}>Stoxsy</strong></div>
        <h1>{title}</h1>
        <p className="muted">{sub}</p>
        {children}
      </div>
    </div>
  );
}

export function Login() {
  const { status, login, mustChange } = useAuth();
  const loc = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (status === "authed" || status === "open") return <Navigate to={mustChange ? "/change-password" : (loc.state as { from?: string } | null)?.from ?? "/"} replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    setBusy(true);
    try { await login(email, password); } catch (x) { setErr(x instanceof Error ? x.message : "Sign-in failed"); } finally { setBusy(false); }
  }
  return (
    <AuthFrame title="Sign in" sub="Research and decision support for Indian equities.">
      <form onSubmit={submit} noValidate>
        <div className="field"><label htmlFor="email">Email</label><input id="email" className="input" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required /></div>
        <div className="field"><label htmlFor="password">Password</label><input id="password" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required /></div>
        {err && <div role="alert" className="error" style={{ marginBottom: 12 }}>{err}</div>}
        <button className="btn primary" style={{ width: "100%" }} disabled={busy || !email || !password}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </AuthFrame>
  );
}

export function ChangePassword() {
  const { status, changePassword, mustChange } = useAuth();
  const [oldP, setOld] = useState("");
  const [newP, setNew] = useState("");
  const [again, setAgain] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  if (status === "anon") return <Navigate to="/login" replace />;
  if (status === "open") return <Navigate to="/" replace />;
  if (done) return <Navigate to="/" replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    if (newP !== again) { setErr("The new passwords do not match."); return; }
    setBusy(true);
    try { await changePassword(oldP, newP); setDone(true); } catch (x) { setErr(x instanceof Error ? x.message : "Could not change the password"); } finally { setBusy(false); }
  }
  return (
    <AuthFrame title={mustChange ? "Choose a new password" : "Change password"} sub={mustChange ? "You are using a temporary password. Choose your own to continue." : "Changing your password signs out every other session."}>
      <form onSubmit={submit} noValidate>
        <div className="field"><label htmlFor="old">Current password</label><input id="old" className="input" type="password" autoComplete="current-password" value={oldP} onChange={(e) => setOld(e.target.value)} /></div>
        <div className="field"><label htmlFor="new">New password (12+ characters)</label><input id="new" className="input" type="password" autoComplete="new-password" value={newP} onChange={(e) => setNew(e.target.value)} /></div>
        <div className="field"><label htmlFor="again">Repeat new password</label><input id="again" className="input" type="password" autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} /></div>
        {err && <div role="alert" className="error" style={{ marginBottom: 12 }}>{err}</div>}
        <button className="btn primary" style={{ width: "100%" }} disabled={busy || !oldP || newP.length < 12}>{busy ? "Saving…" : "Save password"}</button>
      </form>
    </AuthFrame>
  );
}
