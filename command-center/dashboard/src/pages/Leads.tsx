import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type LeadRecord, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";

const EMPTY = { name: "", company: "", email: "", phone: "", website: "", service_interest: "", message: "" };

export default function Leads() {
  const { canOperate } = useAuth();
  const leads = usePoll(() => api.get<{ leads: LeadRecord[] }>("/api/leads"), 6_000);
  const [form, setForm] = useState(EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [open, setOpen] = useState<LeadRecord | null>(null);

  async function add(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/leads", form);
      setForm(EMPTY);
      await leads.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function run(lead: LeadRecord, capability: string) {
    setError(null);
    try {
      const r = await api.post<{ task: Task }>("/api/agents/sales/tasks", { capability, params: { lead_id: lead.id } });
      setNote(r.task.status === "awaiting_approval" ? `Sending to ${lead.email} is waiting for approval (task #${r.task.id}).` : `Task #${r.task.id} queued.`);
      await leads.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Leads</h1>
          <p className="muted">
            New enquiries from the dashboard or the n8n lead-intake webhook. The Sales agent scores each one; proposals are drafted for review and sent only
            after approval.
          </p>
        </div>
      </header>
      {canOperate ? (
        <Card title="Add a lead">
          <form className="grid-form" onSubmit={add}>
            {(["name", "company", "email", "phone", "website", "service_interest"] as const).map((k) => (
              <input key={k} aria-label={k} placeholder={k.replace("_", " ")} value={form[k]} onChange={(e) => setForm({ ...form, [k]: e.target.value })} required={k === "name"} />
            ))}
            <textarea aria-label="message" placeholder="message" rows={2} value={form.message} onChange={(e) => setForm({ ...form, message: e.target.value })} />
            <button className="btn primary" type="submit">
              Add lead
            </button>
          </form>
        </Card>
      ) : null}
      <ErrorNote error={leads.error ?? error} />
      {note ? <div className="alert alert-good">{note}</div> : null}
      <Card title={`Pipeline (${leads.data?.leads.length ?? 0})`}>
        {leads.data?.leads.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>Lead</th>
                <th>Score</th>
                <th>Status</th>
                <th>Source</th>
                <th>Received</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {leads.data.leads.map((l) => (
                <tr key={l.id}>
                  <td className="clickable" onClick={() => setOpen(l)}>
                    <strong>{l.company || l.name}</strong>
                    <div className="muted small">
                      {l.name} · {l.email || "no email"}
                    </div>
                  </td>
                  <td>{l.score === null ? "…" : <Pill value={l.score >= 75 ? "up" : l.score >= 50 ? "degraded" : "unknown"} label={String(l.score)} />}</td>
                  <td>
                    <Pill value={l.status === "spam" ? "failed" : l.status === "proposal_sent" ? "succeeded" : "info"} label={l.status.replace(/_/g, " ")} />
                  </td>
                  <td className="small">{l.source}</td>
                  <td>
                    <Time iso={l.created_at} />
                  </td>
                  <td>
                    <div className="row-actions cell-actions">
                      {l.proposal_artifact_id ? <Link to="/drafts">proposal</Link> : null}
                      {canOperate && l.status !== "spam" && l.status !== "proposal_sent" ? (
                        <>
                          <button className="btn small" onClick={() => void run(l, "draft_proposal")}>
                            {l.proposal_artifact_id ? "Redraft" : "Draft proposal"}
                          </button>
                          {l.proposal_artifact_id ? (
                            <button className="btn small" onClick={() => void run(l, "send_proposal")}>
                              Send (needs approval)
                            </button>
                          ) : null}
                        </>
                      ) : null}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No leads yet.</Empty>
        )}
      </Card>
      {open ? (
        <Modal title={open.company || open.name} onClose={() => setOpen(null)}>
          <dl className="kv">
            <dt>Contact</dt>
            <dd>
              {open.name} · {open.email} · {open.phone}
            </dd>
            <dt>Website</dt>
            <dd>{open.website || "—"}</dd>
            <dt>Interested in</dt>
            <dd>{open.service_interest || "—"}</dd>
          </dl>
          <p className="pre-wrap">{open.message}</p>
          <h3>Score {open.score ?? "…"}</h3>
          <ul className="list small">
            {open.score_reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
        </Modal>
      ) : null}
    </div>
  );
}
