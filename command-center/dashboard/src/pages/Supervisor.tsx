import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type SupervisorRequest, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Findings, Pill, RiskBadge, Time } from "../components";
import { usePoll } from "../hooks";

const EXAMPLES = [
  "Check all websites",
  "Run a security scan on every site",
  "Is example.com up?",
  "Give me the daily briefing",
  "Is the system healthy?",
  "help",
];

const ACTIVE = new Set(["planning", "running"]);

function RequestDetail({ id }: { id: number }) {
  const detail = usePoll(() => api.get<{ request: SupervisorRequest; tasks: Task[] }>(`/api/supervisor/requests/${id}`), 2_000, [id]);
  const req = detail.data?.request;
  const tasks = (detail.data?.tasks ?? []).filter((t) => !(t.agent === "supervisor" && ["plan_request", "summarize_request"].includes(t.capability)));
  if (detail.error) return <ErrorNote error={detail.error} />;
  if (!req) return <p className="muted">Loading…</p>;
  return (
    <div className="request-detail">
      <div className="bubble bubble-user">
        <div className="muted small">
          {req.requested_by} via {req.source} · <Time iso={req.created_at} />
        </div>
        {req.message}
      </div>
      <div className="bubble bubble-ai">
        <div className="muted small">
          Supervisor · planned by {req.planner || "…"} <Pill value={req.status} />
        </div>
        {req.status === "planning" ? <p>Planning…</p> : <p>{req.reply}</p>}
        {req.plan.notes?.length ? (
          <ul className="notes">
            {req.plan.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        ) : null}
      </div>
      {tasks.length ? (
        <div className="task-steps">
          {tasks.map((t) => (
            <div className="step" key={t.id}>
              <div className="step-head">
                <Pill value={t.status} /> <code>
                  {t.agent}.{t.capability}
                </code>{" "}
                <RiskBadge risk={t.risk} />
                <Link className="small" to={`/tasks?id=${t.id}`}>
                  task #{t.id}
                </Link>
              </div>
              {t.summary ? <p>{t.summary}</p> : null}
              {t.error ? <p className="text-bad">{t.error}</p> : null}
              {t.status === "awaiting_approval" ? (
                <p className="text-warn">
                  Held for approval. <Link to="/approvals">Review it</Link>
                </p>
              ) : null}
              {t.result?.findings?.length ? <Findings findings={t.result.findings.slice(0, 8)} /> : null}
            </div>
          ))}
        </div>
      ) : null}
      {req.summary && req.status !== "planning" ? (
        <div className="bubble bubble-ai summary">
          <div className="muted small">Summary · {req.summarizer}</div>
          <pre className="plain">{req.summary}</pre>
        </div>
      ) : ACTIVE.has(req.status) ? (
        <p className="muted">Agents working…</p>
      ) : null}
    </div>
  );
}

export default function Supervisor() {
  const { canOperate } = useAuth();
  const [message, setMessage] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const list = usePoll(() => api.get<{ requests: SupervisorRequest[] }>("/api/supervisor/requests?limit=30"), 4_000);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!message.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ request: SupervisorRequest }>("/api/supervisor/requests", { message });
      setSelected(r.request.id);
      setMessage("");
      void list.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const current = selected ?? list.data?.requests[0]?.id ?? null;
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>AI Supervisor</h1>
          <p className="muted">
            Ask in plain language. The supervisor plans the work, hands it to the agents and reports back. Anything that publishes, sends or spends waits
            for your approval.
          </p>
        </div>
      </header>
      {canOperate ? (
        <Card>
          <form className="ask" onSubmit={submit}>
            <textarea
              aria-label="Request"
              placeholder="e.g. Check all websites and tell me what needs fixing"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit(e as unknown as FormEvent);
              }}
              rows={3}
              maxLength={4000}
            />
            <div className="ask-row">
              <div className="chips">
                {EXAMPLES.map((ex) => (
                  <button type="button" key={ex} className="chip" onClick={() => setMessage(ex)}>
                    {ex}
                  </button>
                ))}
              </div>
              <button className="btn primary" type="submit" disabled={busy || !message.trim()}>
                {busy ? "Sending…" : "Send"}
              </button>
            </div>
            <ErrorNote error={error} />
          </form>
        </Card>
      ) : (
        <p className="muted">Your role is read-only; ask an operator to submit requests.</p>
      )}
      <div className="split">
        <Card title="History" className="history">
          <ErrorNote error={list.error} />
          {list.data?.requests.length ? (
            <ul className="selectable">
              {list.data.requests.map((r) => (
                <li key={r.id} className={r.id === current ? "selected" : ""}>
                  <button onClick={() => setSelected(r.id)}>
                    <Pill value={r.status} />
                    <span className="truncate">{r.message}</span>
                    <span className="muted small">
                      <Time iso={r.created_at} />
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No requests yet. Try one of the examples above.</Empty>
          )}
        </Card>
        <Card title={current ? `Request #${current}` : "Conversation"}>{current ? <RequestDetail id={current} /> : <Empty>Pick a request.</Empty>}</Card>
      </div>
    </div>
  );
}
