import { Fragment, ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { api, type Agent, type Integration, type Risk } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, RiskBadge } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import "./KnowledgeBase.css";

/* Articles are data so the search can read them. Inline markup in strings:
   **bold**, `code`, [[Key]] for a keyboard key, [label](/route) or [label](#article). */
type Block =
  | { p: string }
  | { h: string }
  | { ul: string[] }
  | { ol: string[] }
  | { dl: [string, string][] }
  | { note: string; tone?: "info" | "warn" }
  | { risks: [Risk, string][] }
  | { areas: Area["group"] }
  | { live: "agents" | "channels" | "role" };

interface Area {
  group: "Command" | "Operations";
  to: string;
  label: string;
  icon: IconName;
  text: string;
  admin?: boolean;
  sensitive?: boolean;
}

// The sidebar (shell/nav.ts), explained.
const AREAS: Area[] = [
  { group: "Command", to: "/", label: "Dashboard", icon: "dashboard", text: "Live overview: agents, websites, tasks completed today, uptime, service health, approvals and alerts." },
  { group: "Command", to: "/agents", label: "AI Agents", icon: "agents", text: "The AI Supervisor and the seven agents. Switch one on or off, or run a capability by hand." },
  { group: "Command", to: "/workflows", label: "Workflows", icon: "workflows", text: "Scheduled automations, the n8n workflow engine and recent AI Supervisor requests." },
  { group: "Command", to: "/websites", label: "Websites", icon: "globe", text: "Monitored sites: uptime, checks and findings; WP-CLI checks and repairs for sites linked to a server." },
  { group: "Command", to: "/seo", label: "SEO & Marketing", icon: "trending", text: "SEO audits, keyword briefs, blog drafts and ad reports. Publishing and budget changes wait for approval." },
  { group: "Command", to: "/servers", label: "CWP / Servers", icon: "server", text: "Servers reached over SSH through cc-gate, as a restricted non-root account (ccagent by default): host keys, connection tests and, for operators and admins, every command sent." },
  { group: "Command", to: "/email", label: "Email", icon: "mail", text: "Client messages, leads and outgoing email. Replies and proposals are sent only after approval." },
  { group: "Command", to: "/credit-repair", label: "Credit Repair", icon: "shield", text: "The isolated Credit Repair module. Admins see the page so they can switch it on; client data needs the sensitive-data permission, admins included.", sensitive: true },
  { group: "Command", to: "/social", label: "Social Media / Postonomous", icon: "megaphone", text: "Social post drafts for review. Publishing through Postonomous needs approval." },
  { group: "Command", to: "/logs", label: "Automation Logs", icon: "logs", text: "Every alert, task result, approval request and monitoring finding, and whether it reached the workflow engine." },
  { group: "Command", to: "/kb", label: "Knowledge Base", icon: "book", text: "These guides." },
  { group: "Command", to: "/settings", label: "Settings", icon: "settings", text: "Business details, AI provider key, edition and licence, users and roles.", admin: true },
  { group: "Operations", to: "/approvals", label: "Approvals", icon: "approvals", text: "Actions waiting for a person to approve or reject. The badge shows how many." },
  { group: "Operations", to: "/tasks", label: "Tasks", icon: "tasks", text: "Every unit of agent work: status, parameters, log and result." },
  { group: "Operations", to: "/supervisor", label: "AI Supervisor", icon: "sparkles", text: "The full console for requests: the plan, each task and the summary." },
  { group: "Operations", to: "/leads", label: "Leads", icon: "users", text: "Enquiries from the dashboard or the n8n lead-intake webhook, scored by the agent." },
  { group: "Operations", to: "/support", label: "Client support", icon: "inbox", text: "Client messages, triaged with a reply drafted for review." },
  { group: "Operations", to: "/drafts", label: "Drafts & reports", icon: "file", text: "Briefings, audits, proposals and content the agents wrote. Reviewing never publishes." },
  { group: "Operations", to: "/outbox", label: "Outbox", icon: "send", text: "Every approved outward action, and whether it was really sent or only recorded." },
  { group: "Operations", to: "/integrations", label: "Integrations", icon: "plug", text: "The workflow engine, Claude and the outward channels, each marked live, mock or disabled." },
  { group: "Operations", to: "/activity", label: "Activity log", icon: "history", text: "The audit log of everything a person did.", admin: true },
];

interface Article {
  id: string;
  title: string;
  icon: IconName;
  tone: "blue" | "purple" | "orange" | "cyan" | "green";
  summary: string;
  link?: { to: string; label: string };
  blocks: Block[];
}

