// The seven real agents as cards (owner request 1): colour and filled glyph, short name, keywords for capabilities that
// exist, status, current work, the 24-hour success rate (labelled as such) and a link to the agent's page.
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import type { Agent, User } from "../api";
import { AgentIcon, agentKeywords, agentStyle } from "../agentStyle";
import PanelError from "./PanelError";
import { canSeeSensitive } from "../work";
import { agentActivity, agentStatus, specialists, successRate } from "./model";

export function AgentCard({ agent, user, workerAlive, now }: { agent: Agent; user: User; workerAlive: boolean | null; now: number }) {
  const style = agentStyle(agent.name, agent.title);
  const status = agentStatus(agent);
  const activity = agentActivity(agent, workerAlive, now);
  const { rate, runs, failed } = successRate(agent);
  const keywords = agentKeywords(agent);
  const locked = agent.sensitive && !canSeeSensitive(user);
  const reasonId = `agent-locked-${agent.name}`;
  const pct = rate === null ? null : Math.round(rate * 100);
  return (
    <li className="agent-card" style={{ "--agent": style.color } as CSSProperties}>
      <div className="agent-card-head">
        <AgentIcon name={agent.name} size={28} />
        <h3 title={agent.title}>{style.short}</h3>
      </div>
      <p className="agent-keywords" title={keywords.join(" • ")}>
        {keywords.map((k, i) => (
          <span key={k}>
            {k}
            {i < keywords.length - 1 ? " •" : ""}
          </span>
        ))}
      </p>
      <p className={`agent-status tone-text-${status.tone}`}>
        <span className={`dot dot-${status.tone}`} aria-hidden /> {status.text}
      </p>
      <p className={`agent-activity ${activity.tone ? `tone-text-${activity.tone}` : ""}`}>{activity.text}</p>
      <div className="agent-rate" title={pct === null ? undefined : `${runs - failed} of ${runs} task${runs === 1 ? "" : "s"} created in the last 24 hours succeeded`}>
        <span className="agent-rate-label">
          {pct === null ? (
            "No runs in 24 h"
          ) : (
            <>
              <span>24 h success rate</span>
              <strong className="tabular">{pct}%</strong>
              <span className="sr-only">
                , {runs - failed} of {runs} succeeded
              </span>
            </>
          )}
        </span>
        <div className="agent-bar" aria-hidden>
          {pct !== null ? (
            <>
              <span className="agent-bar-ok" style={{ width: `${pct}%` }} />
              {failed ? <span className="agent-bar-failed" style={{ width: `${100 - pct}%` }} /> : null}
            </>
          ) : null}
        </div>
      </div>
      {locked ? (
        <>
          <button type="button" className="agent-open" disabled aria-describedby={reasonId}>
            Open
          </button>
          <span className="agent-locked" id={reasonId}>
            Needs sensitive-data access
          </span>
        </>
      ) : (
        <Link className="agent-open" to={style.route} aria-label={`Open ${style.short}`}>
          Open
        </Link>
      )}
    </li>
  );
}

export default function AgentCards({
  agents,
  error,
  user,
  workerAlive,
  now,
}: {
  agents: Agent[] | null;
  error: string | null;
  user: User;
  workerAlive: boolean | null;
  now: number;
}) {
  const list = agents ? specialists(agents) : [];
  return (
    <section className="dash-agents" aria-labelledby="dash-agents-title">
      <div className="dash-section-head">
        <div>
          <h2 id="dash-agents-title">AI Agents</h2>
          <p className="dash-section-sub">Specialized agents working together to grow and protect your business.</p>
        </div>
        <Link className="btn outline dash-manage" to="/agents">
          <svg width={15} height={15} viewBox="0 0 24 24" aria-hidden>
            <path d="M7 17 17 7M9 7h8v8" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Manage Agents
        </Link>
      </div>
      {/* With cards on screen, they are the last good answer: say they are not current. */}
      <PanelError error={error && agents ? `Not updated: ${error}` : error} />
      {agents ? (
        list.length ? (
          <ul className="agent-row" aria-labelledby="dash-agents-title">
            {list.map((a) => (
              <AgentCard key={a.name} agent={a} user={user} workerAlive={workerAlive} now={now} />
            ))}
          </ul>
        ) : (
          <p className="empty">No agents are installed.</p>
        )
      ) : !error ? (
        <p className="empty">Loading agents…</p>
      ) : null}
    </section>
  );
}
