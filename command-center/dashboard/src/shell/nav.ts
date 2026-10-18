import type { SystemStatus, User } from "../api";
import type { IconName } from "../icons";

export interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  badge?: (s: SystemStatus) => number;
  adminOnly?: boolean;
  sensitiveOnly?: boolean;
}

export interface NavGroup {
  title: string;
  items: NavItem[];
}

export const NAV: NavGroup[] = [
  {
    title: "Command",
    items: [
      { to: "/", label: "Dashboard", icon: "dashboard" },
      { to: "/agents", label: "AI Agents", icon: "agents" },
      { to: "/workflows", label: "Workflows", icon: "workflows" },
      { to: "/websites", label: "Websites", icon: "globe", badge: (s) => (s.counts.sites.down ?? 0) + (s.counts.sites.degraded ?? 0) },
      { to: "/seo", label: "SEO & Marketing", icon: "trending" },
      { to: "/servers", label: "CWP / Servers", icon: "server" },
      { to: "/email", label: "Email", icon: "mail" },
      { to: "/credit-repair", label: "Credit Repair", icon: "shield", sensitiveOnly: true },
      { to: "/social", label: "Social Media / Postonomous", icon: "megaphone" },
      { to: "/logs", label: "Automation Logs", icon: "logs" },
      { to: "/kb", label: "Knowledge Base", icon: "book" },
      { to: "/settings", label: "Settings", icon: "settings", adminOnly: true },
    ],
  },
  {
    title: "Operations",
    items: [
      { to: "/approvals", label: "Approvals", icon: "approvals", badge: (s) => s.counts.approvals_pending },
      { to: "/tasks", label: "Tasks", icon: "tasks" },
      { to: "/supervisor", label: "AI Supervisor", icon: "sparkles" },
      { to: "/leads", label: "Leads", icon: "users" },
      { to: "/support", label: "Client support", icon: "inbox" },
      { to: "/drafts", label: "Drafts & reports", icon: "file" },
      { to: "/outbox", label: "Outbox", icon: "send" },
      { to: "/integrations", label: "Integrations", icon: "plug" },
      { to: "/activity", label: "Activity log", icon: "history", adminOnly: true },
    ],
  },
];

export function visible(item: NavItem, user: User): boolean {
  if (item.adminOnly && user.role !== "admin") return false;
  if (item.sensitiveOnly && !(user.role === "admin" || user.can_access_sensitive)) return false;
  return true;
}
