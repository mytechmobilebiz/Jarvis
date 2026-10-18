import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Task } from "../api";
import { Card, Empty, ErrorNote, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { TaskDetail } from "../work";
import "./Tasks.css";

const STATUSES = ["", "queued", "running", "awaiting_approval", "succeeded", "failed", "rejected", "cancelled"];

/** Status groups counted for the tiles: one `limit=1` read each, using the list's `total`. */
const TOTALS = { active: "running,queued", awaiting: "awaiting_approval", failed: "failed", succeeded: "succeeded" } as const;
type Totals = Record<keyof typeof TOTALS, number>;

async function statusTotals(agent: string): Promise<Totals> {
  const keys = Object.keys(TOTALS) as (keyof typeof TOTALS)[];
  const totals = await Promise.all(
    keys.map((k) => {
      const q = new URLSearchParams({ status: TOTALS[k], limit: "1" });
      if (agent) q.set("agent", agent);
      return api.get<{ total: number }>(`/api/tasks?${q}`).then((r) => r.total);
    }),
  );
  return Object.fromEntries(keys.map((k, i) => [k, totals[i]])) as Totals;
}

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const [agent, setAgent] = useState("");
  const query = new URLSearchParams({ limit: "100" });
  if (status) query.set("status", status);
  if (agent) query.set("agent", agent);
  // The tile totals ride on the list's own poll; a failed count never hides the list.
  const list = usePoll(
    async () => {
      const [page, totals] = await Promise.all([api.get<{ tasks: Task[]; total: number }>(`/api/tasks?${query}`), statusTotals(agent).catch(() => null)]);
      return { ...page, totals };
    },
    5_000,
    [status, agent],
  );
  const openId = params.get("id") ? Number(params.get("id")) : null;
  const totals = list.data?.totals ?? null;
  const scope = agent ? (
    <>
      agent <code>{agent}</code>
    </>
  ) : (
    "all agents"
  );
  const value = (n: number | undefined) => <span className="tile-value tabular">{n == null ? "—" : n.toLocaleString()}</span>;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Tasks</h1>
          <p className="muted">Every unit of agent work: what ran, who asked for it, what it found.</p>
        </div>
      </header>

      <div className="tiles cols-4">
        <Tile icon="play" tone="blue" label="Running or queued" hint={<>Working now or waiting for a worker · {scope}</>}>
          {value(totals?.active)}
        </Tile>
        <Tile
          icon="approvals"
          tone={totals?.awaiting ? "warn" : "orange"}
          label="Awaiting approval"
          to="/approvals"
          hint={<>Held until an operator decides · {scope}</>}
        >
          {value(totals?.awaiting)}
        </Tile>
        <Tile icon="alert" tone={totals?.failed ? "bad" : "muted"} label="Failed" hint={<>All time · {scope}</>}>
          {value(totals?.failed)}
        </Tile>
        <Tile icon="check" tone="green" label="Succeeded" hint={<>All time · {scope}</>}>
          {value(totals?.succeeded)}
        </Tile>
      </div>

      <Card
        className="tk-card"
        title={
          <>
            <Icon name="tasks" size={18} className="card-icon" /> {list.data?.total ?? 0} task(s)
          </>
        }
        actions={
          <div className="filters tk-filters">
            <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s ? s.replace(/_/g, " ") : "all statuses"}
                </option>
              ))}
            </select>
            <span className="tk-search">
              <Icon name="search" size={16} className="tk-search-icon" />
              <input aria-label="Agent filter" placeholder="agent" value={agent} onChange={(e) => setAgent(e.target.value.trim())} />
            </span>
          </div>
        }
      >
        <ErrorNote error={list.error} />
        {list.data?.tasks.length ? (
          <div className="table-wrap">
            <table className="table tk-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Action</th>
                  <th>Status</th>
                  <th>Risk</th>
                  <th>Source</th>
                  <th>Created</th>
                  <th className="tk-summary">Summary</th>
                </tr>
              </thead>
              <tbody>
                {list.data.tasks.map((t) => (
                  <tr key={t.id} className="clickable" onClick={() => setParams({ id: String(t.id) })}>
                    <td className="tk-id tabular">{t.id}</td>
                    <td className="tk-action">
                      <code>
                        {t.agent}.<wbr />
                        {t.capability}
                      </code>
                    </td>
                    <td>
                      <Pill value={t.status} />
                    </td>
                    <td>
                      <RiskBadge risk={t.risk} />
                    </td>
                    <td className="small muted tk-nowrap">{t.source}</td>
                    <td className="small tk-nowrap">
                      <Time iso={t.created_at} />
                    </td>
                    <td className={`tk-summary small ${t.error ? "text-bad" : ""}`}>
                      <span className="tk-clip" title={t.error || t.summary}>
                        {t.error || t.summary}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No tasks match.</Empty>
        )}
      </Card>
      {openId ? <TaskDetail id={openId} onClose={() => setParams({})} /> : null}
    </div>
  );
}
