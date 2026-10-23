import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { REGIME_LABEL, regimeTone, shortDate } from "../lib/format";
import { useAlertSummary, useDefensive, useProfile, useSystem, useUpdateProfile } from "../lib/queries";
import { applyPref, nextPref, readPref, type ThemePref } from "../lib/theme";
import type { Mode } from "../lib/api";
import { Chip, Icon, Logo, Segmented, type Tone } from "./ui";

function useCollapsed() {
  const [c, setC] = useState(() => {
    try { return localStorage.getItem("stoxsy.sidebar") === "1"; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem("stoxsy.sidebar", c ? "1" : "0"); } catch { /* ignore */ }
  }, [c]);
  return [c, setC] as const;
}

function ThemeButton() {
  const [pref, setPref] = useState<ThemePref>(readPref);
  const icon = pref === "light" ? "sun" : pref === "dark" ? "moon" : "system";
  return (
    <button className="icon-btn" aria-label={`Theme: ${pref}. Click to change`} title={`Theme: ${pref}`} onClick={() => { const n = nextPref(pref); applyPref(n); setPref(n); }}>
      <Icon name={icon} />
    </button>
  );
}

function AlertBell() {
  const a = useAlertSummary();
  const o = a.data?.open;
  const n = o ? o.critical + o.warning : 0;
  const label = o ? `Alerts: ${o.critical} critical, ${o.warning} warning, ${o.info} info open` : "Alerts";
  return (
    <Link to="/alerts" className="icon-btn" aria-label={label} title={label} style={{ position: "relative" }}>
      <Icon name="bell" />
      {n > 0 && <span aria-hidden="true" style={{ position: "absolute", top: 2, right: 2, minWidth: 16, height: 16, padding: "0 4px", borderRadius: 8, fontSize: 10, lineHeight: "16px", textAlign: "center", fontWeight: 700,
        color: "#fff", background: o && o.critical > 0 ? "var(--down)" : "var(--warn)" }}>{n}</span>}
    </Link>
  );
}

const MODE_OPTIONS: { value: Mode; label: string }[] = [{ value: "investor", label: "Investor" }, { value: "trader", label: "Trader" }];

/** Mode is a lens and a stored preference. It never moves money between pots (the server enforces that). */
function ModeSwitch({ canWrite }: { canWrite: boolean }) {
  const p = useProfile();
  const upd = useUpdateProfile();
  if (!p.data) return null;
  return (
    <div title={canWrite ? "Switch the lens. Your investment and trading pots stay separate." : "Viewers cannot change the mode."} style={{ display: "inline-flex", alignItems: "center", gap: 6, opacity: canWrite ? 1 : 0.6 }}>
      <Segmented label="Mode" value={p.data.mode ?? ("" as Mode)} options={MODE_OPTIONS} onChange={(m) => { if (canWrite && m !== p.data?.mode) upd.mutate({ mode: m }); }} />
      {p.data.mode === null && <span className="muted" style={{ fontSize: "0.8rem" }}>choose a mode</span>}
      {upd.isError && <span role="alert" className="muted" style={{ color: "var(--down)", fontSize: "0.8rem" }}>mode not saved</span>}
    </div>
  );
}

function StatusStrip() {
  const def = useDefensive({ limit: 1 });
  const sys = useSystem();
  const regime = def.data?.regime ?? null;
  return (
    <div className="strip" aria-label="System status">
      <Chip tone={regimeTone(regime) as Tone} title="Universe regime from trend and volatility (known at the close)">
        Regime: {regime ? REGIME_LABEL[regime] ?? regime : "…"}
      </Chip>
      <Chip title="Latest session in the price archive">Data: {sys.data ? shortDate(sys.data.ohlcv_latest) : "…"}</Chip>
      <Chip tone={sys.data?.promotion_enabled ? "bad" : "ok"} title="Model promotion is an explicit owner decision">
        <Icon name="lock" size={12} /> Promotion {sys.data?.promotion_enabled ? "ENABLED" : "off"}
      </Chip>
    </div>
  );
}

