import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Agent, type Capability, type Task } from "../api";
import { useAuth } from "../App";
import { Card, ErrorNote, Modal, Pill, RiskBadge, Time } from "../components";
import { usePoll } from "../hooks";

const INTERNAL = new Set(["supervisor.plan_request", "supervisor.summarize_request"]);

function defaultsFor(cap: Capability): string {
  const out: Record<string, unknown> = {};
  for (const [key, prop] of Object.entries(cap.params_schema.properties ?? {})) {
    if (prop.default !== undefined) out[key] = prop.default;
    else if (cap.params_schema.required?.includes(key)) out[key] = prop.type === "integer" || prop.type === "number" ? 0 : "";
  }
  return JSON.stringify(out, null, 2);
}

function RunModal({ agent, cap, onClose }: { agent: Agent; cap: Capability; onClose: () => void }) {
  const [params, setParams] = useState(defaultsFor(cap));
  const [error, setError] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(params || "{}");
    } catch {
      setError("Parameters must be valid JSON.");
      return;
    }
    try {
      const r = await api.post<{ task: Task }>(`/api/agents/${agent.name}/tasks`, { capability: cap.name, params: parsed });
      setTask(r.task);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const props = Object.entries(cap.params_schema.properties ?? {});
  return (
    <Modal title={`${agent.title}: ${cap.name}`} onClose={onClose}>
      <p>{cap.description}</p>
      <p>
        <RiskBadge risk={cap.risk} />
      </p>
      {props.length ? (
        <ul className="params-help">
          {props.map(([k, p]) => (
            <li key={k}>
              <code>{k}</code> {p.type ? <span className="muted">({p.type})</span> : null} {p.description}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No parameters.</p>
      )}
      {task ? (
        <div className="alert alert-good">
          Task #{task.id} created — {task.status === "awaiting_approval" ? "held for approval." : "queued."} <Link to={`/tasks?id=${task.id}`}>Open task</Link>
        </div>
      ) : (
        <form onSubmit={submit}>
          <label>
            Parameters (JSON)
            <textarea className="mono" rows={6} value={params} onChange={(e) => setParams(e.target.value)} />
          </label>
          <ErrorNote error={error} />
          <button className="btn primary" type="submit">
            {cap.requires_approval ? "Request (needs approval)" : "Run now"}
          </button>
        </form>
      )}
    </Modal>
  );
}

export default function Agents() {
  const { canOperate } = useAuth();
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 15_000);
  const [running, setRunning] = useState<{ agent: Agent; cap: Capability } | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function toggle(agent: Agent) {
    setError(null);
    try {
      await api.patch(`/api/agents/${agent.name}`, { enabled: !agent.enabled });
      await agents.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Agents</h1>
          <p className="muted">The supervisor and its specialists. Risk levels decide what needs your approval; agents cannot change that themselves.</p>
        </div>
      </header>
      <ErrorNote error={agents.error ?? error} />
      <div className="agent-grid">
        {(agents.data?.agents ?? []).map((agent) => {
          const stats = agent.stats_24h;
          return (
            <Card
              key={agent.name}
              className={agent.enabled ? "" : "disabled"}
              title={
                <>
                  {agent.title} <Pill value={agent.enabled ? "up" : "disabled"} label={agent.enabled ? "enabled" : "disabled"} />
                  {agent.entitled === false ? <Pill value="disabled" label="not in your edition" /> : null}
                  {agent.sensitive ? <Pill value="warning" label="sensitive data" /> : null}
                </>
              }
              actions={
                canOperate && agent.name !== "supervisor" ? (
                  <button className="btn small" onClick={() => void toggle(agent)}>
                    {agent.enabled ? "Disable" : "Enable"}
                  </button>
                ) : null
              }
            >
              <p className="muted">{agent.description}</p>
              <div className="small muted">
                24 h: {stats.succeeded ?? 0} succeeded · {stats.failed ?? 0} failed · {stats.awaiting_approval ?? 0} awaiting approval · last run{" "}
                <Time iso={agent.last_run_at} />
              </div>
              {agent.integrations.length ? <div className="small muted">Integrations: {agent.integrations.join(", ")}</div> : null}
              <ul className="caps">
                {agent.capabilities
                  .filter((c) => !INTERNAL.has(`${agent.name}.${c.name}`))
                  .map((cap) => (
                    <li key={cap.name}>
                      <div>
                        <code>{cap.name}</code> <RiskBadge risk={cap.risk} />
                        <div className="small muted">{cap.description}</div>
                      </div>
                      {canOperate && agent.enabled && agent.entitled !== false ? (
                        <button className="btn small" onClick={() => setRunning({ agent, cap })}>
                          Run
                        </button>
                      ) : null}
                    </li>
                  ))}
              </ul>
            </Card>
          );
        })}
      </div>
      {running ? <RunModal agent={running.agent} cap={running.cap} onClose={() => setRunning(null)} /> : null}
    </div>
  );
}
