import { forwardRef, useEffect, useId, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import type { SystemStatus, User } from "../api";
import { useNow } from "../hooks";
import { Icon } from "../icons";
import { ago } from "../dashboard/model";
import { aiState, clockTime, engineState, isStale, serviceHealth, statusAge, type StatusMeta } from "../status";
import DeviceCard from "./DeviceCard";
import { MAIN_NAV, MORE_NAV, OPS_NAV, badgeTotal, visible, type NavItem } from "./nav";
import QuickActions from "./QuickActions";

// Kept here as well: pages imported it from the sidebar before it moved to status.ts.
export { serviceHealth };

const MORE_KEY = "cc.nav.more";

function readMore(): boolean {
  try {
    return localStorage.getItem(MORE_KEY) === "open";
  } catch {
    return false;
  }
}

function saveMore(open: boolean) {
  try {
    localStorage.setItem(MORE_KEY, open ? "open" : "closed");
  } catch {
    /* private window: not remembered */
  }
}

function NavEntry({ item, status, onNavigate }: { item: NavItem; status: SystemStatus | null; onNavigate: () => void }) {
  const count = status && item.badge ? item.badge(status) : 0;
  return (
    <li>
      <NavLink to={item.to} end={item.to === "/"} className={({ isActive }) => (isActive ? "active" : "")} onClick={onNavigate} title={item.label}>
        <Icon name={item.icon} size={20} strokeWidth={1.6} className="nav-icon" />
        <span className="nav-label">{item.label}</span>
        {count ? (
          <span className="nav-badge" aria-label={`${count} need attention`}>
            {count}
          </span>
        ) : null}
      </NavLink>
    </li>
  );
}

/** The workflow engine's words in the footer. The webhook is never probed: it reports deliveries only, never "connected" and
 * never a warning just because time has passed (C2-04). */
function engineWords(status: SystemStatus, now: number): string {
  const webhook = status.workflow_engine === "webhook";
  switch (engineState(status, now)) {
    case "none":
      return "not configured";
    case "not-contacted":
      return webhook ? "no delivery yet" : "not contacted yet";
    case "delivered":
      return `last delivery ${ago(statusAge(status, status.n8n?.last_ok_at, now) ?? 0)}`;
    case "connected":
      return "connected";
    case "failing":
      return webhook ? "last delivery failed" : "check Integrations";
    default:
      return status.n8n?.last_ok_at ? `not checked since ${clockTime(status.n8n.last_ok_at)}` : "not checked yet";
  }
}

/** Worker, workflow engine and AI lines, and the versions. While the status is stale every dot is "unknown" (hollow). */
function SystemFooter({ status, statusMeta }: { status: SystemStatus | null; statusMeta: StatusMeta }) {
  const now = useNow(10_000);
  const stale = isStale(statusMeta, now);
  const health = serviceHealth(status, statusMeta, now);
  // Short on screen; the line's title says why.
  const unknown = "unknown";
  const workerText = !status ? "" : stale ? unknown : health.worker === "good" ? "running" : "not reporting";
  const webhook = status?.workflow_engine === "webhook";
  const engineName = webhook ? "Webhook" : "n8n";
  const engineText = !status ? "" : stale ? unknown : engineWords(status, now);
  const ai = status ? aiState(status) : null;
  const aiText = !status
    ? "…"
    : stale
      ? unknown
      : ai === "rule-based"
        ? "rule-based"
        : ai === "failing"
          ? "last call failed"
          : (status.llm.model ?? "Claude");
  const why = stale && status ? " (no current status: the Command Center is not answering)" : "";
  const versions = `Dashboard v${__APP_VERSION__}${status ? ` · API v${status.version}` : ""}`;
  return (
    // On the icon rail the texts are visually hidden; the dots differ in shape (see shell.css) and the titles carry the state.
    <div className="sidebar-foot">
      <div className="health-title nav-label">System</div>
      <div className="health-line" title={`Worker (runs the agents' tasks)${workerText ? `: ${workerText}` : ""}${why}`}>
        <span className={`dot dot-${health.worker}`} /> <span className="nav-label">Worker {workerText}</span>
      </div>
      <div className="health-line" title={`${engineName} (workflow engine)${engineText ? `: ${engineText}` : ""}${why}`}>
        <span className={`dot dot-${health.n8n}`} />{" "}
        <span className="nav-label">
          {engineName}
          {webhook && engineText ? ":" : ""} {engineText}
        </span>
      </div>
      <div className="health-line" title={`AI planner: ${aiText}${why}`}>
        <span className={`dot dot-${health.ai}`} /> <span className="nav-label">AI: {aiText}</span>
      </div>
      <div className="health-line muted version-line nav-label" title={`${versions}${status?.edition ? ` · ${status.edition.title}` : ""}`}>
        {/* breaks, if it must, between the two versions */}
        <span className="nowrap">Dashboard v{__APP_VERSION__}</span>
        {status ? (
          <>
            {" · "}
            <span className="nowrap">API v{status.version}</span>
          </>
        ) : null}
      </div>
      {status && status.version !== __APP_VERSION__ ? (
        <div className="health-line" title={`The API reports version ${status.version}; this dashboard is ${__APP_VERSION__}`}>
          <span className="dot dot-warn" /> <span className="nav-label text-warn">API v{status.version}: not updated</span>
        </div>
      ) : null}
    </div>
  );
}

export default function Sidebar({
  user,
  status,
  statusMeta,
  open,
  onNavigate,
  onClose,
}: {
  user: User;
  status: SystemStatus | null;
  statusMeta: StatusMeta;
  open: boolean;
  onNavigate: () => void;
  onClose: () => void;
}) {
  const { pathname } = useLocation();
  const [moreOpen, setMoreOpen] = useState(readMore);
  const moreId = useId();
  const canOperate = user.role === "admin" || user.role === "operator";
  const main = MAIN_NAV.filter((i) => visible(i, user));
  const ops = OPS_NAV.filter((i) => visible(i, user));
  const more = MORE_NAV.filter((i) => visible(i, user));
  const moreCount = badgeTotal(more, status);
  const onMorePage = more.some((i) => pathname === i.to || pathname.startsWith(`${i.to}/`));

  // A page under "More" that is open (e.g. from a link) shows its entry, so the active page is never hidden.
  useEffect(() => {
    if (onMorePage) setMoreOpen(true);
  }, [onMorePage]);

  // Phone drawer: the first link takes the focus when it opens.
  useEffect(() => {
    if (open) document.querySelector<HTMLElement>(".sidebar nav a")?.focus();
  }, [open]);

  function toggleMore() {
    const next = !moreOpen;
    setMoreOpen(next);
    saveMore(next);
  }

  return (
    <aside className={`sidebar ${open ? "open" : ""}`} aria-label="Main navigation">
      <button className="top-btn drawer-close" aria-label="Close the menu" onClick={onClose}>
        <Icon name="close" />
      </button>
      {/* The menu and Quick Actions scroll when the window is short; Intern 2, the system lines and the versions stay pinned. */}
      <div className="sidebar-scroll">
        <nav>
          <ul className="nav-list">
            {main.map((item) => (
              <NavEntry key={item.to} item={item} status={status} onNavigate={onNavigate} />
            ))}
          </ul>
          <ul className="nav-list nav-ops">
            {ops.map((item) => (
              <NavEntry key={item.to} item={item} status={status} onNavigate={onNavigate} />
            ))}
          </ul>
          {more.length ? (
            <>
              <button type="button" className="nav-more" aria-expanded={moreOpen} aria-controls={moreId} onClick={toggleMore} title="More pages">
                <Icon name="dots" size={20} className="nav-icon" />
                <span className="nav-label">More</span>
                {moreCount && !moreOpen ? (
                  <span className="nav-badge" aria-label={`${moreCount} need attention`}>
                    {moreCount}
                  </span>
                ) : null}
                <Icon name="chevronDown" size={16} className="nav-more-chevron" />
              </button>
              <ul className="nav-list nav-more-list" id={moreId} hidden={!moreOpen}>
                {more.map((item) => (
                  <NavEntry key={item.to} item={item} status={status} onNavigate={onNavigate} />
                ))}
              </ul>
            </>
          ) : null}
        </nav>
        {canOperate ? <QuickActions user={user} status={status} /> : null}
      </div>
      <div className="sidebar-pinned">
        <DeviceCard />
        <SystemFooter status={status} statusMeta={statusMeta} />
      </div>
    </aside>
  );
}

export const MenuButton = forwardRef<HTMLButtonElement, { open: boolean; onClick: () => void }>(function MenuButton({ open, onClick }, ref) {
  return (
    <button ref={ref} className="top-btn menu-btn" aria-label={open ? "Close the menu" : "Open the menu"} aria-expanded={open} onClick={onClick}>
      <Icon name={open ? "close" : "menu"} />
    </button>
  );
});
