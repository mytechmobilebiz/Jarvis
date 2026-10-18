import { useState } from "react";
import { api, type Agent, type Capability } from "../api";
import { useAuth } from "../App";
import { Card, ErrorNote, Pill, RiskBadge, Time } from "../components";
import { usePoll } from "../hooks";
import { RunModal } from "../work";

const INTERNAL = new Set(["supervisor.plan_request", "supervisor.summarize_request"]);

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

  const all = agents.data?.agents ?? [];
  const supervisor = all.find((a) => a.role === "supervisor");
  const specialists = all.filter((a) => a.role !== "supervisor");

  function card(agent: Agent, number?: number) {
    const stats = agent.stats_24h;
    return (
      <Card
        key={agent.name}
        className={agent.enabled ? "" : "disabled"}
        title={
          <>
            {number ? <span className="agent-number">{number}</span> : null}
            {agent.title} <Pill value={agent.enabled ? "up" : "disabled"} label={agent.enabled ? "enabled" : "disabled"} />
            {agent.entitled === false ? <Pill value="disabled" label="not in your edition" /> : null}
            {agent.sensitive ? <Pill value="warning" label="sensitive data" /> : null}
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
        <p className="muted">{agent.description}</p>
        <div className="small muted">
          24 h: {stats.succeeded ?? 0} succeeded · {stats.failed ?? 0} failed · {stats.awaiting_approval ?? 0} awaiting approval · last run{" "}
          <Time iso={agent.last_run_at} />
        </div>
        {agent.integrations.length ? <div className="small muted">Integrations: {agent.integrations.join(", ")}</div> : null}
        {agent.capabilities.length ? null : <p className="small muted">No capabilities in this build yet.</p>}
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
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Agents</h1>
          <p className="muted">The AI Supervisor and the seven agents. Risk levels decide what needs your approval; agents cannot change that themselves.</p>
        </div>
      </header>
      <ErrorNote error={agents.error ?? error} />
      {supervisor ? (
        <section className="agent-section">
          <h2>Coordinator</h2>
          <p className="muted small">The AI Supervisor is not one of the seven agents: it plans your requests and hands the work to them.</p>
          <div className="agent-grid single">{card(supervisor)}</div>
        </section>
      ) : null}
      <section className="agent-section">
        <h2>The seven agents</h2>
        <div className="agent-grid">{specialists.map((a, i) => card(a, i + 1))}</div>
      </section>
      {running ? <RunModal agent={running.agent} cap={running.cap} onClose={() => setRunning(null)} /> : null}
    </div>
  );
}
