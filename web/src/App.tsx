import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Shell } from "./components/Shell";
import { Skeleton } from "./components/ui";
import { useAuth } from "./lib/auth";
import { Admin } from "./pages/Admin";
import { Alerts } from "./pages/Alerts";
import { ChangePassword, Login } from "./pages/Auth";
import { Dashboard } from "./pages/Dashboard";
import { Decision } from "./pages/Decision";
import { Defensive } from "./pages/Defensive";
import { Funds } from "./pages/Funds";
import { LiveCompare } from "./pages/LiveCompare";
import { Portfolio } from "./pages/Portfolio";
import { ReportView, Research } from "./pages/Research";
import { System } from "./pages/System";

function Guard({ admin = false, children }: { admin?: boolean; children: React.ReactNode }) {
  const { status, me, mustChange } = useAuth();
  const loc = useLocation();
  if (status === "loading") return <div className="auth-wrap"><div className="card auth-card"><Skeleton rows={3} /></div></div>;
  if (status === "anon") return <Navigate to="/login" replace state={{ from: loc.pathname }} />;
  if (mustChange) return <Navigate to="/change-password" replace />;
  if (admin && me?.role !== "admin") return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/change-password" element={<ChangePassword />} />
      <Route element={<Guard><Shell /></Guard>}>
        <Route index element={<Dashboard />} />
        <Route path="portfolio" element={<Portfolio />} />
        <Route path="decision" element={<Decision />} />
        <Route path="defensive" element={<Defensive />} />
        <Route path="alerts" element={<Alerts />} />
        <Route path="live" element={<LiveCompare />} />
        <Route path="funds" element={<Funds />} />
        <Route path="research" element={<Research />} />
        <Route path="research/:group/:name" element={<ReportView />} />
        <Route path="system" element={<System />} />
        <Route path="admin" element={<Guard admin><Admin /></Guard>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
