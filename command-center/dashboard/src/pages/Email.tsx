import { KeyboardEvent, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Agent, type Artifact, type Capability, type Integration, type OutboxRecord, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { ArtifactView, RunModal } from "../work";
import { deliveryCounts, deliverySummary, leadStatus, mockLabel, ticketStatus, type LeadWithDelivery, type TicketWithDelivery } from "./shared";
import "./Email.css";

const AGENT = "communications";
// The integration the communications agent sends through (backend: integrations/mocks.py MockEmail.name).
const EMAIL = "email";
// Most records the leads, tickets and outbox endpoints return in one call.
const LIMIT = 500;
const SHOWN = 25;

type TabKey = "inbox" | "leads" | "outbox" | "drafts";
type TaskPage = { tasks: Task[]; total: number };
type Attachment = { filename?: string; artifact_id?: number };

const TABS: { key: TabKey; label: string; icon: IconName }[] = [
  { key: "inbox", label: "Inbox", icon: "inbox" },
  { key: "leads", label: "Leads", icon: "users" },
  { key: "outbox", label: "Outbox", icon: "send" },
  { key: "drafts", label: "Drafts", icon: "file" },
];

// Ticket statuses (backend agents/support.py): open -> triaged -> drafted -> replied, or closed. Labels: pages/shared.tsx.
const ANSWERED = new Set(["replied", "closed"]);
const PRIORITY_TONE: Record<string, string> = { urgent: "failed", high: "pending", normal: "queued", low: "unknown" };

const DRAFT_KIND: Record<string, string> = { support_reply: "Reply draft", proposal: "Proposal" };

const GROUPS: { key: string; title: string; icon: IconName; caps: { name: string; label: string; icon: IconName }[] }[] = [
  {
    key: "support",
    title: "Client messages",
    icon: "inbox",
    caps: [
      { name: "support_overview", label: "Support overview", icon: "pulse" },
      { name: "triage_ticket", label: "Triage a message", icon: "search" },
      { name: "draft_reply", label: "Draft a reply", icon: "file" },
      { name: "send_reply", label: "Send a reply", icon: "send" },
    ],
  },
  {
    key: "sales",
    title: "Leads & proposals",
    icon: "users",
    caps: [
      { name: "lead_overview", label: "Lead overview", icon: "trending" },
      { name: "score_lead", label: "Score a lead", icon: "sparkles" },
      { name: "draft_proposal", label: "Draft a proposal", icon: "file" },
      { name: "send_proposal", label: "Send a proposal", icon: "send" },
    ],
  },
];

function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function plural(n: number, noun: string, many = `${noun}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? noun : many}`;
}

function newestFirst<T extends { id: number; created_at: string }>(rows: T[]): T[] {
  return [...rows].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime() || b.id - a.id);
}

function initials(name: string, email: string): string {
  const words = (name.trim() || email.split("@")[0] || "?").split(/[\s._-]+/).filter(Boolean);
  return ((words[0]?.[0] ?? "?") + (words.length > 1 ? words[words.length - 1][0] : "")).toUpperCase();
}

function attachmentsOf(e: OutboxRecord): Attachment[] {
  const list = e.payload?.attachments;
  return Array.isArray(list) ? (list as Attachment[]) : [];
}

function modeLabel(mode: Integration["mode"] | OutboxRecord["mode"]): string {
  return mode === "live" ? "Live" : mode === "mock" ? "Mock" : "Disabled";
}

/** The outbox tile's label: "sent" only for what a live integration delivered (C2-11). */
export function emailTileLabel(live: number, mock: number, mode: Integration["mode"] | undefined): string {
  if (live && mock) return "Emails sent or recorded";
  if (live) return "Emails sent";
  if (mock) return "Emails recorded (mock)";
  return mode === "live" ? "Emails sent" : mode === "mock" ? "Emails recorded (mock)" : "Emails";
}

