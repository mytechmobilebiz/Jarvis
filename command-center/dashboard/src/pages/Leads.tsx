import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api, type LeadRecord, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import "./Leads.css";

const EMPTY = { name: "", company: "", email: "", phone: "", website: "", service_interest: "", message: "" };
const WEEK = 7 * 24 * 3_600_000;
/** The most the API returns in one list; when the list is that long the tiles say they count the latest leads, not all. */
const LIMIT = 500;

function scoreTone(v: number): "good" | "warn" | "bad" {
  return v >= 75 ? "good" : v >= 50 ? "warn" : "bad";
}

export default function Leads() {
  const { canOperate } = useAuth();
  const leads = usePoll(() => api.get<{ leads: LeadRecord[] }>(`/api/leads?limit=${LIMIT}`), 6_000);
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
      const r = await api.post<{ task: Task }>("/api/agents/communications/tasks", { capability, params: { lead_id: lead.id } });
      setNote(r.task.status === "awaiting_approval" ? `Sending to ${lead.email} is waiting for approval (task #${r.task.id}).` : `Task #${r.task.id} queued.`);
      await leads.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  // Summary of the same list the pipeline table shows.
  const all = leads.data?.leads ?? [];
  const loaded = !!leads.data;
  const pending = leads.error ?? "…";
  const byStatus = (status: string) => all.filter((l) => l.status === status).length;
  const recent = all.filter((l) => Date.now() - new Date(l.created_at).getTime() < WEEK).length;
  const scores = all.map((l) => l.score).filter((v): v is number => typeof v === "number");
  const avgScore = scores.length ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : null;
  const spam = byStatus("spam");
  const capped = all.length >= LIMIT;

  return (
    <div className="page le-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Communications</div>
          <h1>Leads</h1>
          <p className="muted">
            New enquiries from the dashboard or the n8n lead-intake webhook. The Sales agent scores each one; proposals are drafted for review and sent only
            after approval.
          </p>
        </div>
      </header>

      <div className="tiles cols-4">
        <Tile
          icon="users"
          tone="blue"
          label={capped ? "Leads" : "Total leads"}
          hint={loaded ? `${recent} received in the last 7 days${capped ? ` · in the latest ${LIMIT}` : ""}` : pending}
        >
          <span className="tile-value tabular">{loaded ? all.length : "—"}</span>
        </Tile>
        <Tile
          icon="inbox"
          tone="cyan"
          label="New leads"
          hint={loaded ? `${byStatus("qualified")} qualified · ${byStatus("proposal_drafted")} with a proposal · ${byStatus("proposal_sent")} sent` : pending}
        >
          <span className="tile-value tabular">{loaded ? byStatus("new") : "—"}</span>
        </Tile>
        <Tile
          icon="trending"
          tone="purple"
          label="Average score"
          hint={loaded ? (scores.length ? `${scores.length} scored${all.length > scores.length ? ` · ${all.length - scores.length} waiting for a score` : ""}` : "No lead scored yet") : pending}
        >
          <span className="tile-value tabular">{avgScore ?? "—"}</span>
          {avgScore != null ? <span className="tile-of">of 100</span> : null}
          {avgScore != null ? (
            <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={avgScore} aria-label="Average lead score">
              <span className={`meter-fill tone-${scoreTone(avgScore)}`} style={{ width: `${Math.max(2, avgScore)}%` }} />
            </div>
          ) : null}
        </Tile>
        <Tile icon="shield" tone={spam ? "orange" : "muted"} label="Spam" hint={loaded ? "Flagged by the Sales agent's scoring" : pending}>
          <span className="tile-value tabular">{loaded ? spam : "—"}</span>
        </Tile>
      </div>

      {canOperate ? (
        <Card
          title={
            <>
              <Icon name="user" size={18} className="card-icon" /> Add a lead
            </>
          }
        >
          <form className="grid-form le-form" onSubmit={add}>
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
      <Card
        title={
          <>
            <Icon name="trending" size={18} className="card-icon" /> Pipeline ({leads.data?.leads.length ?? 0})
          </>
        }
      >
        {leads.data?.leads.length ? (
          <div className="table-wrap">
            <table className="table le-table">
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
                    <td className="clickable le-lead" onClick={() => setOpen(l)}>
                      {/* The cell opens on a click; this button is the keyboard path to the same dialog. */}
                      <button
                        type="button"
                        className="link-button le-open"
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpen(l);
                        }}
                      >
                        <strong className="le-name">{l.company || l.name}</strong>
                      </button>
                      <div className="muted small le-contact">
                        {l.name} · {l.email || "no email"}
                      </div>
                    </td>
                    <td>{l.score === null ? "…" : <Pill value={l.score >= 75 ? "up" : l.score >= 50 ? "degraded" : "unknown"} label={String(l.score)} />}</td>
                    <td>
                      <Pill value={l.status === "spam" ? "failed" : l.status === "proposal_sent" ? "succeeded" : "info"} label={l.status.replace(/_/g, " ")} />
                    </td>
                    <td className="small">{l.source}</td>
                    <td className="le-nowrap">
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
          </div>
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
