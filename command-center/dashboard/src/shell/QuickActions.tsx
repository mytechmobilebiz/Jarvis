// Sidebar Quick Actions (owner request 2): read-only checks and draft reports, which the task layer runs without approval.
// Each one reports the real task the API created, and is disabled with the reason when it cannot run.
import { FormEvent, ReactNode, useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";
import { api, type Agent, type Server, type Site, type SystemStatus, type Task, type User } from "../api";
import { ErrorNote, Modal } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { n8nEditorUrl } from "../status";

export type ActionKey = "scan" | "seo" | "servers" | "security_report" | "uptime_report";

export const ACTIONS: Record<ActionKey, { agent: string; capability: string; label: string }> = {
  scan: { agent: "monitoring", capability: "check_all_sites", label: "Scan all websites" },
  seo: { agent: "seo_content", capability: "audit_page", label: "Run SEO audit" },
  servers: { agent: "server_management", capability: "check_services", label: "Check servers" },
  security_report: { agent: "wordpress_security", capability: "security_report", label: "Security report" },
  uptime_report: { agent: "monitoring", capability: "uptime_report", label: "Uptime report" },
};

const AGENT_NAME: Record<string, string> = {
  monitoring: "Monitoring agent",
  seo_content: "SEO agent",
  server_management: "Server agent",
  wordpress_security: "WordPress agent",
};

interface Loaded<T> {
  data: T | null;
  error: string | null;
}

export interface QuickActionInputs {
  user: User;
  agents: Loaded<Agent[]>;
  sites: Loaded<Site[]>;
  servers: Loaded<Server[]>;
  /** Tasks that are queued or running now. */
  active: Loaded<Task[]>;
}

/** Servers a service check can run on: switched on, with a pinned (verified) host key. */
export function checkableServers(servers: Server[]): Server[] {
  return servers.filter((s) => s.enabled && s.host_key_pinned);
}

function loading<T>(x: Loaded<T>, what: string): string | null {
  if (x.data !== null) return null;
  return x.error ? `Cannot check ${what}: ${x.error}` : "Checking…";
}

function agentBlocker(agents: Loaded<Agent[]>, key: ActionKey): string | null {
  const wait = loading(agents, "the agents");
  if (wait) return wait;
  const { agent, capability } = ACTIONS[key];
  const a = agents.data!.find((x) => x.name === agent);
  const name = AGENT_NAME[agent] ?? agent;
  if (!a || !a.capabilities.some((c) => c.name === capability)) return `${name} not available`;
  if (!a.entitled) return `${name}: not in this edition`;
  if (!a.enabled) return `${name} is switched off`;
  return null;
}

function busyBlocker(active: Loaded<Task[]>, key: ActionKey): string | null {
  const wait = loading(active, "running tasks");
  if (wait) return wait;
  const { agent, capability } = ACTIONS[key];
  const t = active.data!.find((x) => x.agent === agent && x.capability === capability && (x.status === "queued" || x.status === "running"));
  return t ? `Already ${t.status}: task #${t.id}` : null;
}

function sitesBlocker(sites: Loaded<Site[]>): string | null {
  return loading(sites, "the websites") ?? (sites.data!.some((s) => s.enabled) ? null : "No websites being monitored");
}

/** Why each action cannot run now, or null when it can. */
export function quickActionBlockers({ user, agents, sites, servers, active }: QuickActionInputs): Record<ActionKey, string | null> {
  const out = {} as Record<ActionKey, string | null>;
  for (const key of Object.keys(ACTIONS) as ActionKey[]) {
    if (user.role === "viewer") {
      out[key] = "Viewers cannot start actions";
      continue;
    }
    let need: string | null = null;
    if (key === "scan" || key === "security_report" || key === "uptime_report") need = sitesBlocker(sites);
    if (key === "servers") {
      need =
        loading(servers, "the servers") ??
        (!servers.data!.length ? "No servers added" : checkableServers(servers.data!).length ? null : "No enabled server with a pinned host key");
    }
    out[key] = agentBlocker(agents, key) ?? need ?? busyBlocker(active, key);
  }
  return out;
}

const STATUS_WORDS: Record<string, string> = {
  queued: "queued",
  running: "running",
  awaiting_approval: "waiting for approval",
  succeeded: "finished",
  failed: "failed",
  rejected: "rejected",
  cancelled: "cancelled",
};

interface Outcome {
  label: string;
  pending?: boolean;
  tasks: { task: Task; target?: string }[];
  errors: string[];
  note?: ReactNode;
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

async function start(key: ActionKey, params: Record<string, unknown>): Promise<Task> {
  const { agent, capability } = ACTIONS[key];
  const r = await api.post<{ task: Task }>(`/api/agents/${agent}/tasks`, { capability, params });
  return r.task;
}

function QuickButton({ icon, label, reason, busy, onClick }: { icon: IconName; label: string; reason: string | null; busy: boolean; onClick: () => void }) {
  const id = useId();
  const blocked = Boolean(reason) || busy;
  return (
    <li>
      {/* aria-disabled, not disabled: the button stays focusable, so keyboard users reach the reason. */}
      <button
        type="button"
        className="qa-btn"
        aria-disabled={blocked || undefined}
        aria-describedby={reason ? id : undefined}
        title={reason ? `${label}: ${reason}` : label}
        onClick={() => !blocked && onClick()}
      >
        <Icon name={icon} size={17} strokeWidth={1.6} className="qa-icon" />
        <span className="nav-label">{busy ? "Starting…" : label}</span>
      </button>
      {reason ? (
        <span id={id} className="qa-reason nav-label">
          {reason}
        </span>
      ) : null}
    </li>
  );
}

function OutcomeView({ outcome, onDismiss }: { outcome: Outcome; onDismiss: () => void }) {
  if (outcome.pending) return <p className="qa-outcome-text">Starting {outcome.label.toLowerCase()}…</p>;
  return (
    <div className="qa-outcome">
      <div className="qa-outcome-text">
        <strong>{outcome.label}</strong>
        {outcome.tasks.map(({ task, target }) => (
          <div key={task.id}>
            <Link to={`/tasks?id=${task.id}`}>Task #{task.id}</Link> {STATUS_WORDS[task.status] ?? task.status}
            {target ? <span className="muted"> · {target}</span> : null}
          </div>
        ))}
        {outcome.errors.map((e, i) => (
          <div key={i} className="text-bad">
            Not started: {e}
          </div>
        ))}
        {outcome.tasks.length && outcome.note ? <div className="muted">{outcome.note}</div> : null}
      </div>
      <button type="button" className="btn ghost icon" onClick={onDismiss} aria-label="Dismiss the result">
        <Icon name="close" size={14} />
      </button>
    </div>
  );
}

function SeoAuditDialog({ sites, onClose, onRun }: { sites: Site[]; onClose: () => void; onRun: (url: string) => Promise<void> }) {
  const monitored = sites.filter((s) => s.enabled);
  const [choice, setChoice] = useState(monitored[0]?.url ?? "");
  const [other, setOther] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const url = (choice || other).trim();

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    let parsed: URL | null = null;
    try {
      parsed = new URL(url);
    } catch {
      /* reported below */
    }
    if (!parsed || !/^https?:$/.test(parsed.protocol)) {
      setError("Enter a full page address that starts with https:// or http://.");
      return;
    }
    setBusy(true);
    try {
      await onRun(url);
    } catch (err) {
      setError(message(err));
      setBusy(false);
    }
  }

  return (
    <Modal title="Run SEO audit" onClose={onClose}>
      <form onSubmit={submit}>
        <p className="muted">The SEO agent reads one public page and reports on its title, description, headings and links. Nothing on the site changes.</p>
        {monitored.length ? (
          <label>
            Page to audit
            <select value={choice} onChange={(e) => setChoice(e.target.value)}>
              {monitored.map((s) => (
                <option key={s.id} value={s.url}>
                  {s.name} ({s.url})
                </option>
              ))}
              <option value="">Another address…</option>
            </select>
          </label>
        ) : null}
        {!choice ? (
          <label>
            Page address
            <input type="url" inputMode="url" placeholder="https://example.com/services" value={other} onChange={(e) => setOther(e.target.value)} required autoFocus />
          </label>
        ) : null}
        <ErrorNote error={error} />
        <div className="row-actions">
          <button className="btn primary" type="submit" disabled={busy}>
            {busy ? "Starting…" : "Run SEO audit"}
          </button>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}

const REPORTS: { key: "security_report" | "uptime_report"; text: string }[] = [
  { key: "security_report", text: "WordPress agent: the security findings of every monitored website." },
  { key: "uptime_report", text: "Monitoring agent: the uptime and incidents of every monitored website." },
];

function ReportDialog({ blockers, onClose, onRun }: { blockers: Record<ActionKey, string | null>; onClose: () => void; onRun: (key: "security_report" | "uptime_report") => Promise<void> }) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  async function run(key: "security_report" | "uptime_report") {
    setError(null);
    setBusy(key);
    try {
      await onRun(key);
    } catch (err) {
      setError(message(err));
      setBusy(null);
    }
  }
  return (
    <Modal title="Generate a report" onClose={onClose}>
      <p className="muted">The report is saved to Drafts & reports when its task finishes. Nothing is sent or published.</p>
      <ul className="qa-reports">
        {REPORTS.map(({ key, text }) => (
          <li key={key}>
            <div>
              <strong>{ACTIONS[key].label}</strong>
              <div className="muted small">{text}</div>
              {blockers[key] ? <div className="text-warn small">{blockers[key]}</div> : null}
            </div>
            <button className="btn small" type="button" disabled={Boolean(blockers[key]) || busy !== null} onClick={() => void run(key)}>
              {busy === key ? "Starting…" : `Generate ${ACTIONS[key].label.toLowerCase()}`}
            </button>
          </li>
        ))}
      </ul>
      <ErrorNote error={error} />
    </Modal>
  );
}

/** The combined reason for "Generate report": blocked only when neither report can run. */
function reportBlocker(b: Record<ActionKey, string | null>): string | null {
  const s = b.security_report;
  const u = b.uptime_report;
  if (!s || !u) return null;
  return s === u ? s : `Security: ${s}. Uptime: ${u}`;
}

export default function QuickActions({ user, status }: { user: User; status: SystemStatus | null }) {
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents").then((r) => r.agents), 30_000);
  const sites = usePoll(() => api.get<{ sites: Site[] }>("/api/sites").then((r) => r.sites), 60_000);
  const servers = usePoll(() => api.get<{ servers: Server[] }>("/api/servers").then((r) => r.servers), 60_000);
  const active = usePoll(() => api.get<{ tasks: Task[] }>("/api/tasks?status=queued,running&limit=200").then((r) => r.tasks), 15_000);
  const [busy, setBusy] = useState<ActionKey | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [dialog, setDialog] = useState<"seo" | "report" | null>(null);
  const titleId = useId();
  const live = useRef<HTMLDivElement>(null);
  const blockers = quickActionBlockers({ user, agents, sites, servers, active });
  const editor = user.role === "admin" ? n8nEditorUrl(status) : null;
  const draftsNote = (
    <>
      Saved to <Link to="/drafts">Drafts & reports</Link> when it finishes.
    </>
  );

  /** Starts one task. From a dialog (`inDialog`), an error is thrown back for the dialog to show; otherwise it is shown here. */
  async function runOne(key: ActionKey, params: Record<string, unknown>, opts: { target?: string; note?: ReactNode; inDialog?: boolean } = {}) {
    const label = ACTIONS[key].label;
    setBusy(key);
    if (!opts.inDialog) setOutcome({ label, pending: true, tasks: [], errors: [] });
    try {
      const task = await start(key, params);
      setOutcome({ label, tasks: [{ task, target: opts.target }], errors: [], note: opts.note });
      setDialog(null);
    } catch (e) {
      if (opts.inDialog) throw e;
      setOutcome({ label, tasks: [], errors: [message(e)] });
    } finally {
      setBusy(null);
      void active.refresh();
    }
  }

  // In a short window the result lands below the fold of the scrolling sidebar: bring it into view.
  useEffect(() => {
    if (outcome) live.current?.scrollIntoView?.({ block: "nearest" });
  }, [outcome]);

  async function checkServers() {
    const targets = checkableServers(servers.data ?? []);
    setBusy("servers");
    setOutcome({ label: ACTIONS.servers.label, pending: true, tasks: [], errors: [] });
    const results = await Promise.allSettled(targets.map((s) => start("servers", { server_id: s.id })));
    const done: Outcome = { label: ACTIONS.servers.label, tasks: [], errors: [] };
    results.forEach((r, i) => {
      if (r.status === "fulfilled") done.tasks.push({ task: r.value, target: targets[i].name });
      else done.errors.push(`${targets[i].name}: ${message(r.reason)}`);
    });
    setOutcome(done);
    setBusy(null);
    void active.refresh();
  }

  return (
    <div className="quick-actions" role="group" aria-labelledby={titleId}>
      <div className="qa-title nav-label" id={titleId}>
        Quick Actions
      </div>
      <ul className="qa-list">
        <QuickButton icon="radar" label={ACTIONS.scan.label} reason={blockers.scan} busy={busy === "scan"} onClick={() => void runOne("scan", {})} />
        <QuickButton icon="search" label={ACTIONS.seo.label} reason={blockers.seo} busy={busy === "seo"} onClick={() => setDialog("seo")} />
        <QuickButton icon="serverCheck" label={ACTIONS.servers.label} reason={blockers.servers} busy={busy === "servers"} onClick={() => void checkServers()} />
        <QuickButton
          icon="report"
          label="Generate report"
          reason={reportBlocker(blockers)}
          busy={busy === "security_report" || busy === "uptime_report"}
          onClick={() => setDialog("report")}
        />
        {editor ? (
          <li>
            <a className="qa-btn" href={editor} target="_blank" rel="noopener noreferrer" title="Open the n8n editor (new tab)">
              <Icon name="external" size={17} strokeWidth={1.6} className="qa-icon" />
              <span className="nav-label">Open n8n</span>
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          </li>
        ) : null}
      </ul>
      <div className="qa-live" role="status" ref={live}>
        {outcome ? <OutcomeView outcome={outcome} onDismiss={() => setOutcome(null)} /> : null}
      </div>
      {/* In document.body: the sidebar is a stacking context (sticky, or transformed as the phone drawer) that would trap the dialog. */}
      {dialog === "seo"
        ? createPortal(<SeoAuditDialog sites={sites.data ?? []} onClose={() => setDialog(null)} onRun={(url) => runOne("seo", { url }, { target: url, inDialog: true })} />, document.body)
        : null}
      {dialog === "report"
        ? createPortal(<ReportDialog blockers={blockers} onClose={() => setDialog(null)} onRun={(key) => runOne(key, {}, { note: draftsNote, inDialog: true })} />, document.body)
        : null}
    </div>
  );
}
