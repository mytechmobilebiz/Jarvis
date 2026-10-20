import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { api, ApiError, type SystemStatus, type User } from "./api";
import { useNow, usePoll } from "./hooks";
import { Icon, InfinityMark } from "./icons";
import Activity from "./pages/Activity";
import Agents from "./pages/Agents";
import AutomationLogs from "./pages/AutomationLogs";
import Approvals from "./pages/Approvals";
import Brain from "./pages/Brain";
import CreditRepair from "./pages/CreditRepair";
import Dashboard from "./pages/Dashboard";
import Drafts from "./pages/Drafts";
import Email from "./pages/Email";
import Integrations from "./pages/Integrations";
import KnowledgeBase from "./pages/KnowledgeBase";
import Leads from "./pages/Leads";
import Login from "./pages/Login";
import Outbox from "./pages/Outbox";
import Research from "./pages/Research";
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
import { APP_TITLE, pageTitle } from "./shell/nav";
import Sidebar, { MenuButton } from "./shell/Sidebar";
import { clockTime, isStale, type StatusMeta } from "./status";

export type { StatusMeta } from "./status";

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
  // How current `status` is: pages must not show an old answer as live (C2-03).
  const statusMeta = useMemo<StatusMeta>(() => ({ error: status.error, lastSuccessAt: status.lastSuccessAt }), [status.error, status.lastSuccessAt]);
  const canOperate = user.role === "admin" || user.role === "operator";
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const header = useRef<HTMLElement>(null);
  const main = useRef<HTMLElement>(null);
  const restoreFocus = useRef(false);
  const { pathname } = useLocation();
  const s = status.data;

  useEffect(() => {
    document.title = pageTitle(pathname);
  }, [pathname]);

  const closeMenu = useCallback(() => {
    restoreFocus.current = true;
    setMenuOpen(false);
  }, []);

  // Phone drawer: while it is open the rest of the page is inert; Escape closes it and the focus goes back to the menu button.
  useEffect(() => {
    for (const el of [header.current, main.current]) if (el) el.inert = menuOpen;
    if (!menuOpen) {
      if (restoreFocus.current) menuButton.current?.focus();
      restoreFocus.current = false;
      return;
    }
    // A dialog opened from the drawer (a Quick Action) takes Escape for itself.
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !document.querySelector('[aria-modal="true"]') && closeMenu();
    // The drawer only exists on a phone-width screen; widening the window closes it.
    const wide = window.matchMedia?.("(min-width: 761px)");
    const onWide = () => wide?.matches && setMenuOpen(false);
    document.addEventListener("keydown", onKey);
    wide?.addEventListener("change", onWide);
    return () => {
      document.removeEventListener("keydown", onKey);
      wide?.removeEventListener("change", onWide);
    };
  }, [menuOpen, closeMenu]);

  return (
    <AuthContext.Provider value={{ user, canOperate, signOut: onSignOut }}>
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <TestCopyBanner status={s} />
      <div className={`layout ${menuOpen ? "menu-open" : ""}${s?.instance_label?.trim() ? " has-test-banner" : ""}`}>
        <header className="topbar" ref={header}>
          <MenuButton ref={menuButton} open={menuOpen} onClick={() => setMenuOpen((o) => !o)} />
          <Link to="/" className="brand" aria-label="Infinite AI Command Center, dashboard">
            <InfinityMark className="brand-mark" />
            <span className="brand-text">
              <span className="brand-name">Infinite AI</span>
              <span className="brand-sub">Command Center</span>
            </span>
          </Link>
          <CommandBar />
          <div className="top-actions">
            {user.role === "admin" ? (
              <Link className="top-btn top-settings" to="/settings" aria-label="Settings" title="Settings">
                <Icon name="settings" size={21} strokeWidth={1.6} />
              </Link>
            ) : null}
            <NotificationsMenu status={s} />
            <AccountMenu />
          </div>
        </header>
        <Sidebar user={user} status={s} statusMeta={statusMeta} open={menuOpen} onNavigate={() => setMenuOpen(false)} onClose={closeMenu} />
        {menuOpen ? <div className="scrim" onClick={closeMenu} aria-hidden /> : null}
        <main className="content" id="main" tabIndex={-1} ref={main}>
          <ConnectionLost meta={statusMeta} />
          <InternetLost status={s} meta={statusMeta} />
          <VersionMismatch status={s} />
          <Routes>
            <Route path="/" element={<Dashboard status={s} statusMeta={statusMeta} />} />
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
            <Route path="/brain" element={<Brain />} />
            <Route path="/research" element={<Research />} />
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

/** A test copy of the Command Center (CC_INSTANCE_LABEL set; the JARVIS Brain test instance) says so on every page, so it is
 * never taken for the live install. Absent from older APIs, empty on a live one. */
export function TestCopyBanner({ status }: { status: SystemStatus | null }) {
  const label = status?.instance_label?.trim();
  if (!label) return null;
  return (
    <div className="test-banner" role="status">
      <strong>TEST COPY</strong> · {label} · not your live Command Center
    </div>
  );
}

/** Shown while the status poll fails or its last success is over 30 s old, so nothing on screen passes for live (C2-03). */
function ConnectionLost({ meta }: { meta: StatusMeta }) {
  const now = useNow(5_000);
  if (!isStale(meta, now)) return null;
  const when = meta.lastSuccessAt ? `last update ${clockTime(meta.lastSuccessAt)}` : "no update received yet";
  return (
    <div className="version-banner">
      <div className="alert alert-bad connection-banner" role="alert">
        <strong>Cannot reach the Command Center</strong> · {when}. What this page shows may be out of date; retrying every 10 seconds.
      </div>
    </div>
  );
}

/** Shown while the monitor finds this PC offline (C2-22): sites are not checked then, so their status is their last real
 * result, not a current one. Left to ConnectionLost while the status itself is out of date. */
export function InternetLost({ status, meta }: { status: SystemStatus | null; meta: StatusMeta }) {
  const now = useNow(5_000);
  const net = status?.connectivity;
  if (!net || net.state !== "offline" || isStale(meta, now)) return null;
  return (
    <div className="version-banner">
      <div className="alert alert-warn" role="status">
        <strong>No internet on this PC</strong>
        {net.since ? ` since ${clockTime(net.since)}` : ""}. Websites are not being checked: their status is the last result from before,
        and the time without checks does not count toward uptime.
      </div>
    </div>
  );
}

/** Shown when the API answering this dashboard reports a different version. The usual cause is a page left open during an update: it
 * keeps running the previous dashboard's code, so the dashboard the server serves now (/version.json) decides which message applies.
 * Up to 0.5.0 an install could also build an image from out-of-date copies of the files that hold the version numbers (Docker kept
 * them from an earlier build; see scripts/prepare-build.ps1). */
function VersionMismatch({ status }: { status: SystemStatus | null }) {
  const apiVersion = status?.version ?? null;
  const differs = apiVersion !== null && apiVersion !== __APP_VERSION__;
  const [served, setServed] = useState<string | null>(null);
  useEffect(() => {
    if (!differs) return;
    let current = true;
    fetch("/version.json", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((body: { version?: unknown } | null) => {
        if (current) setServed(typeof body?.version === "string" ? body.version : null);
      })
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [differs, apiVersion]);
  if (!differs) return null;
  if (served && served !== __APP_VERSION__) {
    return (
      <div className="version-banner">
        <div className="alert alert-warn" role="status">
          <strong>This page is out of date.</strong> The Command Center now runs dashboard version {served}, but this page is still version{" "}
          {__APP_VERSION__} from before the update. Reload the page.{" "}
          <button type="button" className="btn small" onClick={() => window.location.reload()}>
            Reload
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="version-banner">
      <div className="alert alert-warn" role="status">
        <strong>Version mismatch.</strong> This dashboard reports version {__APP_VERSION__}, but the API it talks to reports version {apiVersion}. If the
        Command Center was updated while this page was open, reload the page first. If the warning stays, at least one of them was not built from the
        files of the version you installed: run <code>scripts\install.ps1</code> again from that version's folder, then <code>scripts\smoke.ps1</code>; see{" "}
        <Link to="/kb#troubleshooting">Knowledge Base › Troubleshooting</Link>.
      </div>
    </div>
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

  useEffect(() => {
    if (checked && !user) document.title = `Sign in · ${APP_TITLE}`;
  }, [checked, user]);

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
