import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes } from "react-router-dom";
import { api, ApiError, type SystemStatus, type User } from "./api";
import { usePoll } from "./hooks";
import { Icon } from "./icons";
import Activity from "./pages/Activity";
import Agents from "./pages/Agents";
import AutomationLogs from "./pages/AutomationLogs";
import Approvals from "./pages/Approvals";
import CreditRepair from "./pages/CreditRepair";
import Dashboard from "./pages/Dashboard";
import Drafts from "./pages/Drafts";
import Email from "./pages/Email";
import Integrations from "./pages/Integrations";
import KnowledgeBase from "./pages/KnowledgeBase";
import Leads from "./pages/Leads";
import Login from "./pages/Login";
import Outbox from "./pages/Outbox";
import Schedules from "./pages/Schedules";
import SeoMarketing from "./pages/SeoMarketing";
import Servers from "./pages/Servers";
import Settings from "./pages/Settings";
import Social from "./pages/Social";
import Supervisor from "./pages/Supervisor";
import Support from "./pages/Support";
import Tasks from "./pages/Tasks";
import Websites from "./pages/Websites";
import Workflows from "./pages/Workflows";
import CommandBar from "./shell/CommandBar";
import { AccountMenu, NotificationsMenu } from "./shell/Menus";
import Sidebar, { MenuButton } from "./shell/Sidebar";

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

function Shell({ user, onSignOut }: { user: User; onSignOut: () => Promise<void> }) {
  const status = usePoll(() => api.get<SystemStatus>("/api/system/status"), 10_000);
  const canOperate = user.role === "admin" || user.role === "operator";
  const [menuOpen, setMenuOpen] = useState(false);
  const s = status.data;
  return (
    <AuthContext.Provider value={{ user, canOperate, signOut: onSignOut }}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <div className={`layout ${menuOpen ? "menu-open" : ""}`}>
        <header className="topbar">
          <MenuButton open={menuOpen} onClick={() => setMenuOpen((o) => !o)} />
          <Link to="/" className="brand" aria-label="Infinite AI Command Center, dashboard">
            <span className="logo" aria-hidden>
              ∞
            </span>
            <span className="brand-text">
              <span className="brand-name">Infinite AI</span>
              <span className="brand-sub">Command Center</span>
            </span>
          </Link>
          <CommandBar />
          <div className="top-actions">
            <NotificationsMenu status={s} />
            {user.role === "admin" ? (
              <Link className="top-btn" to="/settings" aria-label="Settings" title="Settings">
                <Icon name="settings" />
              </Link>
            ) : null}
            <AccountMenu />
          </div>
        </header>
        <Sidebar user={user} status={s} open={menuOpen} onNavigate={() => setMenuOpen(false)} />
        {menuOpen ? <div className="scrim" onClick={() => setMenuOpen(false)} aria-hidden /> : null}
        <main className="content" id="main" tabIndex={-1}>
          <Routes>
            <Route path="/" element={<Dashboard status={s} />} />
            <Route path="/agents" element={<Agents />} />
            <Route path="/workflows" element={<Workflows />} />
            <Route path="/websites" element={<Websites />} />
            <Route path="/seo" element={<SeoMarketing />} />
            <Route path="/servers" element={<Servers />} />
            <Route path="/email" element={<Email />} />
            <Route path="/credit-repair" element={<CreditRepair />} />
            <Route path="/social" element={<Social />} />
            <Route path="/logs" element={<AutomationLogs />} />
            <Route path="/kb" element={<KnowledgeBase />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/approvals" element={<Approvals />} />
            <Route path="/tasks" element={<Tasks />} />
            <Route path="/supervisor" element={<Supervisor />} />
            <Route path="/leads" element={<Leads />} />
            <Route path="/support" element={<Support />} />
            <Route path="/drafts" element={<Drafts />} />
            <Route path="/outbox" element={<Outbox />} />
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
