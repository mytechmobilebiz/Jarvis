import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, ApiError, type SystemStatus, type User } from "./api";
import { usePoll } from "./hooks";
import Activity from "./pages/Activity";
import Agents from "./pages/Agents";
import Approvals from "./pages/Approvals";
import Drafts from "./pages/Drafts";
import Integrations from "./pages/Integrations";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Schedules from "./pages/Schedules";
import Supervisor from "./pages/Supervisor";
import Tasks from "./pages/Tasks";
import Websites from "./pages/Websites";

interface AuthState {
  user: User;
  canOperate: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthContext");
  return ctx;
}

const NAV: { to: string; label: string; badge?: (s: SystemStatus) => number }[] = [
  { to: "/", label: "Overview" },
  { to: "/supervisor", label: "Supervisor" },
  { to: "/agents", label: "Agents" },
  { to: "/websites", label: "Websites", badge: (s) => (s.counts.sites.down ?? 0) + (s.counts.sites.degraded ?? 0) },
  { to: "/approvals", label: "Approvals", badge: (s) => s.counts.approvals_pending },
  { to: "/tasks", label: "Tasks" },
  { to: "/drafts", label: "Drafts & reports" },
  { to: "/schedules", label: "Schedules" },
  { to: "/integrations", label: "Integrations" },
  { to: "/activity", label: "Activity log" },
];

function Shell({ user, onSignOut }: { user: User; onSignOut: () => Promise<void> }) {
  const status = usePoll(() => api.get<SystemStatus>("/api/system/status"), 10_000);
  const canOperate = user.role === "admin" || user.role === "operator";
  const s = status.data;
  const workerAlive = s?.workers.some((w) => w.alive);
  return (
    <AuthContext.Provider value={{ user, canOperate, signOut: onSignOut }}>
      <div className="layout">
        <aside className="sidebar">
          <div className="brand">
            <span className="logo" aria-hidden>
              ∞
            </span>
            <div>
              <div className="brand-name">Infinite AI</div>
              <div className="brand-sub">Command Center</div>
            </div>
          </div>
          <nav>
            {NAV.map((item) => {
              const count = s && item.badge ? item.badge(s) : 0;
              return (
                <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => (isActive ? "active" : "")}>
                  <span>{item.label}</span>
                  {count ? <span className="nav-badge">{count}</span> : null}
                </NavLink>
              );
            })}
          </nav>
          <div className="sidebar-foot">
            <div className="health-line">
              <span className={`dot ${s ? (workerAlive ? "dot-good" : "dot-bad") : "dot-muted"}`} /> Worker
            </div>
            <div className="health-line">
              <span className={`dot ${s?.n8n?.last_ok_at && !(s.n8n.last_error_at && s.n8n.last_error_at > s.n8n.last_ok_at) ? "dot-good" : s ? "dot-warn" : "dot-muted"}`} /> n8n
            </div>
            <div className="health-line">
              <span className={`dot ${s?.llm.configured ? "dot-good" : "dot-muted"}`} /> AI: {s ? (s.llm.configured ? s.llm.model : "rule-based") : "…"}
            </div>
            <div className="user-line">
              <span title={user.role}>{user.email}</span>
              <button className="btn ghost small" onClick={() => void onSignOut()}>
                Sign out
              </button>
            </div>
          </div>
        </aside>
        <main className="content">
          <Routes>
            <Route path="/" element={<Overview status={s} />} />
            <Route path="/supervisor" element={<Supervisor />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/websites" element={<Websites />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/drafts" element={<Drafts />} />
            <Route path="/schedules" element={<Schedules />} />
            <Route path="/integrations" element={<Integrations />} />
            <Route path="/activity" element={<Activity />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
      </div>
    </AuthContext.Provider>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);

  const loadMe = useCallback(async () => {
    try {
      const me = await api.get<{ user: User }>("/api/auth/me");
      setUser(me.user);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) console.error(e);
      setUser(null);
    } finally {
      setChecked(true);
    }
  }, []);

  useEffect(() => {
    void loadMe();
    const onUnauthorized = () => setUser(null);
    window.addEventListener("cc:unauthorized", onUnauthorized);
    return () => window.removeEventListener("cc:unauthorized", onUnauthorized);
  }, [loadMe]);

  const signOut = useCallback(async () => {
    await api.post("/api/auth/logout").catch(() => undefined);
    setUser(null);
  }, []);

  if (!checked) return <div className="boot">Loading…</div>;
  return (
    <BrowserRouter>
      {user ? <Shell user={user} onSignOut={signOut} /> : <Login onSignedIn={setUser} />}
    </BrowserRouter>
  );
}
