import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Approval, type Task, type TaskLog } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Findings, JsonView, Modal, Pill, RiskBadge, Time } from "../components";
import { usePoll } from "../hooks";

const STATUSES = ["", "queued", "running", "awaiting_approval", "succeeded", "failed", "rejected", "cancelled"];

function TaskDetail({ id, onClose }: { id: number; onClose: () => void }) {
  const { canOperate } = useAuth();
  const detail = usePoll(() => api.get<{ task: Task; logs: TaskLog[]; approval: Approval | null }>(`/api/tasks/${id}`), 3_000, [id]);
  const [error, setError] = useState<string | null>(null);
  const task = detail.data?.task;

  async function act(path: string) {
    setError(null);
    try {
      await api.post(path);
      await detail.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Modal title={`Task #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error ?? error} />
      {task ? (
        <>
          <p>
            <Pill value={task.status} /> <code>
              {task.agent}.{task.capability}
            </code>{" "}
            <RiskBadge risk={task.risk} />
          </p>
          <dl className="kv">
            <dt>Created</dt>
            <dd>
              <Time iso={task.created_at} /> by {task.created_by || "—"} ({task.source})
            </dd>
            <dt>Ran</dt>
            <dd>
              {task.started_at ? (
                <>
                  started <Time iso={task.started_at} />, finished <Time iso={task.finished_at} />, attempt {task.attempts}
                </>
              ) : (
                "not yet"
              )}
            </dd>
            {task.request_id ? (
              <>
                <dt>Supervisor request</dt>
                <dd>#{task.request_id}</dd>
              </>
            ) : null}
            {detail.data?.approval ? (
              <>
                <dt>Approval</dt>
                <dd>
                  <Pill value={detail.data.approval.status} /> {detail.data.approval.decided_by}
                </dd>
              </>
            ) : null}
          </dl>
          {canOperate ? (
            <div className="row-actions">
              {["queued", "awaiting_approval"].includes(task.status) ? (
                <button className="btn small" onClick={() => void act(`/api/tasks/${id}/cancel`)}>
                  Cancel
                </button>
              ) : null}
              {["failed", "cancelled"].includes(task.status) ? (
                <button className="btn small" onClick={() => void act(`/api/tasks/${id}/retry`)}>
                  Retry
                </button>
              ) : null}
            </div>
          ) : null}
          {task.summary ? <p>{task.summary}</p> : null}
          {task.error ? <p className="text-bad">{task.error}</p> : null}
          {task.result?.findings?.length ? (
            <>
              <h3>Findings</h3>
              <Findings findings={task.result.findings} />
            </>
          ) : null}
          <h3>Parameters</h3>
          <JsonView value={task.params} />
          {detail.data?.logs.length ? (
            <>
              <h3>Log</h3>
              <ul className="log">
                {detail.data.logs.map((l) => (
                  <li key={l.id} className={`log-${l.level}`}>
                    <span className="muted">{new Date(l.at).toLocaleTimeString()}</span> {l.message}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {task.result?.data ? (
            <details>
              <summary>Result data</summary>
              <JsonView value={task.result.data} />
            </details>
          ) : null}
        </>
      ) : (
        <p className="muted">Loading…</p>
      )}
    </Modal>
  );
}

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
