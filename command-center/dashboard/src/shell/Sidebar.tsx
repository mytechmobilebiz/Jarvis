import { forwardRef, useEffect } from "react";
import { NavLink } from "react-router-dom";
import type { SystemStatus, User } from "../api";
import { Icon } from "../icons";
import { NAV, visible } from "./nav";

export function serviceHealth(s: SystemStatus | null) {
  const worker = s ? (s.workers.some((w) => w.alive) ? "good" : "bad") : "muted";
  // No workflow engine set up, or not contacted yet: nothing to report, not a problem.
  const n8n = !s
    ? "muted"
    : s.workflow_engine === "none" || !s.n8n
      ? "muted"
      : s.n8n.last_ok_at && !(s.n8n.last_error_at && s.n8n.last_error_at > s.n8n.last_ok_at)
        ? "good"
        : "warn";
  const database = s ? (s.database.ok ? "good" : "bad") : "muted";
  const ai = s ? (s.llm.configured ? "good" : "muted") : "muted";
  return { worker, n8n, database, ai };
}

export default function Sidebar({
  user,
  status,
  open,
  onNavigate,
  onClose,
}: {
  user: User;
  status: SystemStatus | null;
  open: boolean;
  onNavigate: () => void;
  onClose: () => void;
}) {
  const health = serviceHealth(status);
  const workerText = health.worker === "good" ? "running" : status ? "not reporting" : "";
  const engineName = status?.workflow_engine === "webhook" ? "Webhook" : "n8n";
  const engineText = !status
    ? ""
    : status.workflow_engine === "none"
      ? "not configured"
      : !status.n8n
        ? "not contacted yet"
        : health.n8n === "good"
          ? "connected"
          : "check Integrations";
  const aiText = status ? (status.llm.configured ? status.llm.model : "rule-based") : "…";

  // Phone drawer: the first link takes the focus when it opens.
  useEffect(() => {
    if (open) document.querySelector<HTMLElement>(".sidebar nav a")?.focus();
  }, [open]);

  return (
    <aside className={`sidebar ${open ? "open" : ""}`} aria-label="Main navigation">
      <button className="top-btn drawer-close" aria-label="Close the menu" onClick={onClose}>
        <Icon name="close" />
      </button>
      <nav>
        {NAV.map((group) => {
          const items = group.items.filter((item) => visible(item, user));
          if (!items.length) return null;
          return (
            <div className="nav-group" key={group.title}>
              <div className="nav-group-title">{group.title}</div>
              {items.map((item) => {
                const count = status && item.badge ? item.badge(status) : 0;
                return (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.to === "/"}
                    className={({ isActive }) => (isActive ? "active" : "")}
                    onClick={onNavigate}
                    title={item.label}
                  >
                    <Icon name={item.icon} size={19} className="nav-icon" />
                    <span className="nav-label">{item.label}</span>
                    {count ? (
                      <span className="nav-badge" aria-label={`${count} need attention`}>
                        {count}
                      </span>
                    ) : null}
                  </NavLink>
                );
              })}
            </div>
          );
        })}
      </nav>
      {/* On the icon rail the texts are visually hidden; the dots differ in shape (see shell.css) and the titles carry the state. */}
      <div className="sidebar-foot">
        <div className="health-title">System</div>
        <div className="health-line" title={`Worker (runs the agents' tasks)${workerText ? `: ${workerText}` : ""}`}>
          <span className={`dot dot-${health.worker}`} /> <span className="nav-label">Worker {workerText}</span>
        </div>
        <div className="health-line" title={`${engineName} (workflow engine)${engineText ? `: ${engineText}` : ""}`}>
          <span className={`dot dot-${health.n8n}`} />{" "}
          <span className="nav-label">
            {engineName} {engineText}
          </span>
        </div>
        <div className="health-line" title={`AI planner: ${aiText}`}>
          <span className={`dot dot-${health.ai}`} /> <span className="nav-label">AI: {aiText}</span>
        </div>
        {status ? (
          <div className="health-line muted nav-label">
            v{status.version}
            {status.edition ? ` · ${status.edition.title}` : ""}
          </div>
        ) : null}
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
