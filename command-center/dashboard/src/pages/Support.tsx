import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Task, type Ticket } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, Time } from "../components";
import { usePoll } from "../hooks";

const PRIORITY_TONE: Record<string, string> = { urgent: "critical", high: "warning", normal: "info", low: "unknown" };

export default function Support() {
  const { canOperate } = useAuth();
  const tickets = usePoll(() => api.get<{ tickets: Ticket[] }>("/api/support/tickets"), 6_000);
  const [form, setForm] = useState({ customer_name: "", customer_email: "", subject: "", body: "" });
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function add(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/support/tickets", form);
      setForm({ customer_name: "", customer_email: "", subject: "", body: "" });
      await tickets.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function send(t: Ticket) {
    setError(null);
    try {
      const r = await api.post<{ task: Task }>("/api/agents/support/tasks", { capability: "send_reply", params: { ticket_id: t.id } });
      setNote(`Reply to ${t.customer_email} is waiting for approval (task #${r.task.id}).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Client support</h1>
          <p className="muted">Client messages are triaged (category, priority, sentiment) and a reply is drafted for review. Replies go out only after approval.</p>
        </div>
      </header>
      {canOperate ? (
        <Card title="Log a client message">
          <form className="grid-form" onSubmit={add}>
            <input aria-label="client name" placeholder="client name" value={form.customer_name} onChange={(e) => setForm({ ...form, customer_name: e.target.value })} />
            <input aria-label="client email" placeholder="client email" type="email" value={form.customer_email} onChange={(e) => setForm({ ...form, customer_email: e.target.value })} />
            <input aria-label="subject" placeholder="subject" value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} required />
            <textarea aria-label="message body" placeholder="message" rows={3} value={form.body} onChange={(e) => setForm({ ...form, body: e.target.value })} />
            <button className="btn primary" type="submit">
              Add message
            </button>
          </form>
        </Card>
      ) : null}
      <ErrorNote error={tickets.error ?? error} />
      {note ? <div className="alert alert-good">{note}</div> : null}
      <Card title="Messages">
        {tickets.data?.tickets.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>Message</th>
                <th>Category</th>
                <th>Priority</th>
                <th>Status</th>
                <th>Received</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {tickets.data.tickets.map((t) => (
                <tr key={t.id}>
                  <td>
                    <strong>{t.subject}</strong>
                    <div className="muted small">
                      {t.customer_name || "unknown"} · {t.customer_email || "no email"}
                      {t.sentiment ? ` · ${t.sentiment}` : ""}
                    </div>
                  </td>
                  <td>{t.category || "…"}</td>
                  <td>
                    <Pill value={PRIORITY_TONE[t.priority] ?? "info"} label={t.priority} />
                  </td>
                  <td>
                    <Pill value={t.status === "replied" ? "succeeded" : "info"} label={t.status} />
                  </td>
                  <td>
                    <Time iso={t.created_at} />
                  </td>
                  <td>
                    <div className="row-actions cell-actions">
                      {t.reply_artifact_id ? <Link to="/drafts">draft</Link> : null}
                      {canOperate && t.reply_artifact_id && t.status !== "replied" ? (
                        <button className="btn small" onClick={() => void send(t)}>
                          Send reply (needs approval)
                        </button>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No client messages yet.</Empty>
        )}
      </Card>
    </div>
  );
}