const ARTICLES: Article[] = [
  {
    id: "getting-started",
    title: "Getting started",
    icon: "dashboard",
    tone: "blue",
    summary: "What the Command Center does, where everything is, and what your role allows.",
    link: { to: "/", label: "Dashboard" },
    blocks: [
      {
        p: "Infinite AI Command Center is your private automation platform. The **AI Supervisor** turns plain-language requests into tasks for the specialist agents that are switched on: they monitor websites, scan and repair WordPress, check servers, draft content and handle client communication. It never routes work to Credit Repair, which handles protected personal data. Anything that would publish, send, spend money or change a site waits for a person to approve it.",
      },
      {
        p: "Everything runs in Docker on one machine and listens only on that machine (`127.0.0.1`) until a TLS proxy is deliberately put in front of it. The dashboard is normally at `http://localhost:8090`.",
      },
      { h: "The sidebar" },
      { areas: "Command" },
      { areas: "Operations" },
      { p: "On a tablet the sidebar shrinks to an icon rail; on a phone it opens from the menu button at the top left." },
      { h: "Roles" },
      {
        dl: [
          ["Viewer", "Can read every page except Settings, the Activity log and a server's command audit trail; Credit Repair also needs the sensitive-data permission. Cannot send requests, run, approve or change anything."],
          [
            "Operator",
            "Everything a viewer can, plus: send requests to the AI Supervisor, run agent capabilities, approve or reject (an operator can reject a production change, but only an admin can approve one), switch agents and schedules on or off, add production websites, leads and support tickets.",
          ],
          [
            "Admin",
            "Everything an operator can, plus: Settings, users and roles, the Activity log, servers and the SSH key, staging and test sites, linking sites to servers, and approving production changes by typing the target's name.",
          ],
        ],
      },
      {
        p: "Credit Repair client data needs a separate **sensitive-data** permission. It is off for everyone, admins included, until an admin ticks it for a user on the Settings page.",
      },
      { live: "role" },
    ],
  },
  {
    id: "command-bar",
    title: "The AI command bar",
    icon: "sparkles",
    tone: "purple",
    summary: "Ask for work in plain language from the top of any page.",
    link: { to: "/supervisor", label: "AI Supervisor" },
    blocks: [
      {
        p: "The command input at the top of every page sends a request to the **AI Supervisor**. Press [[Ctrl]] + [[K]] (or [[⌘]] + [[K]] on a Mac) to jump to it. Type what you need and press **Execute** or [[Enter]]; [[Esc]] closes the suggestions.",
      },
      { h: "What Execute does" },
      {
        ol: [
          "It creates an AI Supervisor request under your name, exactly like a request typed on the [AI Supervisor](/supervisor) page. Viewers cannot send requests.",
          "The supervisor plans the work with Claude when an Anthropic API key is set (on the Settings page, or as `ANTHROPIC_API_KEY` in `.env`) and the AI provider setting is not rule-based; otherwise it uses built-in keyword rules. A request also falls back to the rules if Claude fails, refuses or returns only invalid steps, or once the day's AI request limit (200 by default) is used up. Either way it can only choose real agent capabilities, at most eight per request.",
          "The planned tasks are queued for the agents. Tasks that need approval wait on the [Approvals](/approvals) page instead of running.",
          "The answer appears under the input, with links to the full request and to any approvals it is waiting for. Every request stays on the [AI Supervisor](/supervisor) page.",
        ],
      },
      { h: "Examples" },
      {
        ul: [
          "`Check all websites`",
          "`Run a security scan on every site`",
          "`Give me the daily briefing`",
          "`Is the system healthy?`",
          "`help` lists what each agent can do, with example requests, when the built-in rules are planning; with Claude planning you usually get a short answer instead.",
        ],
      },
      { h: "Voice dictation" },
      {
        p: "The microphone button appears only when dictation can work: a browser with speech recognition (Chrome or Edge), a secure address such as `http://localhost:8090`, and an admin or operator account. The browser then asks for microphone permission.",
      },
      {
        p: "The first time, the dashboard asks for your consent, because Chrome and Edge send the audio to Google or Microsoft to turn it into text. Dictation only fills the input. Nothing runs until you press **Execute**.",
      },
      {
        note: "No microphone button? Open the dashboard as `http://localhost:8090` rather than the PC's IP address. If the browser blocked the microphone, allow it from the padlock icon in the address bar.",
        tone: "info",
      },
    ],
  },
  {
    id: "approvals",
    title: "Approvals and risk levels",
    icon: "approvals",
    tone: "orange",
    summary: "Why some tasks wait for you, and how to decide them.",
    link: { to: "/approvals", label: "Approvals" },
    blocks: [
      {
        p: "Every agent capability declares a risk level in code. The task layer, not the agent and not the AI planner, decides from that level whether a person must approve the task before it runs. Neither an agent nor the AI can talk its way past an approval.",
      },
      {
        risks: [
          ["read", "Observes only: checks, scans, reports."],
          ["draft", "Writes drafts inside the Command Center only."],
          ["external", "Contacts or publishes to the outside world: email, social posts, blog publishing, mailed letters."],
          ["change", "Changes a website or server you manage: WordPress repairs, service restarts."],
          ["financial", "Moves or commits money, such as an ad budget change."],
          ["destructive", "An irreversible change or deletion."],
        ],
      },
      { h: "Production sites and servers" },
      {
        p: "A change to a site or server marked **production**, or to any site linked to a production server, can only be approved by an admin, who types the site's name (for a site change) or the server's name (for a server change) to confirm. New sites are production unless an admin registers them as staging or test.",
      },
      { h: "Checked again before it runs" },
      {
        p: "An approval covers the exact target it was given for. It is checked when you decide and again just before the change runs: if the site or server was re-linked, renamed or moved to production in the meantime, the change is refused and must be requested again. Outward channels also refuse to run without an approval on the task.",
      },
      { h: "Where to decide" },
      {
        p: "Open [Approvals](/approvals); the badge in the sidebar shows how many are waiting (the number on the bell in the top bar also counts unread warnings and alerts). Admins and operators approve or reject, with an optional note; only an admin can approve a production change, and deciding a Credit Repair action also needs the sensitive-data permission. Approving runs exactly the parameters shown. Viewers can only look. Every decision is recorded in the audit log.",
      },
    ],
  },
  {
    id: "agents",
    title: "The AI agents",
    icon: "agents",
    tone: "cyan",
    summary: "A live reference: what each agent can do and which actions need approval.",
    link: { to: "/agents", label: "AI Agents" },
    blocks: [
      {
        p: "This list is read live from your Command Center, so it always matches what is installed. Run a capability by hand from [AI Agents](/agents), or ask for it in the command bar.",
      },
      { live: "agents" },
    ],
  },
  {
    id: "websites",
    title: "Websites and monitoring",
    icon: "globe",
    tone: "green",
    summary: "Adding a site, what is checked, uptime figures and alerts.",
    link: { to: "/websites", label: "Websites" },
    blocks: [
      { h: "Adding a site" },
      {
        ol: [
          "Open [Websites](/websites) and enter the site's name and address. A bare domain such as `example.com` becomes `https://example.com`.",
          "Choose the environment. **Production** is the default; only an admin can register a staging or test site. The environment decides whether repairs need an admin's typed confirmation; a site linked to a production server counts as production whatever its own environment.",
          "A first, deep scan is queued straight away. Later scans compare against it.",
        ],
      },
      { h: "What is checked" },
      {
        ul: [
          "**Uptime**, every 15 minutes: the home page answers, how fast, and whether it shows a problem page such as Account Suspended, a database error or domain for sale.",
          "**SSL / TLS**, on every check: certificate validity and days left (a warning at 21 days or fewer, critical at 7 or fewer; an expired certificate marks the site down) and old protocols. Whether plain HTTP redirects to HTTPS is checked in deep scans: the nightly security scan, a new site's first scan and the **Security scan** button.",
          "**Redirects**: each hop is recorded. Redirects to a foreign domain, down to plain HTTP or in long chains are flagged; foreign domains are never visited.",
          "**Security**, nightly at 03:17 UTC: an exposed `debug.log`, directory listings, user lists in the REST API, XML-RPC, version disclosure and cloaking (different content for phones, desktops and search crawlers). Hidden iframes, injected scripts and spam on the home page are looked for on every check, the 15-minute sweep included.",
        ],
      },
      {
        p: "Every check is read-only: GET and HEAD requests to public addresses only. Nothing logs in, submits a form or changes the site.",
      },
      { h: "Uptime figures" },
      {
        p: "Uptime is the share of checks in the last 24 hours, 7 days and 30 days in which the site was not down; a degraded site still answers, so it counts as up. Checks run about every 15 minutes, so the figures are estimates at that resolution. Down periods are listed as incidents on the site's page.",
      },
      { h: "Alerts" },
      {
        p: "A site going **down** raises a critical event, **degraded** a warning, and a recovery an info event. New findings raise one event per check. Events appear on the Dashboard and in [Automation Logs](/logs), and warnings and critical events also under the bell in the top bar; warnings and above, and every recovery, are also forwarded to n8n. One security report covering every monitored site is written each Monday at 06:43 UTC and kept in [Drafts & reports](/drafts).",
      },
    ],
  },
  {
    id: "servers",
    title: "WordPress repairs and servers",
    icon: "server",
    tone: "blue",
    summary: "Restricted SSH through cc-gate, approved WP-CLI repairs, and how to test safely.",
    link: { to: "/servers", label: "CWP / Servers" },
    blocks: [
      {
        p: "The VPS & Server Management and WordPress Security & Repair agents reach a server over SSH only through **cc-gate**: the forced command of a dedicated, non-root account (by default `ccagent`). The key can run nothing else: no shell, no file transfer, no port forwarding. cc-gate performs a short allowlist of operations with checked arguments. The Command Center never logs in as root and cc-gate refuses to run as root; the only thing done as root is restarting a service the server's configuration lists as restartable (none by default), and only after approval.",
      },
      { h: "Connecting a server (admins)" },
      {
        ol: [
          "On [CWP / Servers](/servers), press **Create SSH key** and copy the key from the line shown, starting at `ssh-ed25519` (leave out the `command=…,restrict` part in front; `install-gate.sh` adds it). The private half stays encrypted in the Command Center and is never shown, logged or sent to an AI model.",
          "On the server, as root, run `install-gate.sh` from the package's `server` folder with that public key. It prints the server's host-key fingerprints: note them.",
          "List each WordPress site in `/etc/cc-gate/config.json`, keep `\"allow_changes\": false` for now, and run `install-gate.sh --sync-sites`.",
          "Add the server in the dashboard, press **Scan host key**, compare the fingerprint with the one the installer printed and trust it only if they match. From then on the key is pinned and a different one is refused.",
          "Press **Test connection**, then link each site to its server from the site's page and run a server-side check.",
        ],
      },
      { p: "The complete guide is `docs/SERVER_SETUP.md` in the package." },
      { h: "Repairs" },
      {
        p: "Restoring home and siteurl, deactivating, activating, updating or reinstalling a plugin, reinstalling core files, maintenance mode, flushing caches and quarantining `debug.log` are **change** risk: each waits for approval, and production needs an admin's typed confirmation. Every repair:",
      },
      {
        ol: [
          "checks the current state first and changes nothing if nothing is wrong (reinstalling core or a plugin and flushing caches always run);",
          "backs up the database into the server's backup folder first, or stops (maintenance mode, cache flushing and the `debug.log` quarantine need no backup);",
          "makes the one change that was approved, running WP-CLI as the site's own user in a clean environment;",
          "confirms the result over HTTP and, for most repairs, on the server. If it fails part-way, a critical `repair.failed` event names what was done and the backup file.",
        ],
      },
      { p: "A service can be restarted only if the server's configuration lists it as restartable, and only after approval." },
      {
        note: "Test on a disposable WordPress install or a non-production server first. So far the repairs have been tested only on a disposable WordPress install, not on a CWP server, and updating or reinstalling a plugin or core has not yet been seen to succeed (the test sandbox could not reach WordPress.org). While the server's own `allow_changes` is false, every change is refused, whatever was approved.",
        tone: "warn",
      },
      {
        p: "Every request is recorded in the Command Center (on the server's page) and in `/var/log/cc-gate/gate.log` on the server. To take access away, run `install-gate.sh --remove` on the server.",
      },
    ],
  },
  {
    id: "workflows",
    title: "Workflows and n8n",
    icon: "workflows",
    tone: "purple",
    summary: "Schedules, the n8n workflow engine and the events it receives.",
    link: { to: "/workflows", label: "Workflows" },
    blocks: [
      { h: "Schedules" },
      {
        p: "The worker runs agent routines on a timer. Out of the box: an uptime sweep every 15 minutes, a nightly security scan, an hourly AI Supervisor health review and a weekly security report on Monday mornings. Timings are cron expressions in UTC.",
      },
      {
        p: "See them on [Workflows](/workflows). Operators and admins can pause, resume or retime a schedule; viewers can only look. Every change is recorded in the audit log.",
      },
      { h: "The n8n workflow engine" },
      {
        p: "n8n runs beside the Command Center for cross-system work. Its editor is at `http://localhost:5680`, on this machine only. Three workflows are provisioned at start-up:",
      },
      {
        ul: [
          "**Event router**: receives events from the Command Center, decides whether you should be told and acknowledges each one back. Sending you a notification is not configured yet.",
          "**Morning briefing**: on weekday mornings, asks the AI Supervisor for the daily briefing.",
          "**Lead intake**: a webhook for website lead forms. It tidies the fields and hands the lead to the Command Center.",
        ],
      },
      {
        p: "n8n calls back with a service token. That token can submit AI Supervisor requests, hand in leads and support messages, and acknowledge events; it cannot read dashboard data or approve anything.",
      },
      { h: "Events forwarded to n8n" },
      {
        p: "Events of warning severity and above, plus every site recovery, are queued for n8n. The worker delivers them and retries a failed delivery with growing delays, up to 8 attempts. The connection state is on [Integrations](/integrations) and [Workflows](/workflows).",
      },
    ],
  },
  {
    id: "security",
    title: "Security and privacy",
    icon: "shield",
    tone: "green",
    summary: "Where your data lives, and what the dashboard never does on its own.",
    link: { to: "/integrations", label: "Integrations" },
    blocks: [
      {
        ul: [
          "**Hosted locally**: everything runs in Docker on your machine, bound to `127.0.0.1`. The dashboard loads nothing from other sites; even its typeface is bundled.",
          "**Secrets stay on the server**: passwords and keys live in `.env` on the machine. API keys entered on the Settings page are stored encrypted, and only a short hint is shown afterwards. Keep a copy of `.env` with your backups: its keys are needed to read stored credentials.",
          "**Audit log**: sign-ins, tasks, approvals, sites, schedules, agents, servers, settings and users are recorded with who, when and from which address. Admins read it in the [Activity log](/activity).",
          "**Monitoring is read-only**: GET and HEAD requests to public addresses only, with no logins. Redirects to other domains are recorded, never followed.",
          "**Model text is data**: fetched pages and agent output reach the AI only as data, and the planner can only choose real capabilities.",
          "**Credit Repair is isolated**: its own database and login role, field encryption with its own key and its own access log. The AI never sees it.",
        ],
      },
      { h: "Never without a person's approval" },
      {
        ul: [
          "Publishing anything: blog posts, social posts.",
          "Sending email or mailing a letter.",
          "Spending or committing money, such as ad budget changes.",
          "Changing a website or server: repairs and restarts.",
        ],
      },
      { h: "Outward channels in this build" },
      { live: "channels" },
    ],
  },
  {
    id: "troubleshooting",
    title: "Troubleshooting",
    icon: "alert",
    tone: "orange",
    summary: "Where to look first, and fixes for common problems.",
    link: { to: "/logs", label: "Automation Logs" },
    blocks: [
      { h: "Where to look" },
      {
        dl: [
          ["Automation Logs", "Alerts and events by severity: sites down, failed tasks, failed repairs, host-key mismatches."],
          ["Tasks", "Each task's status, parameters, log and result. Operators can retry a failed task."],
          ["Integrations", "The workflow engine, Claude and the outward channels, with their mode and last error."],
          ["Dashboard and sidebar", "Worker and service health: database, worker, n8n and the AI planner."],
        ],
      },
      { h: "Common problems" },
      {
        dl: [
          [
            "A task stays queued",
            "The worker is not running: check the Worker line at the bottom of the sidebar. On Windows, run `.\\scripts\\status.ps1`. Server-side tasks also wait while another one runs on the same WordPress install.",
          ],
          ["n8n shows a problem", "n8n takes about a minute on first start. Look at `docker compose logs --tail=100 n8n`; event deliveries are retried automatically."],
          ["Planning is rule-based", "No Anthropic API key is set, the AI provider on the Settings page is set to rule-based, or this edition has no AI models. A single request also falls back to the rules when today's AI limit is used up or the model call fails; its notes on the AI Supervisor page say so. An admin can add the key or change the provider on the Settings page."],
          [
            "An approved repair was refused",
            "If Approve is refused, or the approved task fails as refused, the site or server changed since the request (re-linked, renamed or moved to production): request the change again. If the server's `allow_changes` is false, every change is refused: set `\"allow_changes\": true` in `/etc/cc-gate/config.json` on the server first, then request it again.",
          ],
          ["No microphone button", "See [The AI command bar](#command-bar)."],
          ["Port already in use", "Set `DASHBOARD_PORT` or `N8N_PORT` in `.env`, then run `start.ps1`."],
          ["Images fail to download", "Check that Docker Desktop is signed in or that Docker Hub is reachable."],
          ["n8n sign-in fails from another machine", "By design: n8n is local-only until a TLS reverse proxy is added."],
        ],
      },
      { h: "Windows 11 install" },
      {
        p: "Follow `docs/DEPLOY_WINDOWS.md` in the package. `scripts\\install.ps1` must end with `SMOKE TEST PASSED`. Day to day: `status.ps1`, `stop.ps1`, `start.ps1 -NoBuild` and `backup.ps1`.",
      },
      {
        note: "Never run `docker compose down -v` unless you mean to delete all data. To update, back up first, extract the new package, copy your `.env` across and run `install.ps1`.",
        tone: "warn",
      },
    ],
  },
];

