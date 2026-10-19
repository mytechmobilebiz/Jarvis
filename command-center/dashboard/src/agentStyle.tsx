// Per-agent look shared by the dashboard (cards, chart, tables): colour token, short name, page, capability keywords
// and a filled glyph. Colours are CSS variables in agents.css (validated as a chart palette there).
import type { CSSProperties, ReactNode } from "react";
import type { Agent } from "./api";
import "./agents.css";

export interface AgentStyle {
  /** CSS colour, always a token: var(--agent-<name>). */
  color: string;
  /** Short display name; the agent's full title stays available as its tooltip. */
  short: string;
  /** The page that shows the agent's work. */
  route: string;
  /** Keywords, each shown only when the agent has at least one of its capabilities ("repair_*" matches a prefix). */
  keywords: { label: string; caps: string[] }[];
}

export const AGENT_STYLE: Record<string, AgentStyle> = {
  wordpress_security: {
    color: "var(--agent-wordpress_security)",
    short: "WordPress Agent",
    route: "/websites",
    keywords: [
      { label: "Security scans", caps: ["security_scan", "security_scan_all"] },
      { label: "Diagnosis", caps: ["wp_diagnose"] },
      { label: "Repairs", caps: ["repair_*"] },
      { label: "Reports", caps: ["security_report"] },
    ],
  },
  seo_content: {
    color: "var(--agent-seo_content)",
    short: "SEO Agent",
    route: "/seo",
    keywords: [
      { label: "Page audits", caps: ["audit_page"] },
      { label: "Keyword briefs", caps: ["keyword_brief"] },
      { label: "Blog drafts", caps: ["draft_blog_post"] },
      { label: "Publishing", caps: ["publish_draft"] },
    ],
  },
  social_marketing: {
    color: "var(--agent-social_marketing)",
    short: "Marketing Agent",
    route: "/social",
    keywords: [
      { label: "Social drafts", caps: ["draft_social_posts"] },
      { label: "Postonomous", caps: ["publish_draft"] },
      { label: "Ad reports", caps: ["ads_report"] },
      { label: "Budgets", caps: ["change_budget"] },
    ],
  },
  server_management: {
    color: "var(--agent-server_management)",
    short: "Server Agent",
    route: "/servers",
    keywords: [
      { label: "CWP / VPS", caps: ["test_connection", "server_overview", "check_services"] },
      { label: "Server overview", caps: ["server_overview"] },
      { label: "Service checks", caps: ["check_services"] },
      { label: "Restarts", caps: ["restart_service"] },
    ],
  },
  communications: {
    color: "var(--agent-communications)",
    short: "Email Agent",
    route: "/email",
    keywords: [
      { label: "Lead scoring", caps: ["score_lead"] },
      { label: "Proposals", caps: ["draft_proposal"] },
      { label: "Ticket triage", caps: ["triage_ticket"] },
      { label: "Replies", caps: ["draft_reply"] },
    ],
  },
  monitoring: {
    color: "var(--agent-monitoring)",
    short: "Monitoring Agent",
    route: "/websites",
    keywords: [
      { label: "Uptime", caps: ["check_site", "check_url", "check_all_sites"] },
      { label: "TLS", caps: ["check_site", "check_url", "check_all_sites"] },
      { label: "Redirects", caps: ["check_site", "check_url", "check_all_sites"] },
      { label: "Server reachability", caps: ["check_server"] },
    ],
  },
  credit_repair: {
    color: "var(--agent-credit_repair)",
    short: "Credit Repair Agent",
    route: "/credit-repair",
    keywords: [
      { label: "Dispute letters", caps: ["draft_dispute_letters"] },
      { label: "Compliance review", caps: ["compliance_review"] },
      { label: "Mailing", caps: ["mail_dispute_letter"] },
    ],
  },
  supervisor: { color: "var(--agent-supervisor)", short: "AI Supervisor", route: "/supervisor", keywords: [] },
};

const WORDS: Record<string, string> = { wp: "WordPress", wordpress: "WordPress", seo: "SEO", ai: "AI", url: "URL", tls: "TLS", ssh: "SSH", n8n: "n8n", cwp: "CWP" };

