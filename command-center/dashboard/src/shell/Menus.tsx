import { ReactNode, useEffect, useId, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type CCEvent, type SystemStatus } from "../api";
import { useAuth } from "../App";
import { Pill, SeverityIcon, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";

const SEEN_KEY = "cc.notifications.seen";

/** A disclosure: a button that shows a small panel of links and buttons. Closes on outside click, Escape (focus goes back to the button),
 * focus moving elsewhere, or navigation inside it. `label` names the panel; `buttonLabel` (default `label`) names the button. */
function Popover({
  label,
  buttonLabel = label,
  button,
  children,
  className = "",
}: {
  label: string;
  buttonLabel?: string;
  button: ReactNode;
  children: (close: () => void) => ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const panelId = useId();
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div
      className={`popover ${className}`}
      ref={ref}
      // Focus moved to something outside (a click on nothing is handled by the mousedown listener).
      onBlur={(e) => e.relatedTarget && !e.currentTarget.contains(e.relatedTarget as Node) && setOpen(false)}
    >
      <button
        ref={buttonRef}
        className="top-btn"
        aria-label={buttonLabel}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((o) => !o)}
      >
        {button}
      </button>
      {open ? (
        <div className="popover-panel" id={panelId} role="group" aria-label={label}>
          {children(() => setOpen(false))}
        </div>
      ) : null}
    </div>
  );
}

function readSeen(): number {
  try {
    return Number(localStorage.getItem(SEEN_KEY) ?? 0) || 0;
  } catch {
    return 0;
  }
}

export function NotificationsMenu({ status }: { status: SystemStatus | null }) {
  const events = usePoll(() => api.get<{ events: CCEvent[] }>("/api/events?limit=30"), 20_000);
  const [seen, setSeen] = useState(readSeen);
  const alerts = (events.data?.events ?? []).filter((e) => e.severity !== "info");
  const unseen = alerts.filter((e) => e.id > seen).length;
  const approvals = status?.counts.approvals_pending ?? 0;
  const count = unseen + approvals;
  const markSeen = () => {
    const top = Math.max(seen, ...(events.data?.events ?? []).map((e) => e.id));
    setSeen(top);
    try {
      localStorage.setItem(SEEN_KEY, String(top));
    } catch {
      /* not remembered in a private window */
    }
  };
  return (
    <Popover
      label={count ? `Notifications (${count} new)` : "Notifications"}
      className="notifications"
      button={
        <>
          <Icon name="bell" size={21} strokeWidth={1.6} />
          {count ? <span className="top-badge">{count > 99 ? "99+" : count}</span> : null}
        </>
      }
    >
      {(close) => (
        <>
          <div className="popover-head">
            <strong>Notifications</strong>
            {unseen ? (
              <button className="btn ghost small" onClick={markSeen}>
                Mark as read
              </button>
            ) : null}
          </div>
          {approvals ? (
            <Link className="popover-row emphasis" to="/approvals" onClick={close}>
              <Icon name="approvals" size={18} />
              <span>
                <strong>{approvals}</strong> change{approvals > 1 ? "s" : ""} waiting for your approval
              </span>
            </Link>
          ) : null}
          {alerts.length ? (
            <ul className="popover-list">
              {alerts.slice(0, 8).map((e) => (
                <li key={e.id} className={e.id > seen ? "unseen" : ""}>
                  <SeverityIcon severity={e.severity} />
                  <div>
                    <div className="popover-title">{e.title}</div>
                    <div className="muted small">
                      {e.source} · <Time iso={e.at} />
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted popover-empty">No warnings or alerts recently.</p>
          )}
          <Link className="popover-foot" to="/logs" onClick={close}>
            All events in Automation Logs <Icon name="chevronRight" size={14} />
          </Link>
        </>
      )}
    </Popover>
  );
}

function initials(name: string): string {
  const parts = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return ((parts[0]?.[0] ?? "?") + (parts[1]?.[0] ?? "")).toUpperCase();
}

export function AccountMenu() {
  const { user, signOut } = useAuth();
  const name = user.display_name || user.email;
  const roleLabel = user.role === "admin" ? "Administrator" : user.role === "operator" ? "Operator" : "Viewer";
  const shortName = user.display_name || user.email.split("@")[0];
  return (
    <Popover
      label="Account menu"
      buttonLabel={`${shortName}, account menu`}
      className="account"
      button={
        <>
          <span className="avatar" aria-hidden>
            {initials(name)}
          </span>
          <span className="account-name">
            <span>{shortName}</span>
            <span className="account-role">{roleLabel}</span>
          </span>
        </>
      }
    >
      {(close) => (
        <>
          <div className="popover-head account-head">
            <span className="avatar lg" aria-hidden>
              {initials(name)}
            </span>
            <div>
              <strong>{name}</strong>
              <div className="muted small">{user.email}</div>
              <Pill value={user.role === "admin" ? "approved" : "info"} label={user.role} />
            </div>
          </div>
          {user.role === "admin" ? (
            <>
              <Link className="popover-row" to="/settings" onClick={close}>
                <Icon name="settings" size={18} /> Settings and users
              </Link>
              <Link className="popover-row" to="/activity" onClick={close}>
                <Icon name="history" size={18} /> Activity log
              </Link>
            </>
          ) : null}
          <Link className="popover-row" to="/kb" onClick={close}>
            <Icon name="book" size={18} /> Knowledge Base
          </Link>
          <button className="popover-row" onClick={() => void signOut()}>
            <Icon name="logout" size={18} /> Sign out
          </button>
        </>
      )}
    </Popover>
  );
}