const ARTICLE_IDS = new Set(ARTICLES.map((a) => a.id));
const INTERNAL_CAPS = new Set(["supervisor.plan_request", "supervisor.summarize_request"]);
const AGENT_ICON: Record<string, IconName> = {
  supervisor: "sparkles",
  wordpress_security: "shield",
  seo_content: "trending",
  social_marketing: "megaphone",
  server_management: "server",
  communications: "mail",
  monitoring: "globe",
  credit_repair: "lock",
};
const CHANNEL_NOTE: Record<Integration["mode"], string> = {
  mock: "recorded in the Outbox; nothing leaves the machine",
  live: "really sends, after approval",
  disabled: "switched off",
};

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\[\[[^\]]+\]\]|\[[^\]]+\]\([^)\s]+\))/g;
const LINK = /^\[([^\]]+)\]\(([^)\s]+)\)$/;

function plain(s: string): string {
  return s
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\[\[([^\]]+)\]\]/g, "$1")
    .replace(/\[([^\]]+)\]\([^)\s]+\)/g, "$1");
}

const ACRONYMS: Record<string, string> = { wp: "WP", url: "URL", seo: "SEO", tls: "TLS", ssh: "SSH", ads: "Ads" };

function humanize(name: string): string {
  const s = name
    .split("_")
    .map((w) => ACRONYMS[w] ?? w)
    .join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function blockText(b: Block): string {
  if ("p" in b) return b.p;
  if ("h" in b) return b.h;
  if ("ul" in b) return b.ul.join(" ");
  if ("ol" in b) return b.ol.join(" ");
  if ("dl" in b) return b.dl.map(([t, d]) => `${t} ${d}`).join(" ");
  if ("note" in b) return b.note;
  if ("areas" in b) return AREAS.filter((a) => a.group === b.areas).map((a) => `${a.label} ${a.text}`).join(" ");
  if ("risks" in b) return b.risks.map(([r, d]) => `${r} ${d} ${r === "read" || r === "draft" ? "" : "needs approval"}`).join(" ");
  return "";
}

function agentText(a: Agent): string {
  return [a.title, a.name, a.description, ...a.capabilities.map((c) => `${humanize(c.name)} ${c.name} ${c.description} ${c.risk}`)].join(" ");
}

function visibleCaps(a: Agent) {
  return a.capabilities.filter((c) => !INTERNAL_CAPS.has(`${a.name}.${c.name}`));
}

function includesAll(text: string, terms: string[]): boolean {
  const t = text.toLowerCase();
  return terms.every((term) => t.includes(term));
}

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function prefersReducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Scrolls to an article and moves keyboard focus to it. */
function goTo(id: string, smooth: boolean) {
  const el = document.getElementById(id);
  if (!el) return false;
  el.scrollIntoView({ behavior: smooth && !prefersReducedMotion() ? "smooth" : "auto", block: "start" });
  el.focus({ preventScroll: true });
  return true;
}

function Hl({ text, re }: { text: string; re: RegExp | null }) {
  if (!re) return <>{text}</>;
  return (
    <>
      {text.split(re).map((part, i) =>
        i % 2 ? (
          <mark key={i} className="kb-mark">
            {part}
          </mark>
        ) : (
          part
        ),
      )}
    </>
  );
}

function Inline({ text, re }: { text: string; re: RegExp | null }) {
  return (
    <>
      {text.split(INLINE).map((part, i) => {
        if (!part) return null;
        if (part.length > 4 && part.startsWith("**") && part.endsWith("**"))
          return (
            <strong key={i}>
              <Hl text={part.slice(2, -2)} re={re} />
            </strong>
          );
        if (part.length > 2 && part.startsWith("`") && part.endsWith("`"))
          return (
            <code key={i} className={part.length <= 38 ? "kb-nowrap" : undefined}>
              <Hl text={part.slice(1, -1)} re={re} />
            </code>
          );
        if (part.startsWith("[[") && part.endsWith("]]"))
          return (
            <kbd key={i} className="kb-kbd">
              {part.slice(2, -2)}
            </kbd>
          );
        const link = LINK.exec(part);
        if (link) {
          const [, label, href] = link;
          return (
            <Link key={i} to={href.startsWith("#") ? `/kb${href}` : href}>
              <Hl text={label} re={re} />
            </Link>
          );
        }
        return <Hl key={i} text={part} re={re} />;
      })}
    </>
  );
}

function RiskTable({ risks, re }: { risks: [Risk, string][]; re: RegExp | null }) {
  return (
    <ul className="kb-risks">
      {risks.map(([risk, text]) => {
        const approval = risk !== "read" && risk !== "draft";
        return (
          <li key={risk}>
            <span className="kb-risk-name">
              <RiskBadge risk={risk} />
            </span>
            <span className="kb-risk-text">
              <strong>
                <Hl text={humanize(risk)} re={re} />.
              </strong>{" "}
              <Inline text={text} re={re} />
            </span>
            <span className={`kb-approval ${approval ? "needs" : "free"}`}>
              <Icon name={approval ? "lock" : "check"} size={13} />
              {approval ? "Needs approval" : "Runs straight away"}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function IntegrationChips({ names, integrations }: { names: string[]; integrations: Integration[] | null }) {
  if (!names.length) return null;
  return (
    <span className="kb-chips">
      {names.map((name) => {
        const found = integrations?.find((i) => i.name === name);
        return (
          <span key={name} className="kb-chip">
            {found ? found.title : name === "ssh" ? "SSH through cc-gate" : name}
            {found ? <Pill value={found.mode} /> : null}
          </span>
        );
      })}
    </span>
  );
}

function AgentReference({
  agents,
  integrations,
  error,
  terms,
  re,
  open,
  onToggle,
}: {
  agents: Agent[] | null;
  integrations: Integration[] | null;
  error: string | null;
  terms: string[];
  re: RegExp | null;
  open: Set<string>;
  onToggle: (name: string) => void;
}) {
  const { user } = useAuth();
  if (!agents) return error ? <ErrorNote error={error} /> : <p className="muted">Loading the agents…</p>;
  const ordered = [...agents.filter((a) => a.role === "supervisor"), ...agents.filter((a) => a.role !== "supervisor")];
  // While searching, show only the agents that match (all of them if the match is in the article's own text).
  const matching = terms.length ? ordered.filter((a) => includesAll(agentText(a), terms)) : [];
  const shown = matching.length ? matching : ordered;
  const canSeeCreditRepair = user.role === "admin" || !!user.can_access_sensitive;
  return (
    <>
      <ErrorNote error={error} />
      {!agents.length ? <Empty>No agents are installed.</Empty> : null}
      <ul className="kb-agents">
        {shown.map((a) => {
          const caps = visibleCaps(a);
          const approvals = caps.filter((c) => c.requires_approval).length;
          const expanded = open.has(a.name) || matching.includes(a);
          const listId = `kb-caps-${a.name}`;
          return (
            <li key={a.name} className="kb-agent">
              <div className="kb-agent-head">
                <span className={`kb-agent-icon ${a.role === "supervisor" ? "kb-tone-purple" : "kb-tone-cyan"}`} aria-hidden>
                  <Icon name={AGENT_ICON[a.name] ?? "agents"} size={18} />
                </span>
                <div className="kb-agent-main">
                  <h3 className="kb-agent-title">
                    <Hl text={a.title} re={re} />
                  </h3>
                  <div className="kb-agent-meta">
                    {!a.entitled ? (
                      <span className="pill tone-muted">Not in this edition</span>
                    ) : a.enabled ? (
                      <span className="pill tone-good">On</span>
                    ) : (
                      <span className="pill tone-muted">Switched off</span>
                    )}
                    {a.sensitive ? <span className="pill tone-warn">Sensitive data</span> : null}
                    <span className="muted small">
                      {caps.length} capabilit{caps.length === 1 ? "y" : "ies"} · {approvals ? `${approvals} need${approvals === 1 ? "s" : ""} approval` : "no approvals needed"}
                    </span>
                  </div>
                </div>
                <button className="btn small kb-agent-toggle" aria-expanded={expanded} aria-controls={listId} onClick={() => onToggle(a.name)}>
                  <span>{expanded ? "Hide" : "Capabilities"}</span>
                  <Icon name="chevronDown" size={15} className={expanded ? "kb-flip" : undefined} />
                  <span className="sr-only"> for {a.title}</span>
                </button>
              </div>
              <p className="kb-agent-desc">
                <Hl text={a.description} re={re} />
              </p>
              {a.integrations.length ? (
                <div className="kb-agent-integrations small">
                  <span className="muted">Uses</span>
                  <IntegrationChips names={a.integrations} integrations={integrations} />
                </div>
              ) : null}
              {a.sensitive ? (
                <div className="kb-note kb-note-warn small">
                  <Icon name="lock" size={16} />
                  <p>
                    Client data lives in its own encrypted database, with its own login role and access log. The AI Supervisor, the other agents and
                    this page never read it; elsewhere a client appears only as a reference such as <code className="kb-nowrap">CR-0001</code>. The module stays off until an
                    admin enables it.{" "}
                    {canSeeCreditRepair ? <Link to="/credit-repair">Open Credit Repair</Link> : "Your account does not have the sensitive-data permission."}
                  </p>
                </div>
              ) : null}
              {expanded ? (
                <ul className="kb-caps" id={listId}>
                  {caps.map((c) => (
                    <li key={c.name}>
                      <div className="kb-cap-main">
                        <div className="kb-cap-name">
                          <strong>
                            <Hl text={humanize(c.name)} re={re} />
                          </strong>
                          <code className="kb-cap-code">{c.name}</code>
                        </div>
                        <div className="muted small kb-cap-desc">
                          <Hl text={c.description} re={re} />
                        </div>
                      </div>
                      <div className="kb-cap-tags">
                        <RiskBadge risk={c.risk} />
                        {c.requires_approval ? (
                          <span className="kb-approval needs">
                            <Icon name="lock" size={13} />
                            Needs approval
                          </span>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : null}
            </li>
          );
        })}
      </ul>
    </>
  );
}

function Channels({ integrations, error }: { integrations: Integration[] | null; error: string | null }) {
  if (!integrations) return error ? <ErrorNote error={error} /> : <p className="muted">Loading the integrations…</p>;
  // The workflow engine (it lists workflows) and Claude are not outward channels.
  const outward = integrations.filter((i) => i.name !== "anthropic" && !("workflows" in i));
  if (!outward.length) return <Empty>No outward channels are configured.</Empty>;
  return (
    <>
      <ul className="kb-channels">
        {outward.map((i) => (
          <li key={i.name}>
            <span className="kb-channel-name">{i.title}</span>
            <Pill value={i.mode} />
            <span className="muted small kb-channel-note">{CHANNEL_NOTE[i.mode]}</span>
          </li>
        ))}
      </ul>
      <p className="muted small kb-foot">
        Read live from <Link to="/integrations">Integrations</Link>. Whatever the mode, anything a channel would send, publish or spend needs an approved task first; reading data, such as the ad platforms' sample figures, does not.
      </p>
    </>
  );
}

function Areas({ group, re }: { group: Area["group"]; re: RegExp | null }) {
  const { user } = useAuth();
  const items = AREAS.filter((a) => a.group === group);
  return (
    <>
      <h4 className="kb-label">{group}</h4>
      <ul className="kb-areas">
        {items.map((a) => {
          const allowed = (!a.admin || user.role === "admin") && (!a.sensitive || user.role === "admin" || !!user.can_access_sensitive);
          const inner = (
            <>
              <span className="kb-area-icon" aria-hidden>
                <Icon name={a.icon} size={16} />
              </span>
              <span className="kb-area-main">
                <span className="kb-area-name">
                  <Hl text={a.label} re={re} />
                  {a.admin ? <span className="kb-area-tag">Admins</span> : null}
                </span>
                <span className="kb-area-text">
                  <Hl text={a.text} re={re} />
                </span>
              </span>
            </>
          );
          return <li key={a.to}>{allowed ? <Link className="kb-area" to={a.to}>{inner}</Link> : <div className="kb-area">{inner}</div>}</li>;
        })}
      </ul>
    </>
  );
}

function RoleNote() {
  const { user } = useAuth();
  const role = user.role.charAt(0).toUpperCase() + user.role.slice(1);
  return (
    <div className="kb-note kb-note-info">
      <Icon name="user" size={16} />
      <p>
        You are signed in as <strong>{user.display_name || user.email}</strong> with the <strong>{role}</strong> role
        {user.can_access_sensitive ? " and the sensitive-data permission" : ""}.
      </p>
    </div>
  );
}

export default function KnowledgeBase() {
  const location = useLocation();
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 60_000);
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 0);
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [tocOpen, setTocOpen] = useState(false);
  const [active, setActive] = useState<string>(ARTICLES[0].id);
  const [pending, setPending] = useState<string | null>(null);
  const firstJump = useRef(true);
  const lastJump = useRef<{ id: string; at: number } | null>(null);

  const agentList = agents.data?.agents ?? null;
  const integrationList = integrations.data?.integrations ?? null;
  const terms = useMemo(() => query.trim().toLowerCase().split(/\s+/).filter(Boolean), [query]);
  const re = useMemo(() => (terms.length ? new RegExp(`(${terms.map(escapeRe).join("|")})`, "gi") : null), [terms]);

  const shown = useMemo(() => {
    if (!terms.length) return ARTICLES;
    return ARTICLES.filter((a) => {
      let text = [a.title, a.summary, ...a.blocks.map(blockText)].map(plain).join(" ");
      if (a.blocks.some((b) => "live" in b && b.live === "agents")) text += " " + (agentList ?? []).map(agentText).join(" ");
      if (a.blocks.some((b) => "live" in b && b.live === "channels")) text += " " + (integrationList ?? []).map((i) => `${i.title} ${i.mode}`).join(" ");
      return includesAll(text, terms);
    });
  }, [terms, agentList, integrationList]);
  const shownKey = shown.map((a) => a.id).join(",");

  // A hash in the address (/kb#approvals) asks for that article: on load, on every link click, and when it changes.
  useEffect(() => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (!ARTICLE_IDS.has(id)) return;
    if (!shown.some((a) => a.id === id)) setQuery("");
    setPending(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key, location.hash]);

  useEffect(() => {
    if (!pending) return;
    if (goTo(pending, !firstJump.current)) {
      firstJump.current = false;
      lastJump.current = { id: pending, at: Date.now() };
      setActive(pending);
      setPending(null);
    }
  }, [pending, shownKey]);

  // The agent list arrives after the first jump and pushes later articles down; jump again if that was just now.
  const agentsLoaded = !!agentList;
  useEffect(() => {
    const last = lastJump.current;
    if (agentsLoaded && last && Date.now() - last.at < 3000) goTo(last.id, false);
  }, [agentsLoaded]);

  // Highlight the article being read in the contents.
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const seen = new Map<string, boolean>();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) seen.set(e.target.id, e.isIntersecting);
        const first = shown.find((a) => seen.get(a.id));
        if (first) setActive(first.id);
      },
      { rootMargin: "-120px 0px -55% 0px" },
    );
    for (const a of shown) {
      const el = document.getElementById(a.id);
      if (el) io.observe(el);
    }
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownKey]);

  function toggleAgent(name: string) {
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  const allOpen = !!agentList?.length && agentList.every((a) => open.has(a.name));

  function renderBlock(b: Block, key: number): ReactNode {
    if ("p" in b)
      return (
        <p key={key}>
          <Inline text={b.p} re={re} />
        </p>
      );
    if ("h" in b)
      return (
        <h3 key={key} className="kb-h">
          <Hl text={b.h} re={re} />
        </h3>
      );
    if ("ul" in b)
      return (
        <ul key={key} className="kb-ul">
          {b.ul.map((t, i) => (
            <li key={i}>
              <Inline text={t} re={re} />
            </li>
          ))}
        </ul>
      );
    if ("ol" in b)
      return (
        <ol key={key} className="kb-ol">
          {b.ol.map((t, i) => (
            <li key={i}>
              <Inline text={t} re={re} />
            </li>
          ))}
        </ol>
      );
    if ("dl" in b)
      return (
        <dl key={key} className="kb-dl">
          {b.dl.map(([term, text]) => (
            <Fragment key={term}>
              <dt>
                <Hl text={term} re={re} />
              </dt>
              <dd>
                <Inline text={text} re={re} />
              </dd>
            </Fragment>
          ))}
        </dl>
      );
    if ("note" in b)
      return (
        <div key={key} className={`kb-note kb-note-${b.tone ?? "info"}`}>
          <Icon name={b.tone === "warn" ? "alert" : "check"} size={16} />
          <p>
            <Inline text={b.note} re={re} />
          </p>
        </div>
      );
    if ("risks" in b) return <RiskTable key={key} risks={b.risks} re={re} />;
    if ("areas" in b) return <Areas key={key} group={b.areas} re={re} />;
    if (b.live === "agents")
      return (
        <AgentReference
          key={key}
          agents={agentList}
          integrations={integrationList}
          error={agents.error}
          terms={terms}
          re={re}
          open={open}
          onToggle={toggleAgent}
        />
      );
    if (b.live === "channels") return <Channels key={key} integrations={integrationList} error={integrations.error} />;
    return <RoleNote key={key} />;
  }

  return (
    <div className="page kb-page">
      <header className="page-head kb-head">
        <div>
          <div className="eyebrow">Help</div>
          <h1>Knowledge Base</h1>
          <p className="muted">
            Practical guides to the Command Center: the command bar, approvals, the agents, websites, servers and workflows, and where to look when
            something goes wrong.
          </p>
        </div>
        <div className="kb-search" role="search" aria-label="Knowledge Base">
          <label htmlFor="kb-search-input" className="sr-only">
            Search the Knowledge Base
          </label>
          <Icon name="search" size={17} className="kb-search-icon" />
          <input
            id="kb-search-input"
            type="search"
            placeholder="Search articles…"
            value={query}
            autoComplete="off"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setQuery("")}
          />
          {query ? (
            <button className="btn ghost icon kb-search-clear" onClick={() => setQuery("")} aria-label="Clear the search">
              <Icon name="close" size={15} />
            </button>
          ) : null}
        </div>
      </header>

      <p className="sr-only" aria-live="polite">
        {terms.length ? `${shown.length} of ${ARTICLES.length} articles match` : ""}
      </p>

      <div className="kb-layout">
        <nav className={`kb-toc ${tocOpen ? "open" : ""}`} aria-label="Knowledge Base contents">
          <button className="kb-toc-toggle" aria-expanded={tocOpen} aria-controls="kb-toc-list" onClick={() => setTocOpen((o) => !o)}>
            <Icon name="book" size={17} />
            <span>Contents</span>
            <span className="muted small">
              {terms.length ? `${shown.length} of ${ARTICLES.length} match` : `${ARTICLES.length} articles`}
            </span>
            <Icon name="chevronDown" size={16} className={`kb-toc-chevron ${tocOpen ? "kb-flip" : ""}`} />
          </button>
          <div className="kb-toc-panel" id="kb-toc-list">
            <h2 className="kb-toc-title">
              Contents
              {terms.length ? (
                <span className="muted small">
                  {shown.length} of {ARTICLES.length} match
                </span>
              ) : null}
            </h2>
            {shown.length ? (
              <ol className="kb-toc-list">
                {shown.map((a) => (
                  <li key={a.id}>
                    <Link
                      to={`/kb#${a.id}`}
                      className={active === a.id ? "active" : undefined}
                      aria-current={active === a.id ? "location" : undefined}
                      onClick={() => setTocOpen(false)}
                    >
                      <span className={`kb-toc-icon kb-tone-${a.tone}`} aria-hidden>
                        <Icon name={a.icon} size={15} />
                      </span>
                      <span className="kb-toc-label">
                        <Hl text={a.title} re={re} />
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="muted small kb-toc-empty">No article matches.</p>
            )}
            <div className="kb-toc-foot small">
              <span className="muted">In the package:</span> <code>README.md</code>, <code>docs/DEPLOY_WINDOWS.md</code>, <code>docs/SERVER_SETUP.md</code>
            </div>
          </div>
        </nav>

        <div className="kb-articles">
          {shown.length ? null : (
            <Card>
              <Empty>
                No article mentions “{query.trim()}”. Try a shorter word, or type <code>help</code> in the command bar to ask the AI Supervisor what the
                agents can do.
              </Empty>
              <button className="btn small" onClick={() => setQuery("")}>
                Clear the search
              </button>
            </Card>
          )}
          {shown.map((a) => (
            <article key={a.id} id={a.id} className={`kb-article kb-tone-${a.tone}`} tabIndex={-1}>
              <Card
                title={
                  <>
                    <Icon name={a.icon} size={18} className="card-icon" />
                    <Hl text={a.title} re={re} />
                  </>
                }
                actions={
                  <>
                    {a.id === "agents" && agentList?.length ? (
                      <button className="btn small" onClick={() => setOpen(allOpen ? new Set() : new Set(agentList.map((x) => x.name)))}>
                        {allOpen ? "Hide all capabilities" : "Show all capabilities"}
                      </button>
                    ) : null}
                    {a.link ? (
                      <Link className="small" to={a.link.to}>
                        {a.link.label}
                      </Link>
                    ) : null}
                  </>
                }
              >
                <div className="kb-body">
                  <p className="kb-lead">
                    <Hl text={a.summary} re={re} />
                  </p>
                  {a.blocks.map(renderBlock)}
                </div>
                {/* Shown only when the contents sit above the articles (tablet and phone). */}
                <div className="kb-article-foot">
                  <button
                    className="btn ghost small"
                    onClick={() => {
                      window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
                      document.querySelector<HTMLElement>(".kb-toc-toggle")?.focus({ preventScroll: true });
                    }}
                  >
                    Back to contents
                  </button>
                </div>
              </Card>
            </article>
          ))}
        </div>
      </div>
    </div>
  );
}