/** "check_all_sites" -> "Check all sites". */
export function humanize(slug: string): string {
  const s = slug
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((w) => WORDS[w.toLowerCase()] ?? w.toLowerCase())
    .join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** The look of an agent this dashboard does not know: grey, its own name, the Agents page. */
export function agentStyle(name: string, title?: string): AgentStyle {
  return AGENT_STYLE[name] ?? { color: "var(--agent-other)", short: title || humanize(name), route: "/agents", keywords: [] };
}

function hasCap(names: Set<string>, cap: string): boolean {
  if (cap.endsWith("*")) {
    const prefix = cap.slice(0, -1);
    return [...names].some((n) => n.startsWith(prefix));
  }
  return names.has(cap);
}

/** Keywords for the agent's capabilities that exist; an unknown agent gets its capability names. */
export function agentKeywords(agent: Pick<Agent, "name" | "capabilities">): string[] {
  const names = new Set(agent.capabilities.map((c) => c.name));
  const style = AGENT_STYLE[agent.name];
  if (!style || !style.keywords.length) return agent.capabilities.slice(0, 4).map((c) => humanize(c.name));
  return style.keywords.filter((k) => k.caps.some((c) => hasCap(names, c))).map((k) => k.label);
}

const LIGHT = { fill: "#fff", fillOpacity: 0.24 };
const MARK = { stroke: "#fff", strokeLinecap: "round" as const, strokeLinejoin: "round" as const, fill: "none" };

const GLYPHS: Record<string, ReactNode> = {
  wordpress_security: (
    <>
      <path d="M12 2.5 4 5.6v6c0 4.9 3.4 8.7 8 9.9 4.6-1.2 8-5 8-9.9v-6L12 2.5Z" fill="currentColor" />
      <path d="M12 2.5v19c4.6-1.2 8-5 8-9.9v-6L12 2.5Z" {...LIGHT} />
      <path d="m8.4 12.1 2.5 2.5 4.7-4.9" {...MARK} strokeWidth={2} />
    </>
  ),
  seo_content: (
    <>
      <circle cx="10.3" cy="10.3" r="6.3" fill="currentColor" fillOpacity={0.28} />
      <circle cx="10.3" cy="10.3" r="6.3" fill="none" stroke="currentColor" strokeWidth={2.6} />
      <path d="m15 15 5.4 5.4" stroke="currentColor" strokeWidth={3.2} strokeLinecap="round" />
      <path d="M7.3 9.4a3.3 3.3 0 0 1 2.6-2.6" {...MARK} strokeOpacity={0.75} strokeWidth={1.6} />
    </>
  ),
  social_marketing: (
    <>
      <path d="M3 9.6A1.6 1.6 0 0 1 4.6 8H8l8.6-4.6v17.2L8 16H4.6A1.6 1.6 0 0 1 3 14.4V9.6Z" fill="currentColor" />
      <path d="M8 8l8.6-4.6v17.2L8 16Z" {...LIGHT} />
      <path d="M5.8 16.2 7.3 20.7a1.2 1.2 0 0 0 1.1.8h1.5L8.3 16.2Z" fill="currentColor" />
      <path d="M19.4 9.3a3.4 3.4 0 0 1 0 5.4" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" />
    </>
  ),
  server_management: (
    <>
      <rect x="3" y="3.2" width="18" height="5.2" rx="1.6" fill="currentColor" />
      <rect x="3" y="9.4" width="18" height="5.2" rx="1.6" fill="currentColor" />
      <rect x="3" y="15.6" width="18" height="5.2" rx="1.6" fill="currentColor" />
      <rect x="3" y="3.2" width="18" height="5.2" rx="1.6" {...LIGHT} />
      {[5.8, 12, 18.2].map((y) => (
        <g key={y}>
          <circle cx="6.6" cy={y} r="1.1" fill="#fff" />
          <path d={`M10.2 ${y}h7.4`} {...MARK} strokeOpacity={0.6} strokeWidth={1.5} />
        </g>
      ))}
    </>
  ),
  communications: (
    <>
      <rect x="2.5" y="5" width="19" height="14" rx="2.2" fill="currentColor" />
      <path d="M2.5 17 9.6 11.6 12 13.4l2.4-1.8L21.5 17v.8a1.2 1.2 0 0 1-1.2 1.2H3.7a1.2 1.2 0 0 1-1.2-1.2V17Z" {...LIGHT} />
      <path d="m3.8 6.6 8.2 6.3 8.2-6.3" {...MARK} strokeOpacity={0.9} strokeWidth={1.8} />
    </>
  ),
  monitoring: (
    <>
      <circle cx="12" cy="12" r="9.5" fill="currentColor" />
      <path d="M2.5 12a9.5 9.5 0 0 1 19 0Z" {...LIGHT} fillOpacity={0.16} />
      <path d="M5.4 12.6h3l1.9-4.3 3.3 8.2 1.9-3.9h3.1" {...MARK} strokeWidth={1.9} />
    </>
  ),
  credit_repair: (
    <>
      <path d="M6.5 2.5H14l5 5V20a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 20V4a1.5 1.5 0 0 1 1.5-1.5Z" fill="currentColor" />
      <path d="M14 2.5V7a.5.5 0 0 0 .5.5H19Z" {...LIGHT} fillOpacity={0.5} />
      <path d="M8.5 11.6h7M8.5 14.6h7M8.5 17.6h4.4" {...MARK} strokeOpacity={0.88} strokeWidth={1.6} />
    </>
  ),
  robot: (
    <>
      <path d="M12 3.8v3" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" />
      <circle cx="12" cy="3.3" r="1.5" fill="currentColor" />
      <rect x="4" y="6.8" width="16" height="12.4" rx="3.6" fill="currentColor" />
      <rect x="1.8" y="10.8" width="2.2" height="4.6" rx="1.1" fill="currentColor" />
      <rect x="20" y="10.8" width="2.2" height="4.6" rx="1.1" fill="currentColor" />
      <rect x="4" y="6.8" width="16" height="6" rx="3" {...LIGHT} fillOpacity={0.16} />
      <circle cx="9" cy="12.4" r="1.7" fill="#fff" />
      <circle cx="15" cy="12.4" r="1.7" fill="#fff" />
      <path d="M9.4 16.1h5.2" {...MARK} strokeOpacity={0.75} strokeWidth={1.5} />
    </>
  ),
};

/** The agent's filled glyph in its colour; decorative unless `label` is given. */
export function AgentIcon({ name, size = 24, label, color, className = "" }: { name: string; size?: number; label?: string; color?: string; className?: string }) {
  const style: CSSProperties = { color: color ?? agentStyle(name).color };
  return (
    <svg
      className={`agent-icon ${className}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      style={style}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
      focusable="false"
    >
      {GLYPHS[name] ?? GLYPHS.robot}
    </svg>
  );
}

/** A small tinted square with the agent's glyph (table rows, workflow rows). */
export function AgentChip({ name, size = 15 }: { name: string; size?: number }) {
  const color = agentStyle(name).color;
  return (
    <span className="agent-chip" style={{ "--chip-color": color } as CSSProperties} aria-hidden>
      <AgentIcon name={name} size={size} color={color} />
    </span>
  );
}
