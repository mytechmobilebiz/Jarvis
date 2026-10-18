// Shared views for running capabilities and inspecting their tasks and drafts; used by several pages.
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Agent, type Approval, type Artifact, type Capability, type Task, type TaskLog } from "./api";
import { useAuth } from "./App";
import { ErrorNote, Findings, JsonView, Markdown, Modal, Pill, RiskBadge, Time } from "./components";
import { usePoll } from "./hooks";

export function defaultsFor(cap: Capability): string {
  const out: Record<string, unknown> = {};
  for (const [key, prop] of Object.entries(cap.params_schema.properties ?? {})) {
    if (prop.default !== undefined) out[key] = prop.default;
    else if (cap.params_schema.required?.includes(key)) out[key] = prop.type === "integer" || prop.type === "number" ? 0 : "";
  }
  return JSON.stringify(out, null, 2);
}

/** Form that queues one capability run (or requests it, when it needs approval). */
export function RunModal({ agent, cap, onClose }: { agent: Agent; cap: Capability; onClose: () => void }) {
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

/** A draft or report, with review buttons. Reviewing never publishes or sends. */
export function ArtifactView({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { canOperate } = useAuth();
  const detail = usePoll(() => api.get<{ artifact: Artifact }>(`/api/artifacts/${id}`), 0, [id]);
  const [error, setError] = useState<string | null>(null);
  const a = detail.data?.artifact;

  async function mark(status: string) {
    setError(null);
    try {
      await api.patch(`/api/artifacts/${id}`, { status });
      await detail.refresh();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Modal title={a?.title ?? `Draft #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error ?? error} />
      {a ? (
        <>
          <p className="muted small">
            {a.kind} by {a.agent} · <Time iso={a.created_at} /> · <Pill value={a.status} />
          </p>
          {canOperate ? (
            <div className="row-actions">
              <button className="btn small" onClick={() => void mark("approved")}>
                Mark reviewed
              </button>
              <button className="btn small" onClick={() => void mark("rejected")}>
                Reject
              </button>
              <button className="btn small" onClick={() => void mark("archived")}>
                Archive
              </button>
              <span className="muted small">Marking a draft reviewed does not publish or send it.</span>
            </div>
          ) : null}
          {a.content_type === "text/html" ? (
            // Rendered with no scripts, forms or same-origin access.
            <iframe className="html-preview" title={a.title} sandbox="" srcDoc={a.content ?? ""} />
          ) : a.content_type === "application/json" ? (
            <JsonView value={(() => { try { return JSON.parse(a.content ?? "null"); } catch { return a.content; } })()} />
          ) : (
            <Markdown text={a.content ?? ""} />
          )}
        </>
      ) : (
        <p className="muted">Loading…</p>
      )}
    </Modal>
  );
}

/** One task: status, approval, logs, findings, with cancel and retry. */
export function TaskDetail({ id, onClose }: { id: number; onClose: () => void }) {
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
