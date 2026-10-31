import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Stoxsy -- Quantitative Stock Decision Engine",
  description: "Momentum, F&O-income and pairs signal engines for liquid Indian equities (NSE)",
};

const navItems = [
  { href: "/",          label: "Dashboard", icon: "◉" },
  // /analyze + /live both merged into /screener — pick the "Custom" tab
  // there for any single-symbol deep dive (live chart + 3-horizon signals
  // with precise entry/exit timing + Pin to track). The old routes are
  // kept as redirects for bookmarks.
  { href: "/screener",  label: "Screener",  icon: "⊞" },
  { href: "/research",  label: "Research",  icon: "◆" },
  { href: "/signals",   label: "Signals",   icon: "⚡" },
  { href: "/watchlist", label: "Watchlist", icon: "★" },
  { href: "/factors",   label: "Factors",   icon: "◈" },
  { href: "/backtest",  label: "Backtest",  icon: "▶" },
  // /paper renders the live-validation journal: open paper trades + the
  // realized model-vs-baselines scorecard from /api/paper/{track-record,drift}.
  // This is where you go to see whether the system has earned the right to
  // step up the risk cap.
  { href: "/paper",     label: "Paper",     icon: "◐" },
  // /scorecard renders the Reality Engine: each engine's MEASURED net edge from
  // the mechanically-resolved idea-log (rule_momentum, fno_income, …) via
  // /api/scorecard. Accuracy is reported here, never promised.
  { href: "/scorecard", label: "Scorecard", icon: "▣" },
];

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>
        <div className="app-layout">
          <aside className="sidebar">
            <div className="sidebar-logo">
              Stoxsy
              <span>Quantitative Stock Decision Engine</span>
            </div>
            <nav className="sidebar-nav">
              {navItems.map((item) => (
                <Link key={item.href} href={item.href} className="nav-link">
                  <span className="icon">{item.icon}</span>
                  {item.label}
                </Link>
              ))}
            </nav>
            <div style={{ marginTop: "auto", paddingTop: "16px", borderTop: "1px solid var(--border-color)" }}>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.7rem", color: "var(--text-muted)" }}>
                Signal-only · human-confirmed orders
              </div>
              <div style={{ fontFamily: "var(--font-mono)", fontSize: "0.65rem", color: "var(--text-muted)", marginTop: "4px" }}>
                NSE liquid · momentum · F&amp;O · pairs
              </div>
            </div>
          </aside>
          <main className="main-content">
            {children}
          </main>
        </div>
      </body>
    </html>
  );
}
