import { FormEvent, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type SupervisorRequest, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Findings, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import "./Supervisor.css";

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
  if (!req) return <p className="muted su-working">Loading…</p>;
  return (
    <div className="request-detail su-thread">
      <div className="su-turn su-turn-user">
        <div className="bubble bubble-user">
          <div className="muted small">
            {req.requested_by} via {req.source} · <Time iso={req.created_at} />
          </div>
          {req.message}
        </div>
        <span className="su-avatar su-avatar-user" aria-hidden>
          <Icon name="user" size={16} />
        </span>
      </div>
      <div className="su-turn">
        <span className="su-avatar" aria-hidden>
          <Icon name="sparkles" size={16} />
        </span>
        <div className="bubble bubble-ai">
          <div className="muted small su-bubble-head">
            Supervisor · planned by {req.planner || "…"} <Pill value={req.status} />
          </div>
          {req.status === "planning" ? <p className="su-working">Planning…</p> : <p>{req.reply}</p>}
          {req.plan.notes?.length ? (
            <ul className="notes">
              {req.plan.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          ) : null}
        </div>
      </div>
      {tasks.length ? (
        <div className="task-steps su-steps">
          {tasks.map((t) => (
            <div className={`step su-step su-step-${t.status}`} key={t.id}>
              <div className="step-head">
                <Pill value={t.status} />{" "}
                <code>
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
        <div className="su-turn">
          <span className="su-avatar" aria-hidden>
            <Icon name="sparkles" size={16} />
          </span>
          <div className="bubble bubble-ai summary su-summary">
            <div className="muted small">Summary · {req.summarizer}</div>
            <pre className="plain">{req.summary}</pre>
          </div>
        </div>
      ) : ACTIVE.has(req.status) ? (
        <p className="muted su-working">Agents working…</p>
      ) : null}
    </div>
  );
}

export default function Supervisor() {
  const { canOperate } = useAuth();
  const [message, setMessage] = useState("");
  // The selected request lives in ?id= so the command bar and dashboard can link straight to one.
  const [params, setParams] = useSearchParams();
  const selected = Number(params.get("id")) || null;
  const setSelected = (id: number) => setParams({ id: String(id) }, { replace: true });
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
  const recent = list.data?.requests ?? null;
  const countOf = (...statuses: SupervisorRequest["status"][]) => (recent ? recent.filter((r) => statuses.includes(r.status)).length : null);
  const working = countOf("planning", "running");
  const waiting = countOf("awaiting_approval");
  const failed = countOf("failed");
  const scope = recent ? `Latest ${recent.length} request${recent.length === 1 ? "" : "s"}` : (list.error ?? "…");
  const value = (n: number | null) => <span className="tile-value tabular">{n ?? "—"}</span>;
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Automation</div>
          <h1>AI Supervisor</h1>
          <p className="muted">
            Ask in plain language. The supervisor plans the work, hands it to the agents and reports back. Anything that publishes, sends or spends waits for
            your approval.
          </p>
        </div>
      </header>
      <div className="tiles cols-4 su-tiles">
        <Tile icon="pulse" tone="blue" label="Working now" hint={<>Planning or running · {scope}</>}>
          {value(working)}
        </Tile>
        <Tile icon="approvals" tone={waiting ? "warn" : "orange"} label="Awaiting approval" to="/approvals" hint={scope}>
          {value(waiting)}
        </Tile>
        <Tile icon="check" tone="green" label="Completed" hint={scope}>
          {value(countOf("completed"))}
        </Tile>
        <Tile icon="alert" tone={failed ? "bad" : "muted"} label="Failed" hint={scope}>
          {value(failed)}
        </Tile>
      </div>
      {canOperate ? (
        <Card
          className="su-ask-card"
          title={
            <>
              <Icon name="send" size={18} className="card-icon" /> New request
            </>
          }
          actions={<span className="muted small su-shortcut">Ctrl + Enter to send</span>}
        >
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
              <button className="btn primary su-send" type="submit" disabled={busy || !message.trim()}>
                <Icon name="send" size={16} /> {busy ? "Sending…" : "Send"}
              </button>
            </div>
            <ErrorNote error={error} />
          </form>
        </Card>
      ) : (
        <p className="alert alert-info su-readonly">
          <Icon name="lock" size={15} /> Your role is read-only; ask an operator to submit requests.
        </p>
      )}
      <div className="split">
        <Card
          title={
            <>
              <Icon name="history" size={18} className="card-icon" /> History
            </>
          }
          className="history su-history-card"
        >
          <ErrorNote error={list.error} />
          {list.data?.requests.length ? (
            <ul className="selectable su-history">
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
        <Card
          className="su-conversation"
          title={
            <>
              <Icon name="sparkles" size={18} className="card-icon" /> {current ? `Request #${current}` : "Conversation"}
            </>
          }
        >
          {current ? <RequestDetail id={current} /> : <Empty>Pick a request.</Empty>}
        </Card>
      </div>
    </div>
  );
}
