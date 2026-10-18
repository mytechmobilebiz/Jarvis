import { NavLink } from "react-router-dom";
import type { SystemStatus, User } from "../api";
import { Icon } from "../icons";
import { NAV, visible } from "./nav";

export function serviceHealth(s: SystemStatus | null) {
  const worker = s ? (s.workers.some((w) => w.alive) ? "good" : "bad") : "muted";
  const n8n = s ? (s.n8n?.last_ok_at && !(s.n8n.last_error_at && s.n8n.last_error_at > s.n8n.last_ok_at) ? "good" : "warn") : "muted";
  const database = s ? (s.database.ok ? "good" : "bad") : "muted";
  const ai = s ? (s.llm.configured ? "good" : "muted") : "muted";
  return { worker, n8n, database, ai };
}

export default function Sidebar({ user, status, open, onNavigate }: { user: User; status: SystemStatus | null; open: boolean; onNavigate: () => void }) {
  const health = serviceHealth(status);
  return (
    <aside className={`sidebar ${open ? "open" : ""}`} aria-label="Main navigation">
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
      <div className="sidebar-foot">
        <div className="health-title">System</div>
        <div className="health-line" title="Worker: runs the agents' tasks">
          <span className={`dot dot-${health.worker}`} /> <span className="nav-label">Worker {health.worker === "good" ? "running" : status ? "not reporting" : ""}</span>
        </div>
        <div className="health-line" title="n8n: workflow engine">
          <span className={`dot dot-${health.n8n}`} /> <span className="nav-label">n8n {health.n8n === "good" ? "connected" : status ? "check Integrations" : ""}</span>
        </div>
        <div className="health-line" title="AI planner">
          <span className={`dot dot-${health.ai}`} />{" "}
          <span className="nav-label">AI: {status ? (status.llm.configured ? status.llm.model : "rule-based") : "…"}</span>
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

export function MenuButton({ open, onClick }: { open: boolean; onClick: () => void }) {
  return (
    <button className="top-btn menu-btn" aria-label={open ? "Close the menu" : "Open the menu"} aria-expanded={open} onClick={onClick}>
      <Icon name={open ? "close" : "menu"} />
    </button>
  );
}