/** Why the agent cannot run right now, or null when it can. */
function unavailable(agent: Agent | undefined): string | null {
  if (!agent) return null;
  if (!agent.entitled) return `${agent.title} is not included in this edition, so it cannot run.`;
  if (!agent.enabled) return `${agent.title} is switched off, so nothing it does will run.`;
  return null;
}

function ActionButton({ cap, label, icon, blocked, onRun }: { cap: Capability; label: string; icon: IconName; blocked: string | null; onRun?: () => void }) {
  const body = (
    <>
      <span className="em-action-icon" aria-hidden>
        <Icon name={icon} size={18} />
      </span>
      <span className="em-action-main">
        <span className="em-action-name">{label}</span>
        <span className="em-action-desc">{cap.description}</span>
        <span className="em-action-meta">
          <RiskBadge risk={cap.risk} />
          {cap.requires_approval ? (
            <span className="em-approval">
              <Icon name="lock" size={12} /> Needs approval
            </span>
          ) : null}
        </span>
      </span>
      {onRun ? <Icon name="chevronRight" size={16} className="em-action-go" /> : null}
    </>
  );
  const tone = cap.requires_approval ? "em-action-external" : "";
  if (!onRun) return <div className={`em-action em-action-static ${tone}`}>{body}</div>;
  return (
    <button type="button" className={`em-action ${tone}`} onClick={onRun} disabled={!!blocked} title={blocked ?? undefined}>
      {body}
    </button>
  );
}

/** Whether email actually leaves the system, from the email integration's mode. */
function DeliveryNote({ email, loaded, error }: { email: Integration | undefined; loaded: boolean; error: string | null }) {
  const mode = email?.mode;
  const headline = !email
    ? error
      ? "Could not load the email integration's status."
      : loaded
        ? "This Command Center does not report an email integration, so no email can be sent."
        : "Checking the email integration…"
    : mode === "live"
      ? "Email is live: approved replies and proposals are delivered to recipients."
      : mode === "mock"
        ? "Email is a mock in this build: approved emails are recorded in the Outbox and never leave the system."
        : "Email is disabled: nothing can be sent, even after approval.";
  return (
    <section className={`em-delivery em-delivery-${mode ?? "unknown"}`} aria-label="Email delivery status">
      <span className="em-delivery-icon" aria-hidden>
        <Icon name="mail" size={20} />
      </span>
      <div className="em-delivery-main">
        <div className="em-delivery-title">
          <strong>{email?.title ?? "Email delivery"}</strong>
          {mode ? <Pill value={mode} label={`${modeLabel(mode)} mode`} /> : null}
        </div>
        <p className="em-delivery-text">{headline}</p>
        {email && mode !== "live" && email.live_requirements ? (
          <p className="em-delivery-req">
            <span className="em-delivery-req-label">To go live:</span> {email.live_requirements}
          </p>
        ) : null}
        {email?.status?.last_error ? (
          <p className="em-delivery-req text-bad">
            Last error <Time iso={email.status.last_error_at} />: {email.status.last_error}
          </p>
        ) : email?.status?.last_ok_at ? (
          <p className="em-delivery-req">
            Last worked <Time iso={email.status.last_ok_at} />
          </p>
        ) : null}
      </div>
      <div className="em-delivery-links">
        <Link className="btn small" to="/outbox">
          <Icon name="send" size={14} /> Outbox
        </Link>
        <Link className="btn small" to="/integrations">
          <Icon name="plug" size={14} /> Integrations
        </Link>
      </div>
    </section>
  );
}

