import type { SystemStatus, User } from "../api";
import type { IconName } from "../icons";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  /** The name in the document title when the sidebar label alone is too short to say what the page is. */
  title?: string;
  badge?: (s: SystemStatus) => number;
  adminOnly?: boolean;
  sensitiveOnly?: boolean;
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

/** Monitored sites that are down or degraded; a paused site's last state is not current, so it does not count (C2-09). */
function siteProblems(s: SystemStatus): number {
  const sites = s.counts.sites_enabled ?? s.counts.sites;
  return (sites.down ?? 0) + (sites.degraded ?? 0);
}

/** The reference's twelve sections, always visible. */
export const MAIN_NAV: NavItem[] = [
  { to: "/", label: "Dashboard", icon: "home" },
  { to: "/agents", label: "AI Agents", icon: "agents" },
  { to: "/workflows", label: "Workflows", icon: "tree" },
  { to: "/websites", label: "Websites", icon: "grid", badge: siteProblems },
  { to: "/seo", label: "SEO & Marketing", icon: "trending" },
  { to: "/servers", label: "CWP / Servers", icon: "server" },
  { to: "/email", label: "Email", icon: "mail" },
  { to: "/credit-repair", label: "Credit Repair", icon: "docs", sensitiveOnly: true },
  { to: "/social", label: "Postonomous", title: "Postonomous (social media)", icon: "chat" },
  { to: "/logs", label: "Automation Logs", icon: "docText" },
  { to: "/kb", label: "Knowledge Base", icon: "docLines" },
  { to: "/settings", label: "Settings", icon: "settings", adminOnly: true },
];

/** Operations pages that stay one click away: approvals must never be hidden (owner rule 3). */
export const OPS_NAV: NavItem[] = [
  { to: "/approvals", label: "Approvals", icon: "approvals", badge: (s) => s.counts.approvals_pending },
  { to: "/tasks", label: "Tasks", icon: "tasks" },
  { to: "/supervisor", label: "AI Supervisor", icon: "sparkles" },
  { to: "/activity", label: "Activity log", icon: "history", adminOnly: true },
];

/** Behind the "More" toggle; any badge among them shows on the toggle while it is closed. */
export const MORE_NAV: NavItem[] = [
  { to: "/leads", label: "Leads", icon: "users" },
  { to: "/support", label: "Client support", icon: "inbox" },
  { to: "/drafts", label: "Drafts & reports", icon: "file" },
  { to: "/outbox", label: "Outbox", icon: "send" },
  { to: "/integrations", label: "Integrations", icon: "plug" },
];

export const NAV: NavGroup[] = [
  { title: "Command", items: MAIN_NAV },
  { title: "Operations", items: OPS_NAV },
  { title: "More", items: MORE_NAV },
];

export function visible(item: NavItem, user: User): boolean {
  if (item.adminOnly && user.role !== "admin") return false;
  if (item.sensitiveOnly && !(user.role === "admin" || user.can_access_sensitive)) return false;
  return true;
}

/** The total of the badges of `items` (for the closed "More" toggle). */
export function badgeTotal(items: NavItem[], status: SystemStatus | null): number {
  if (!status) return 0;
  return items.reduce((n, item) => n + (item.badge ? item.badge(status) || 0 : 0), 0);
}

export const APP_TITLE = "Infinite AI Command Center";
/** Routes that are not in the sidebar. */
const OTHER_PAGES: Record<string, string> = { "/schedules": "Schedules" };

/** The document title for a route: "<page name> · Infinite AI Command Center". */
export function pageTitle(pathname: string): string {
  const path = pathname.replace(/\/+$/, "") || "/";
  const item = NAV.flatMap((g) => g.items).find((i) => i.to === path);
  const label = item ? (item.title ?? item.label) : OTHER_PAGES[path];
  return label ? `${label} · ${APP_TITLE}` : APP_TITLE;
}
