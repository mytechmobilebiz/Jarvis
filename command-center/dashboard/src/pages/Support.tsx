import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { deliveryCounts, ticketStatus, type TicketWithDelivery } from "./shared";
import "./Support.css";

const PRIORITY_TONE: Record<string, string> = { urgent: "critical", high: "warning", normal: "info", low: "unknown" };
/** Statuses that close a message (the Support agent's own overview counts everything else as open). */
const CLOSED = ["replied", "closed"];
/** The most the API returns in one list; when the list is that long the tiles count the latest messages, not all. */
const LIMIT = 500;

export default function Support() {
  const { canOperate } = useAuth();
  const tickets = usePoll(() => api.get<{ tickets: TicketWithDelivery[] }>(`/api/support/tickets?limit=${LIMIT}`), 6_000);
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

  async function send(t: TicketWithDelivery) {
    setError(null);
    try {
      const r = await api.post<{ task: Task }>("/api/agents/communications/tasks", { capability: "send_reply", params: { ticket_id: t.id } });
      setNote(`Reply to ${t.customer_email} is waiting for approval (task #${r.task.id}).`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  // Summary of the same list the messages table shows.
  const all = tickets.data?.tickets ?? [];
  const loaded = !!tickets.data;
  const pending = tickets.error ?? "…";
  const open = all.filter((t) => !CLOSED.includes(t.status));
  const urgent = open.filter((t) => t.priority === "urgent").length;
  const high = open.filter((t) => t.priority === "high").length;
  const awaiting = open.filter((t) => t.reply_artifact_id).length;
  const untriaged = open.filter((t) => t.status === "open").length;
  const capped = all.length >= LIMIT;
  // A reply counts as sent only when a live email integration carried it (C2-01).
  const replies = deliveryCounts(all, "replied");
  const closedCount = all.filter((t) => t.status === "closed").length;
  const answered = [
    closedCount ? `${closedCount} closed` : "",
    replies.live ? `${replies.live} replied` : "",
    replies.mock ? `${replies.mock} reply recorded (mock), not sent` : "",
    replies.unknown ? `${replies.unknown} reply approved, delivery not recorded` : "",
  ].filter(Boolean);

  return (
    <div className="page sp-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Communications</div>
          <h1>Client support</h1>
          <p className="muted">Client messages are triaged (category, priority, sentiment) and a reply is drafted for review. Replies go out only after approval.</p>
        </div>
      </header>

      <div className="tiles">
        <Tile
          icon="inbox"
          tone="blue"
          label="Open messages"
          hint={loaded ? `${answered.join(" · ") || "None answered or closed yet"}${untriaged ? ` · ${untriaged} waiting for triage` : ""}` : pending}
        >
          <span className="tile-value tabular">{loaded ? open.length : "—"}</span>
          {loaded ? <span className="tile-of">of {capped ? `the latest ${LIMIT}` : all.length}</span> : null}
        </Tile>
        <Tile icon="alert" tone={!loaded ? "muted" : urgent ? "bad" : high ? "warn" : "green"} label="Urgent or high priority" hint={loaded ? `${urgent} urgent · ${high} high, among open messages` : pending}>
          <span className="tile-value tabular">{loaded ? urgent + high : "—"}</span>
        </Tile>
        <Tile
          icon="mail"
          tone="purple"
          label="Awaiting reply"
          hint={loaded ? `Reply drafted, not sent yet${open.length > awaiting ? ` · ${open.length - awaiting} open without a draft` : ""}` : pending}
        >
          <span className="tile-value tabular">{loaded ? awaiting : "—"}</span>
        </Tile>
      </div>

      {canOperate ? (
        <Card
          title={
            <>
              <Icon name="mail" size={18} className="card-icon" /> Log a client message
            </>
          }
        >
          <form className="grid-form sp-form" onSubmit={add}>
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
      <Card
        title={
          <>
            <Icon name="inbox" size={18} className="card-icon" /> Messages
          </>
        }
      >
        {tickets.data?.tickets.length ? (
          <div className="table-wrap">
            <table className="table sp-table">
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
                    <td className="sp-message">
                      <strong className="sp-subject" title={t.subject}>
                        {t.subject}
                      </strong>
                      <div className="muted small sp-from">
                        {t.customer_name || "unknown"} · {t.customer_email || "no email"}
                        {t.sentiment ? ` · ${t.sentiment}` : ""}
                      </div>
                    </td>
                    <td>{t.category || "…"}</td>
                    <td>
                      <Pill value={PRIORITY_TONE[t.priority] ?? "info"} label={t.priority} />
                    </td>
                    <td>
                      <Pill value={ticketStatus(t)[0]} label={ticketStatus(t)[1]} />
                    </td>
                    <td className="sp-nowrap">
                      <Time iso={t.created_at} />
                    </td>
                    <td>
                      <div className="row-actions cell-actions">
                        {t.reply_artifact_id ? <Link to={`/drafts?id=${t.reply_artifact_id}`}>draft</Link> : null}
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
          </div>
        ) : (
          <Empty>No client messages yet.</Empty>
        )}
      </Card>
    </div>
  );
}