export function Shell() {
  const { me, status, logout } = useAuth();
  const [collapsed, setCollapsed] = useCollapsed();
  const [drawer, setDrawer] = useState(false);
  const loc = useLocation();
  useEffect(() => setDrawer(false), [loc.pathname]);
  const admin = me?.role === "admin";
  const canWrite = status === "open" || me?.role === "admin" || me?.role === "trader";
  const profile = useProfile();

  return (
    <div className={`app${collapsed ? " collapsed" : ""}${drawer ? " drawer-open" : ""}`}>
      <aside className="sidebar" aria-label="Primary">
        <div className="brand"><Logo /><span className="txt">Stoxsy</span></div>
        <nav className="nav">
          <div className="group">{profile.data?.mode ? `${profile.data.mode === "trader" ? "Trader" : "Investor"} mode` : "Overview"}</div>
          <NavLink to="/plan"><Icon name="target" /><span className="txt">Investor plan</span></NavLink>
          <NavLink to="/profile"><Icon name="users" /><span className="txt">Profile &amp; capital</span></NavLink>
          <NavLink to="/" end><Icon name="home" /><span className="txt">Dashboard</span></NavLink>
          <NavLink to="/portfolio"><Icon name="wallet" /><span className="txt">My portfolio</span></NavLink>
          <NavLink to="/decision"><Icon name="target" /><span className="txt">Decision cards</span></NavLink>
          <NavLink to="/alerts"><Icon name="bell" /><span className="txt">Alerts</span></NavLink>
          <NavLink to="/live"><Icon name="pulse" /><span className="txt">Live compare</span></NavLink>
          <NavLink to="/defensive"><Icon name="shield" /><span className="txt">Defensive signal</span></NavLink>
          <div className="group">Funds</div>
          <NavLink to="/funds"><Icon name="layers" /><span className="txt">Mutual funds &amp; ETFs</span></NavLink>
          <div className="group">Research</div>
          <NavLink to="/research"><Icon name="flask" /><span className="txt">Reports &amp; models</span></NavLink>
          <div className="group">Platform</div>
          <NavLink to="/system"><Icon name="pulse" /><span className="txt">Data &amp; system</span></NavLink>
          {admin && <NavLink to="/admin"><Icon name="users" /><span className="txt">Users &amp; audit</span></NavLink>}
        </nav>
        <div className="side-foot">
          <div className="txt">{status === "open" ? "Local mode (no sign-in)" : me?.email}</div>
          <div className="txt">Research &amp; decision support. Not investment advice.</div>
          <button className="icon-btn hide-sm" style={{ marginTop: 10 }} onClick={() => setCollapsed(!collapsed)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-pressed={collapsed}>
            <span style={{ display: "inline-flex", transform: collapsed ? "rotate(180deg)" : "none" }}><Icon name="chevron" /></span>
          </button>
        </div>
      </aside>
      <div className="scrim" onClick={() => setDrawer(false)} />
      <div className="main">
        <header className="topbar">
          <button className="icon-btn menu-btn" aria-label="Open menu" onClick={() => setDrawer(true)}><Icon name="menu" /></button>
          <ModeSwitch canWrite={canWrite} />
          <StatusStrip />
          <div className="spacer" />
          <AlertBell />
          <ThemeButton />
          {status === "authed" && <button className="icon-btn" aria-label="Sign out" title="Sign out" onClick={() => void logout()}><Icon name="out" /></button>}
        </header>
        <main className="content" id="main">
          <Outlet />
          <p className="footer-note">Stoxsy is a research and decision-support tool. Nothing here is investment advice, a recommendation or a prediction of returns; model promotion is disabled until a model passes the offline gate, live-paper evidence and the owner signs off.</p>
        </main>
      </div>
    </div>
  );
}