export default function Email() {
  const { user, canOperate } = useAuth();
  const [search, setSearch] = useSearchParams();
  const requested = search.get("tab");
  const tab: TabKey = TABS.some((t) => t.key === requested) ? (requested as TabKey) : "inbox";
  const tabRefs = useRef<Partial<Record<TabKey, HTMLButtonElement | null>>>({});

  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 30_000);
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 120_000);
  const tickets = usePoll(() => api.get<{ tickets: TicketWithDelivery[] }>(`/api/support/tickets?limit=${LIMIT}`), 15_000);
  const leads = usePoll(() => api.get<{ leads: LeadWithDelivery[] }>(`/api/leads?limit=${LIMIT}`), 15_000);
  const outbox = usePoll(() => api.get<{ entries: OutboxRecord[] }>(`/api/outbox?limit=${LIMIT}`), 15_000);
  const waiting = usePoll(() => api.get<TaskPage>(`/api/tasks?agent=${AGENT}&status=awaiting_approval&limit=50`), 10_000);
  const drafts = usePoll(() => api.get<{ artifacts: Artifact[] }>(`/api/artifacts?agent=${AGENT}&limit=20`), 15_000);

  const [running, setRunning] = useState<{ agent: Agent; cap: Capability } | null>(null);
  const [openArtifact, setOpenArtifact] = useState<number | null>(null);

  const agent = agents.data?.agents.find((a) => a.name === AGENT);
  const blocked = unavailable(agent);
  const email = integrations.data?.integrations.find((i) => i.name === EMAIL);

  const ticketList = useMemo(() => newestFirst(tickets.data?.tickets ?? []), [tickets.data]);
  const leadList = useMemo(() => newestFirst(leads.data?.leads ?? []), [leads.data]);
  const emails = useMemo(() => newestFirst((outbox.data?.entries ?? []).filter((e) => e.integration === EMAIL)), [outbox.data]);
  const draftList = useMemo(() => newestFirst(drafts.data?.artifacts ?? []), [drafts.data]);

  const open = ticketList.filter((t) => !ANSWERED.has(t.status));
  const urgentOpen = open.filter((t) => t.priority === "urgent").length;
  const highOpen = open.filter((t) => t.priority === "high").length;
  const draftedOpen = open.filter((t) => t.status === "drafted").length;
  const ticketsCapped = (tickets.data?.tickets.length ?? 0) >= LIMIT;

  const newLeads = leadList.filter((l) => l.status === "new").length;
  const qualified = leadList.filter((l) => l.status === "qualified").length;
  const hot = leadList.filter((l) => (l.score ?? 0) >= 75 && ["new", "qualified", "proposal_drafted"].includes(l.status)).length;
  const proposals = deliveryCounts(leadList, "proposal_sent");
  const replies = deliveryCounts(ticketList, "replied");
  const leadsCapped = (leads.data?.leads.length ?? 0) >= LIMIT;

  const mockSent = emails.filter((e) => e.mode === "mock").length;
  const liveSent = emails.length - mockSent;
  const outboxCapped = (outbox.data?.entries.length ?? 0) >= LIMIT;

  const waitingTotal = waiting.data?.total ?? 0;
  const waitingBreakdown = Object.entries(
    (waiting.data?.tasks ?? []).reduce<Record<string, number>>((acc, t) => {
      const noun = t.capability === "send_reply" ? "reply" : t.capability === "send_proposal" ? "proposal" : t.capability.replace(/_/g, " ");
      return { ...acc, [noun]: (acc[noun] ?? 0) + 1 };
    }, {}),
  ).map(([noun, n]) => plural(n, noun, noun === "reply" ? "replies" : `${noun}s`));

  const counts: Record<TabKey, number | null> = {
    inbox: tickets.data ? ticketList.length : null,
    leads: leads.data ? leadList.length : null,
    outbox: outbox.data ? emails.length : null,
    drafts: drafts.data ? draftList.length : null,
  };

  const capByName = new Map((agent?.capabilities ?? []).map((c) => [c.name, c]));
  const grouped = new Set(GROUPS.flatMap((g) => g.caps.map((c) => c.name)));
  const extra = (agent?.capabilities ?? []).filter((c) => !grouped.has(c.name));
  const groups = [
    ...GROUPS.map((g) => ({ ...g, items: g.caps.flatMap((c) => (capByName.has(c.name) ? [{ ...c, cap: capByName.get(c.name) as Capability }] : [])) })),
    ...(extra.length
      ? [{ key: "more", title: "More", icon: "sparkles" as IconName, caps: [], items: extra.map((cap) => ({ name: cap.name, label: capitalize(cap.name.replace(/_/g, " ")), icon: "sparkles" as IconName, cap })) }]
      : []),
  ];

  function selectTab(key: TabKey) {
    const next = new URLSearchParams(search);
    if (key === "inbox") next.delete("tab");
    else next.set("tab", key);
    setSearch(next, { replace: true });
  }

  // Arrow keys move between tabs (WAI-ARIA tabs pattern).
  function onTabKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = TABS.findIndex((t) => t.key === tab);
    const next =
      e.key === "ArrowRight" ? (i + 1) % TABS.length : e.key === "ArrowLeft" ? (i - 1 + TABS.length) % TABS.length : e.key === "Home" ? 0 : e.key === "End" ? TABS.length - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    selectTab(TABS[next].key);
    tabRefs.current[TABS[next].key]?.focus();
  }

  function refreshWork() {
    void tickets.refresh();
    void leads.refresh();
    void outbox.refresh();
    void waiting.refresh();
    void drafts.refresh();
  }

  const shownNote = (shown: number, total: number, noun: string, capped: boolean) =>
    total > shown ? `Newest ${shown} of ${capped ? `the latest ${total.toLocaleString()}` : total.toLocaleString()} ${noun}.` : `${plural(total, noun.replace(/s$/, ""), noun)}.`;

  return (
    <div className="page em-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Communications</div>
          <h1>Email</h1>
          <p className="muted">
            Client messages, leads and outgoing email for the Email &amp; Customer Communications agent. Replies and proposals are drafted for your review and
            are only sent after approval.
          </p>
        </div>
        <div className="row-actions">
          <Link className="btn" to="/support">
            <Icon name="inbox" size={16} /> Client support
          </Link>
          <Link className="btn" to="/leads">
            <Icon name="users" size={16} /> Leads
          </Link>
        </div>
      </header>

      <DeliveryNote email={email} loaded={!!integrations.data} error={integrations.error} />

      <ErrorNote error={agents.error} />
      {agents.data && !agent ? (
        <div className="alert alert-bad" role="alert">
          The Email &amp; Customer Communications agent is not registered on this Command Center.
        </div>
      ) : null}
      {blocked ? (
        <div className={`alert em-alert ${agent?.entitled ? "em-alert-warn" : "alert-bad"}`} role="status">
          <Icon name="alert" size={18} className="em-alert-icon" />
          <div>
            <strong>{agent?.entitled ? "Email & Customer Communications is switched off." : "Email & Customer Communications is not in this edition."}</strong>{" "}
            New messages and leads are stored but not triaged, scored or answered until it runs again.{" "}
            {user.role === "admin" ? <Link to="/agents">Open AI Agents</Link> : <span>Ask an admin to change this on the AI Agents page.</span>}
          </div>
        </div>
      ) : null}

      <div className="tiles cols-4">
        <Tile
          icon="inbox"
          tone={urgentOpen ? "bad" : highOpen ? "warn" : "blue"}
          label="Client messages"
          to="/support"
          hint={
            tickets.data ? (
              open.length ? (
                <>
                  <span className={urgentOpen ? "text-bad" : ""}>{urgentOpen} urgent</span> · <span className={highOpen ? "text-warn" : ""}>{highOpen} high</span> ·{" "}
                  {plural(draftedOpen, "reply", "replies")} drafted
                </>
              ) : ticketList.length ? (
                replies.mock || replies.unknown ? (
                  `No open messages · ${deliverySummary(replies, "replied", "")}`
                ) : (
                  "No open messages: each one was replied to or closed"
                )
              ) : (
                "No client messages yet"
              )
            ) : tickets.error ? (
              "Could not load client messages"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{tickets.data ? open.length.toLocaleString() : "—"}</span>
          {tickets.data ? <span className="tile-of">open of {ticketsCapped ? `the latest ${LIMIT}` : ticketList.length.toLocaleString()}</span> : null}
        </Tile>

        <Tile
          icon="users"
          tone="purple"
          label="New leads"
          to="/leads"
          hint={
            leads.data ? (
              leadList.length ? (
                <>
                  {qualified} qualified · {hot} hot (score 75+) · {deliverySummary(proposals, proposals.live === 1 ? "proposal sent" : "proposals sent", "no proposal sent")}
                </>
              ) : (
                "No leads yet"
              )
            ) : leads.error ? (
              "Could not load leads"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{leads.data ? newLeads.toLocaleString() : "—"}</span>
          {leads.data ? <span className="tile-of">of {leadsCapped ? `the latest ${LIMIT}` : leadList.length.toLocaleString()}</span> : null}
        </Tile>

        <Tile
          icon="send"
          tone={email?.mode === "live" ? "green" : email?.mode === "mock" ? "cyan" : "muted"}
          label={emailTileLabel(liveSent, mockSent, email?.mode)}
          to="/outbox"
          hint={
            outbox.data ? (
              <>
                {liveSent && mockSent
                  ? `${liveSent} delivered · ${mockSent} recorded only (mock)`
                  : mockSent
                    ? "Recorded in the Outbox, not delivered"
                    : liveSent
                      ? "Delivered after approval"
                      : "Nothing sent yet"}
                {emails[0] ? (
                  <>
                    {" "}
                    · last <Time iso={emails[0].created_at} />
                  </>
                ) : null}
                {outboxCapped ? ` · in the latest ${LIMIT} outbox entries` : null}
              </>
            ) : outbox.error ? (
              "Could not load the outbox"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{outbox.data ? emails.length.toLocaleString() : "—"}</span>
          {email ? <Pill value={email.mode} label={`${modeLabel(email.mode)} mode`} /> : null}
        </Tile>

        <Tile
          icon="approvals"
          tone={!waiting.data ? "muted" : waitingTotal ? "warn" : "green"}
          label="Waiting for approval"
          to="/approvals"
          hint={
            waiting.data ? (
              waitingTotal ? (
                <>
                  {waitingBreakdown.join(" · ")}
                  {waitingTotal > waiting.data.tasks.length ? ` · ${waitingTotal - waiting.data.tasks.length} more` : ""}
                </>
              ) : (
                "Nothing from this agent is waiting for you"
              )
            ) : waiting.error ? (
              "Could not load tasks"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{waiting.data ? waitingTotal.toLocaleString() : "—"}</span>
          {waiting.data ? <span className="tile-of">{waitingTotal === 1 ? "task" : "tasks"}</span> : null}
        </Tile>
      </div>

      <Card
        className="em-mailbox"
        title={
          <>
            <Icon name="mail" size={18} className="card-icon em-card-icon" /> Mailbox
          </>
        }
        actions={
          <div className="tabs em-tabs" role="tablist" aria-label="Mailbox" onKeyDown={onTabKey}>
            {TABS.map((t) => (
              <button
                key={t.key}
                ref={(el) => {
                  tabRefs.current[t.key] = el;
                }}
                type="button"
                role="tab"
                id={`em-tab-${t.key}`}
                aria-selected={tab === t.key}
                aria-controls={`em-panel-${t.key}`}
                tabIndex={tab === t.key ? 0 : -1}
                className={tab === t.key ? "active" : ""}
                onClick={() => selectTab(t.key)}
              >
                <Icon name={t.icon} size={15} className="em-tab-icon" />
                <span>{t.label}</span>
                {counts[t.key] !== null ? <span className="em-tab-count tabular">{counts[t.key]}</span> : null}
              </button>
            ))}
          </div>
        }
      >
        <div className="em-panel" role="tabpanel" id={`em-panel-${tab}`} aria-labelledby={`em-tab-${tab}`} tabIndex={0}>
          {tab === "inbox" ? (
            <>
              <ErrorNote error={tickets.error} />
              {ticketList.length ? (
                <ul className="em-rows">
                  {ticketList.slice(0, SHOWN).map((t) => {
                    const [statusTone, statusLabel] = ticketStatus(t);
                    return (
                      <li key={t.id}>
                        <Link className="em-row" to="/support">
                          <span className="em-avatar" aria-hidden>
                            {initials(t.customer_name, t.customer_email)}
                          </span>
                          <span className="em-row-main">
                            <span className="em-row-top">
                              <span className="em-row-name">{t.customer_name || t.customer_email || "Unknown sender"}</span>
                              {t.customer_name && t.customer_email ? <span className="em-row-email">{t.customer_email}</span> : null}
                            </span>
                            <span className="em-row-line">
                              <span className="em-row-subject">{t.subject}</span>
                              {t.body ? <span className="em-row-snippet"> — {t.body.replace(/\s+/g, " ").trim()}</span> : null}
                            </span>
                            <span className="em-row-tags">
                              {t.priority ? <Pill value={PRIORITY_TONE[t.priority] ?? "unknown"} label={`${capitalize(t.priority)} priority`} /> : null}
                              {t.category ? <span className="em-tag">{t.category}</span> : null}
                              {t.sentiment ? <span className="em-tag">{t.sentiment} sentiment</span> : null}
                              {!t.category && !t.sentiment && t.status === "open" ? <span className="em-tag">waiting for triage</span> : null}
                            </span>
                          </span>
                          <span className="em-row-side">
                            <Pill value={statusTone} label={statusLabel} />
                            <Time iso={t.created_at} />
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              ) : tickets.data ? (
                <Empty>No client messages yet. Messages logged on Client support or sent in by the n8n support workflow appear here.</Empty>
              ) : (
                <Empty>Loading…</Empty>
              )}
              {ticketList.length ? (
                <p className="em-foot">
                  {shownNote(Math.min(SHOWN, ticketList.length), ticketList.length, "client messages", ticketsCapped)} Each one is triaged and a reply drafted
                  automatically; open <Link to="/support">Client support</Link> to log a message or send a reply.
                </p>
              ) : null}
            </>
          ) : null}

          {tab === "leads" ? (
            <>
              <ErrorNote error={leads.error} />
              {leadList.length ? (
                <ul className="em-rows">
                  {leadList.slice(0, SHOWN).map((l) => {
                    const [statusTone, statusLabel] = leadStatus(l);
                    const scoreTone = l.score === null ? "none" : l.score >= 75 ? "good" : l.score >= 50 ? "warn" : "low";
                    return (
                      <li key={l.id}>
                        <Link className="em-row" to="/leads">
                          <span className={`em-score em-score-${scoreTone}`} title={l.score === null ? "Not scored yet" : `Lead score ${l.score} of 100`}>
                            <span className="em-score-value tabular">{l.score ?? "…"}</span>
                            <span className="em-score-label">{l.score === null ? "scoring" : "score"}</span>
                          </span>
                          <span className="em-row-main">
                            <span className="em-row-top">
                              <span className="em-row-name">{l.company || l.name}</span>
                              {l.company ? <span className="em-row-email">{l.name}</span> : null}
                            </span>
                            <span className="em-row-line">
                              <span className="em-row-subject">{l.email || "no email"}</span>
                              {l.service_interest ? <span className="em-row-snippet"> — {l.service_interest}</span> : null}
                            </span>
                            <span className="em-row-tags">
                              <span className="em-tag">via {l.source}</span>
                              {l.proposal_artifact_id ? <span className="em-tag">proposal #{l.proposal_artifact_id}</span> : null}
                            </span>
                          </span>
                          <span className="em-row-side">
                            <Pill value={statusTone} label={statusLabel} />
                            <Time iso={l.created_at} />
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              ) : leads.data ? (
                <Empty>No leads yet. Leads added on the Leads page or by the n8n lead-intake workflow appear here.</Empty>
              ) : (
                <Empty>Loading…</Empty>
              )}
              {leadList.length ? (
                <p className="em-foot">
                  {shownNote(Math.min(SHOWN, leadList.length), leadList.length, "leads", leadsCapped)} New leads are scored automatically; open{" "}
                  <Link to="/leads">Leads</Link> to draft or send a proposal.
                </p>
              ) : null}
            </>
          ) : null}

          {tab === "outbox" ? (
            <>
              <ErrorNote error={outbox.error} />
              {emails.length ? (
                <ul className="em-rows">
                  {emails.slice(0, SHOWN).map((e) => {
                    const files = attachmentsOf(e);
                    return (
                      <li key={e.id}>
                        <details className="em-mail">
                          <summary className="em-row">
                            <span className={`em-mail-icon em-mail-${e.mode}`} aria-hidden>
                              <Icon name="mail" size={16} />
                            </span>
                            <span className="em-row-main">
                              <span className="em-row-top">
                                <span className="em-row-name">{e.recipient || "No recipient"}</span>
                              </span>
                              <span className="em-row-line">
                                <span className="em-row-subject">{e.subject || "(no subject)"}</span>
                              </span>
                              <span className="em-row-tags">
                                <Pill value={e.mode} label={e.mode === "mock" ? mockLabel("email") : "Live: sent"} />
                                {e.result ? <span className="em-tag">{e.result}</span> : null}
                                {e.approved_by ? <span className="em-tag em-tag-wrap">approved by {e.approved_by}</span> : null}
                                {files.length ? <span className="em-tag">{plural(files.length, "attachment")}</span> : null}
                              </span>
                            </span>
                            <span className="em-row-side">
                              <Time iso={e.created_at} />
                              <Icon name="chevronDown" size={16} className="em-chevron" />
                            </span>
                          </summary>
                          <div className="em-mail-body">
                            <dl className="kv em-kv">
                              <dt>To</dt>
                              <dd>{e.recipient || "—"}</dd>
                              <dt>Subject</dt>
                              <dd>{e.subject || "—"}</dd>
                              <dt>Approved by</dt>
                              <dd>{e.approved_by || "—"}</dd>
                              <dt>Result</dt>
                              <dd>
                                {e.mode === "mock" ? mockLabel("email") : "Sent (live)"}: {e.result || "—"}
                              </dd>
                              {e.task_id ? (
                                <>
                                  <dt>Task</dt>
                                  <dd>
                                    <Link to={`/tasks?id=${e.task_id}`}>#{e.task_id}</Link>
                                  </dd>
                                </>
                              ) : null}
                            </dl>
                            {e.body ? <pre className="plain boxed em-pre">{e.body}</pre> : <p className="muted small">No message body was recorded.</p>}
                            {files.length ? (
                              <ul className="em-attachments">
                                {files.map((f, i) => (
                                  <li key={`${f.artifact_id ?? "file"}-${i}`}>
                                    <Icon name="file" size={14} />
                                    <span className="em-attachment-name">{f.filename || "attachment"}</span>
                                    {typeof f.artifact_id === "number" ? (
                                      <button type="button" className="btn small" onClick={() => setOpenArtifact(f.artifact_id as number)}>
                                        Open
                                      </button>
                                    ) : null}
                                  </li>
                                ))}
                              </ul>
                            ) : null}
                          </div>
                        </details>
                      </li>
                    );
                  })}
                </ul>
              ) : outbox.data ? (
                <Empty>No emails yet. A reply or proposal appears here once it has been approved and sent.</Empty>
              ) : (
                <Empty>Loading…</Empty>
              )}
              {emails.length ? (
                <p className="em-foot">
                  {shownNote(Math.min(SHOWN, emails.length), emails.length, "emails", false)}
                  {outboxCapped ? ` Counted in the latest ${LIMIT} outbox entries.` : ""}{" "}
                  {email?.mode === "mock"
                    ? "Email is a mock in this build: these were approved and recorded, but not delivered."
                    : email?.mode === "live"
                      ? "Each email was sent only after approval."
                      : ""}{" "}
                  Expand an entry to read it.
                </p>
              ) : null}
            </>
          ) : null}

          {tab === "drafts" ? (
            <>
              <ErrorNote error={drafts.error} />
              {draftList.length ? (
                <ul className="em-rows">
                  {draftList.map((a) => {
                    const ticketId = typeof a.data?.ticket_id === "number" ? a.data.ticket_id : null;
                    const leadId = typeof a.data?.lead_id === "number" ? a.data.lead_id : null;
                    const writer = typeof a.data?.writer === "string" ? a.data.writer : "";
                    return (
                      <li key={a.id}>
                        <button type="button" className="em-row" onClick={() => setOpenArtifact(a.id)}>
                          <span className={`em-draft-icon em-draft-${a.kind === "proposal" ? "proposal" : "reply"}`} aria-hidden>
                            <Icon name={a.kind === "proposal" ? "file" : "mail"} size={16} />
                          </span>
                          <span className="em-row-main">
                            <span className="em-row-top">
                              <span className="em-row-name">{a.title}</span>
                            </span>
                            <span className="em-row-line">
                              <span className="em-row-snippet">
                                {DRAFT_KIND[a.kind] ?? capitalize(a.kind.replace(/_/g, " "))}
                                {ticketId !== null ? ` · client message #${ticketId}` : ""}
                                {leadId !== null ? ` · lead #${leadId}` : ""}
                                {writer === "template" ? " · from a template" : writer ? ` · by ${writer}` : ""}
                              </span>
                            </span>
                          </span>
                          <span className="em-row-side">
                            <Pill value={a.status} label={a.status === "approved" ? "Reviewed" : undefined} />
                            <Time iso={a.created_at} />
                          </span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              ) : drafts.data ? (
                <Empty>No drafts yet. Reply and proposal drafts appear here for review.</Empty>
              ) : (
                <Empty>Loading…</Empty>
              )}
              {draftList.length ? (
                <p className="em-foot">
                  Latest {draftList.length} drafts from the Email &amp; Customer Communications agent. Marking a draft reviewed never sends it.
                </p>
              ) : null}
            </>
          ) : null}
        </div>
      </Card>

      <Card
        className="em-actions-card"
        title={
          <>
            <Icon name="play" size={18} className="card-icon em-card-icon" /> Quick actions
          </>
        }
        actions={
          <Link className="small" to="/agents">
            All agents
          </Link>
        }
      >
        {!canOperate ? (
          <p className="em-note">
            <Icon name="lock" size={14} /> Your account is view-only. These are the actions an operator or admin can run here.
          </p>
        ) : null}
        {blocked && canOperate ? (
          <p className="em-note em-note-warn">
            <Icon name="alert" size={14} /> {blocked}
          </p>
        ) : null}
        {groups.some((g) => g.items.length) ? (
          <div className="em-groups">
            {groups.map((g) =>
              g.items.length ? (
                <section key={g.key} className="em-group" aria-labelledby={`em-group-${g.key}`}>
                  <h3 id={`em-group-${g.key}`}>
                    <Icon name={g.icon} size={16} /> {g.title}
                  </h3>
                  <ul className="em-action-list">
                    {g.items.map((item) => (
                      <li key={item.name}>
                        <ActionButton
                          cap={item.cap}
                          label={item.label}
                          icon={item.icon}
                          blocked={blocked}
                          onRun={canOperate && agent ? () => setRunning({ agent, cap: item.cap }) : undefined}
                        />
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null,
            )}
          </div>
        ) : (
          <Empty>{agents.data ? "No actions available." : "Loading…"}</Empty>
        )}
        <p className="em-foot">
          Overviews, triage, scoring and drafts run straight away and never contact anyone. Sending a reply or proposal is requested here and waits on the
          Approvals page
          {email ? `; email is in ${modeLabel(email.mode).toLowerCase()} mode` : ""}.
        </p>
      </Card>

      {running ? (
        <RunModal
          agent={running.agent}
          cap={running.cap}
          onClose={() => {
            setRunning(null);
            refreshWork();
          }}
        />
      ) : null}
      {openArtifact ? <ArtifactView id={openArtifact} onClose={() => setOpenArtifact(null)} onChanged={() => void drafts.refresh()} /> : null}
    </div>
  );
}
