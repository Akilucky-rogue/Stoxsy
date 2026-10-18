import { useState, type FormEvent } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Chip, ErrorState, PageHead, Skeleton, type Tone } from "../components/ui";
import { patch, post } from "../lib/api";
import { shortDate } from "../lib/format";
import { useAudit, useUsers } from "../lib/queries";

export function Admin() {
  const users = useUsers();
  const audit = useAudit();
  const qc = useQueryClient();
  const [form, setForm] = useState({ email: "", name: "", role: "viewer", temp_password: "" });
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const create = useMutation({
    mutationFn: () => post("/api/admin/users", form),
    onSuccess: () => { setMsg({ ok: true, text: `Created ${form.email}. They must choose a new password at first sign-in.` }); setForm({ email: "", name: "", role: "viewer", temp_password: "" }); void qc.invalidateQueries({ queryKey: ["admin-users"] }); void qc.invalidateQueries({ queryKey: ["admin-audit"] }); },
    onError: (e: Error) => setMsg({ ok: false, text: e.message }),
  });
  const setStatus = useMutation({
    mutationFn: (v: { id: string; status: string }) => patch(`/api/admin/users/${v.id}`, { status: v.status }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ["admin-users"] }); void qc.invalidateQueries({ queryKey: ["admin-audit"] }); },
    onError: (e: Error) => setMsg({ ok: false, text: e.message }),
  });
  const submit = (e: FormEvent) => { e.preventDefault(); setMsg(null); create.mutate(); };

  return (
    <>
      <PageHead title="Users & audit" sub="Accounts, roles and a record of sign-ins and administrative actions." />
      <div className="grid two">
        <div className="card">
          <header><h2>Add a user</h2></header>
          <form onSubmit={submit} noValidate>
            <div className="field"><label htmlFor="n-email">Email</label><input id="n-email" className="input" type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></div>
            <div className="field"><label htmlFor="n-name">Name</label><input id="n-name" className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></div>
            <div className="field"><label htmlFor="n-role">Role</label>
              <select id="n-role" className="input" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })}>
                <option value="viewer">Viewer (read-only)</option><option value="trader">Trader (own portfolios and alerts)</option><option value="admin">Admin</option>
              </select></div>
            <div className="field"><label htmlFor="n-pw">Temporary password (12+ characters)</label><input id="n-pw" className="input" type="password" autoComplete="new-password" value={form.temp_password} onChange={(e) => setForm({ ...form, temp_password: e.target.value })} /></div>
            {msg && <div role={msg.ok ? "status" : "alert"} className={msg.ok ? "notice" : "error"} style={{ marginBottom: 12 }}>{msg.text}</div>}
            <button className="btn primary" disabled={create.isPending || !form.email || form.temp_password.length < 12}>{create.isPending ? "Creating…" : "Create user"}</button>
          </form>
        </div>
        <div className="card">
          <header><h2>Accounts</h2></header>
          {users.isLoading && <Skeleton rows={4} />}
          {users.isError && <ErrorState error={users.error} onRetry={() => users.refetch()} />}
          {users.data && (
            <div className="table-wrap"><table>
              <thead><tr><th>Email</th><th>Role</th><th>Status</th><th>Last sign-in</th><th /></tr></thead>
              <tbody>{users.data.map((u) => (
                <tr key={u.id}><td>{u.email}</td><td><Chip>{u.role}</Chip></td><td><Chip tone={(u.status === "active" ? "ok" : "bad") as Tone}>{u.status}</Chip></td><td>{shortDate(u.last_login_at)}</td>
                  <td><button className="btn" style={{ padding: "4px 10px" }} onClick={() => setStatus.mutate({ id: u.id, status: u.status === "active" ? "disabled" : "active" })}>{u.status === "active" ? "Disable" : "Enable"}</button></td></tr>
              ))}</tbody>
            </table></div>
          )}
        </div>
      </div>
      <div className="card">
        <header><h2>Recent activity</h2></header>
        {audit.isLoading && <Skeleton rows={5} />}
        {audit.isError && <ErrorState error={audit.error} onRetry={() => audit.refetch()} />}
        {audit.data && (
          <div className="table-wrap" style={{ maxHeight: 420 }}><table>
            <thead><tr><th>Time</th><th>Action</th><th>Account</th><th>Result</th><th>IP</th></tr></thead>
            <tbody>{audit.data.map((a) => (
              <tr key={a.id}><td className="mono">{new Date(a.ts).toLocaleString("en-IN")}</td><td>{a.action}</td><td>{a.email ?? "—"}</td><td><Chip tone={a.ok ? "ok" : "bad"}>{a.ok ? "ok" : "failed"}</Chip></td><td className="mono">{a.ip ?? "—"}</td></tr>
            ))}</tbody>
          </table></div>
        )}
      </div>
    </>
  );
}
