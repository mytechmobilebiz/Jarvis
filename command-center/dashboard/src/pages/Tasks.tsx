import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Task } from "../api";
import { Card, Empty, ErrorNote, Pill, RiskBadge, Time } from "../components";
import { usePoll } from "../hooks";
import { TaskDetail } from "../work";

const STATUSES = ["", "queued", "running", "awaiting_approval", "succeeded", "failed", "rejected", "cancelled"];

export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const [agent, setAgent] = useState("");
  const query = new URLSearchParams({ limit: "100" });
  if (status) query.set("status", status);
  if (agent) query.set("agent", agent);
  const list = usePoll(() => api.get<{ tasks: Task[]; total: number }>(`/api/tasks?${query}`), 5_000, [status, agent]);
  const openId = params.get("id") ? Number(params.get("id")) : null;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Tasks</h1>
          <p className="muted">Every unit of agent work: what ran, who asked for it, what it found.</p>
        </div>
        <div className="filters">
          <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s ? s.replace(/_/g, " ") : "all statuses"}
              </option>
            ))}
          </select>
          <input aria-label="Agent filter" placeholder="agent" value={agent} onChange={(e) => setAgent(e.target.value.trim())} />
        </div>
      </header>
      <Card title={`${list.data?.total ?? 0} task(s)`}>
        <ErrorNote error={list.error} />
        {list.data?.tasks.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>#</th>
                <th>Action</th>
                <th>Status</th>
                <th>Risk</th>
                <th>Source</th>
                <th>Created</th>
                <th>Summary</th>
              </tr>
            </thead>
            <tbody>
              {list.data.tasks.map((t) => (
                <tr key={t.id} className="clickable" onClick={() => setParams({ id: String(t.id) })}>
                  <td>{t.id}</td>
                  <td>
                    <code>
                      {t.agent}.{t.capability}
                    </code>
                  </td>
                  <td>
                    <Pill value={t.status} />
                  </td>
                  <td>
                    <RiskBadge risk={t.risk} />
                  </td>
                  <td className="small">{t.source}</td>
                  <td>
                    <Time iso={t.created_at} />
                  </td>
                  <td className="truncate small">{t.error || t.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No tasks match.</Empty>
        )}
      </Card>
      {openId ? <TaskDetail id={openId} onClose={() => setParams({})} /> : null}
    </div>
  );
}
