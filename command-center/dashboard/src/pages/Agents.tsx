import { useState } from "react";
import { api, type Agent, type Capability } from "../api";
import { useAuth } from "../App";
import { Card, ErrorNote, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { RunModal } from "../work";
import "./Agents.css";

const INTERNAL = new Set(["supervisor.plan_request", "supervisor.summarize_request"]);

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

/** Sum of one status (or of every status) in the agents' last-24-hour task counts. */
function total(agents: Agent[], status?: string): number {
  return agents.reduce((n, a) => n + Object.entries(a.stats_24h ?? {}).reduce((m, [k, v]) => m + (!status || k === status ? v : 0), 0), 0);
}

export default function Agents() {
  const { canOperate } = useAuth();
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 15_000);
  const [running, setRunning] = useState<{ agent: Agent; cap: Capability } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function toggle(agent: Agent) {
    setError(null);
    setNote(null);
    // Switching an agent off also withdraws its changes that have not started, approved or not (0.5.0 API, C3-04).
    if (
      agent.enabled &&
      !window.confirm(`Switch off ${agent.title}? Its changes, sends and publishes that have not started yet, approved or not, are cancelled. Tasks already running finish.`)
    )
      return;
    try {
      const r = await api.patch<{ cancelled_tasks?: number[] }>(`/api/agents/${agent.name}`, { enabled: !agent.enabled });
      const cancelled = r?.cancelled_tasks ?? [];
      if (cancelled.length) setNote(`${agent.title} is off. Cancelled ${cancelled.length === 1 ? "task" : "tasks"} ${cancelled.map((id) => `#${id}`).join(", ")}.`);
      await agents.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const all = agents.data?.agents ?? [];
  const supervisor = all.find((a) => a.role === "supervisor");
  const specialists = all.filter((a) => a.role !== "supervisor");
  const loaded = !!agents.data;
  const active = specialists.filter((a) => a.enabled && a.entitled !== false).length;
  const switchedOff = specialists.filter((a) => !a.enabled).length;
  const notEntitled = specialists.filter((a) => a.entitled === false).length;
  const tasks24 = total(all);
  const succeeded24 = total(all, "succeeded");
  const inProgress = total(all, "queued") + total(all, "running");
  const awaiting = total(all, "awaiting_approval");
  // Held now, however old (0.5.0 API: agents[].active); older APIs give only the last 24 hours' count (C2-14).
  const hasActive = all.some((a) => a.active);
  const awaitingNow = hasActive ? all.reduce((n, a) => n + (a.active?.awaiting_approval ?? 0), 0) : null;
  const held = awaitingNow ?? awaiting;
  const failed = total(all, "failed");
  const failingAgents = all.filter((a) => (a.stats_24h?.failed ?? 0) > 0).length;
  const pending = agents.error ?? "…";

  function card(agent: Agent, number?: number) {
    const stats = agent.stats_24h;
    const caps = agent.capabilities.filter((c) => !INTERNAL.has(`${agent.name}.${c.name}`));
    // An agent with a long capability list takes a whole row and lists them in columns, so the cards beside it stay compact.
    const layout = agent.role === "supervisor" ? "ag-coordinator" : caps.length > 8 ? "ag-wide" : "";
    return (
      <Card
        key={agent.name}
        className={`ag-card ${layout} ${agent.enabled ? "" : "disabled"}`}
        title={
          <>
            <span className="ag-icon" aria-hidden>
              <Icon name={AGENT_ICON[agent.name] ?? "agents"} size={18} />
              {number ? <span className="ag-number">{number}</span> : null}
            </span>
            <span className="ag-title">{agent.title}</span>
          </>
        }
        actions={
          canOperate && agent.role !== "supervisor" ? (
            <button className="btn small" onClick={() => void toggle(agent)}>
              {agent.enabled ? "Disable" : "Enable"}
            </button>
          ) : null
        }
      >
        <div className="ag-pills">
          <Pill value={agent.enabled ? "up" : "disabled"} label={agent.enabled ? "enabled" : "disabled"} />
          {agent.entitled === false ? <Pill value="disabled" label="not in your edition" /> : null}
          {agent.sensitive ? <Pill value="warning" label="sensitive data" /> : null}
        </div>
        <p className="muted ag-desc">{agent.description}</p>
        <dl className="ag-stats">
          <div>
            <dt>Succeeded 24 h</dt>
            <dd className="tabular">{stats.succeeded ?? 0}</dd>
          </div>
          <div>
            <dt>Failed 24 h</dt>
            <dd className={`tabular ${stats.failed ? "text-bad" : ""}`}>{stats.failed ?? 0}</dd>
          </div>
          {agent.active ? (
            <div title="Held for approval now, however old">
              <dt>Awaiting approval now</dt>
              <dd className={`tabular ${agent.active.awaiting_approval ? "text-warn" : ""}`}>{agent.active.awaiting_approval}</dd>
            </div>
          ) : (
            <div>
              <dt>Awaiting approval 24 h</dt>
              <dd className={`tabular ${stats.awaiting_approval ? "text-warn" : ""}`}>{stats.awaiting_approval ?? 0}</dd>
            </div>
          )}
          <div>
            <dt>Last run</dt>
            <dd>
              <Time iso={agent.last_run_at} />
            </dd>
          </div>
        </dl>
        {agent.integrations.length ? (
          <div className="ag-integrations small">
            <span className="muted">Integrations:</span>
            {agent.integrations.map((i) => (
              <span key={i} className="ag-chip">
                {i}
              </span>
            ))}
          </div>
        ) : null}
        {agent.capabilities.length ? null : <p className="small muted">No capabilities in this build yet.</p>}
        <ul className="caps ag-caps">
          {caps.map((cap) => (
            <li key={cap.name}>
              <div className="ag-cap">
                <div className="ag-cap-name">
                  <code>{cap.name}</code> <RiskBadge risk={cap.risk} />
                </div>
                <div className="small muted">{cap.description}</div>
              </div>
              {canOperate && agent.enabled && agent.entitled !== false ? (
                <button className="btn small" onClick={() => setRunning({ agent, cap })}>
                  <Icon name="play" size={14} /> Run
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      </Card>
    );
  }

  return (
    <div className="page ag-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Automation</div>
          <h1>AI Agents</h1>
          <p className="muted">The AI Supervisor and the seven agents. Risk levels decide what needs your approval; agents cannot change that themselves.</p>
        </div>
      </header>
      <ErrorNote error={agents.error ?? error} />
      {note ? (
        <div className="alert alert-info" role="status">
          {note}
        </div>
      ) : null}

      <div className="tiles cols-4">
        <Tile
          icon="agents"
          tone="purple"
          label="Active agents"
          hint={
            loaded
              ? [switchedOff ? `${switchedOff} switched off` : "", notEntitled ? `${notEntitled} not in your edition` : ""].filter(Boolean).join(" · ") ||
                `All ${specialists.length} switched on · AI Supervisor ${supervisor?.enabled ? "on" : "off"}`
              : pending
          }
        >
          <span className="tile-value tabular">{loaded ? active : "—"}</span>
          {loaded ? <span className="tile-of">of {specialists.length}</span> : null}
        </Tile>
        <Tile
          icon="tasks"
          tone="blue"
          label="Tasks in 24 h"
          to="/tasks"
          hint={loaded ? `${succeeded24} succeeded · ${inProgress} queued or running` : pending}
        >
          <span className="tile-value tabular">{loaded ? tasks24.toLocaleString() : "—"}</span>
        </Tile>
        <Tile
          icon="approvals"
          tone={!loaded ? "muted" : held ? "warn" : "green"}
          label={awaitingNow === null ? "Awaiting approval, 24 h" : "Awaiting approval"}
          to="/approvals"
          hint={
            !loaded
              ? pending
              : awaitingNow === null
                ? awaiting
                  ? "Tasks from the last 24 h held for you; older ones are on Approvals"
                  : "Nothing from the last 24 h is held; older ones are on Approvals"
                : awaitingNow
                  ? `Held for you now, however old · ${awaiting} requested in the last 24 h`
                  : "Nothing is held for you"
          }
        >
          <span className="tile-value tabular">{loaded ? held : "—"}</span>
        </Tile>
        <Tile
          icon="alert"
          tone={!loaded ? "muted" : failed ? "bad" : "green"}
          label="Failed 24 h"
          to="/tasks"
          hint={loaded ? (failed ? `${failingAgents} agent${failingAgents === 1 ? "" : "s"} with failures` : "No failed tasks") : pending}
        >
          <span className="tile-value tabular">{loaded ? failed : "—"}</span>
        </Tile>
      </div>

      {supervisor ? (
        <section className="agent-section">
          <div className="ag-section-head">
            <h2>
              <Icon name="sparkles" size={18} className="card-icon" /> Coordinator
            </h2>
            <p className="muted small">The AI Supervisor is not one of the seven agents: it plans your requests and hands the work to them.</p>
          </div>
          <div className="agent-grid single">{card(supervisor)}</div>
        </section>
      ) : null}
      <section className="agent-section">
        <div className="ag-section-head">
          <h2>
            <Icon name="agents" size={18} className="card-icon" /> The seven agents
          </h2>
        </div>
        <div className="agent-grid ag-grid">{specialists.map((a, i) => card(a, i + 1))}</div>
      </section>
      {running ? <RunModal agent={running.agent} cap={running.cap} onClose={() => setRunning(null)} /> : null}
    </div>
  );
}
