// Readable one-line descriptions of tasks for the Recent Tasks table, built from the capability and its key parameter.
import type { Server, Site, Task, User } from "../api";
import { humanize } from "../agentStyle";
import { hidesTaskDetail } from "../work";

/** The AI Supervisor's own planning and summary steps: bookkeeping, not work anyone asked for. */
export function isInternal(t: Pick<Task, "agent" | "capability">): boolean {
  return t.agent === "supervisor" && (t.capability === "plan_request" || t.capability === "summarize_request");
}

export interface TaskContext {
  sites: Map<number, Site>;
  servers: Map<number, Server>;
  user: User;
}

const str = (v: unknown): string => (typeof v === "string" ? v.trim() : typeof v === "number" ? String(v) : "");

const REPAIR: Record<string, string> = {
  repair_restore_site_url: "Restore site URL",
  repair_deactivate_plugin: "Deactivate plugin",
  repair_activate_plugin: "Activate plugin",
  repair_update_plugin: "Update plugin",
  repair_reinstall_plugin: "Reinstall plugin",
  repair_reinstall_core: "Reinstall WordPress core",
  repair_maintenance_mode: "Maintenance mode",
  repair_flush_caches: "Flush caches",
  repair_quarantine_debug_log: "Quarantine debug.log",
};

/** "Check website: Client site", "Server overview: cwp-1", "Draft blog post: Spring tips". */
export function taskText(t: Task, ctx: TaskContext): string {
  if (hidesTaskDetail(t, ctx.user)) return "Credit repair task";
  const p = t.params ?? {};
  const siteId = typeof p.site_id === "number" ? p.site_id : null;
  const site = siteId !== null ? ctx.sites.get(siteId)?.name ?? `site #${siteId}` : "";
  const serverId = typeof p.server_id === "number" ? p.server_id : null;
  const server = serverId !== null ? ctx.servers.get(serverId)?.name ?? `server #${serverId}` : "";
  const on = (label: string, target: string) => (target ? `${label}: ${target}` : label);
  const key = `${t.agent}.${t.capability}`;
  switch (key) {
    case "monitoring.check_all_sites":
      return p.deep ? "Check all websites (deep)" : "Check all websites";
    case "monitoring.check_site":
      return on("Check website", site);
    case "monitoring.check_url":
      return on("Check URL", str(p.url));
    case "monitoring.check_server":
      return on("Check server reachability", str(p.host));
    case "monitoring.sites_overview":
      return "Websites overview";
    case "monitoring.uptime_report":
      return on("Uptime report", site || "all websites");
    case "wordpress_security.security_scan":
      return on("Security scan", site);
    case "wordpress_security.security_scan_all":
      return "Security scan of all websites";
    case "wordpress_security.security_report":
      return on("Security report", site || "all websites");
    case "wordpress_security.wp_diagnose":
      return on("Diagnose WordPress", site);
    case "seo_content.audit_page":
      return on("SEO audit", str(p.url));
    case "seo_content.keyword_brief":
      return on("Keyword brief", str(p.topic));
    case "seo_content.draft_blog_post":
      return on("Draft blog post", str(p.topic));
    case "seo_content.publish_draft":
    case "social_marketing.publish_draft":
      return `Publish draft${str(p.artifact_id) ? ` #${str(p.artifact_id)}` : ""}${str(p.destination) ? ` to ${str(p.destination)}` : ""}`;
    case "social_marketing.draft_social_posts":
      return on("Draft social posts", str(p.topic));
    case "social_marketing.ads_report":
      return "Ads report (sample data)";
    case "social_marketing.change_budget":
      return on("Change ad budget", str(p.campaign_id));
    case "server_management.test_connection":
      return on("Test connection", server);
    case "server_management.server_overview":
      return on("Server overview", server);
    case "server_management.check_services":
      return on("Check services", server);
    case "server_management.restart_service":
      return `Restart ${str(p.service) || "service"}${server ? ` on ${server}` : ""}`;
    case "communications.score_lead":
      return `Score lead #${str(p.lead_id)}`;
    case "communications.draft_proposal":
      return `Draft proposal for lead #${str(p.lead_id)}`;
    case "communications.send_proposal":
      return `Send proposal to lead #${str(p.lead_id)}`;
    case "communications.triage_ticket":
      return `Triage ticket #${str(p.ticket_id)}`;
    case "communications.draft_reply":
      return `Draft reply to ticket #${str(p.ticket_id)}`;
    case "communications.send_reply":
      return `Send reply to ticket #${str(p.ticket_id)}`;
    case "communications.lead_overview":
      return "Leads overview";
    case "communications.support_overview":
      return "Support overview";
    case "supervisor.daily_briefing":
      return "Daily briefing";
    case "supervisor.health_review":
      return "Health review";
    case "credit_repair.compliance_review":
      return "Compliance review";
    case "credit_repair.draft_dispute_letters":
      return "Draft dispute letters";
    case "credit_repair.mail_dispute_letter":
      return "Mail dispute letter";
  }
  if (t.agent === "wordpress_security" && REPAIR[t.capability]) {
    const what = REPAIR[t.capability] + (str(p.slug) ? ` ${str(p.slug)}` : "") + (t.capability === "repair_maintenance_mode" ? (p.on ? " on" : " off") : "");
    return `Repair: ${what}${site ? ` on ${site}` : ""}`;
  }
  return humanize(t.capability);
}

export interface StatusPill {
  label: string;
  tone: "done" | "running" | "queued" | "approval" | "failed" | "muted";
}

const PILLS: Record<Task["status"], StatusPill> = {
  succeeded: { label: "Completed", tone: "done" },
  running: { label: "Running", tone: "running" },
  queued: { label: "Queued", tone: "queued" },
  awaiting_approval: { label: "Needs approval", tone: "approval" },
  failed: { label: "Failed", tone: "failed" },
  rejected: { label: "Rejected", tone: "muted" },
  cancelled: { label: "Cancelled", tone: "muted" },
};

export function statusPill(status: Task["status"]): StatusPill {
  return PILLS[status] ?? { label: humanize(status), tone: "muted" };
}

/** Finished minus started; live while running; null while it has not started (queued, waiting for approval). */
export function taskDuration(t: Pick<Task, "status" | "started_at" | "finished_at">, now: number = Date.now()): number | null {
  if (!t.started_at) return null;
  const start = Date.parse(t.started_at);
  if (t.finished_at) return Date.parse(t.finished_at) - start;
  return t.status === "running" ? now - start : null;
}
