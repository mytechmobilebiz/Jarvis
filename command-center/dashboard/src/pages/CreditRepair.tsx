import { FormEvent, useState } from "react";
import { api, type CRClient, type CRStatus, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { mockLabel } from "./shared";
import "./CreditRepair.css";

/** How a letter was sent for mailing (0.5.0 API): "mock" recorded only, nothing mailed; "live" mailed by a provider; null not sent. */
type MailMode = "mock" | "live" | null;

interface ClientDetail {
  client: CRClient;
  items: { id: number; bureau: string; creditor: string; account_last4: string; reason: string; detail: string; status: string; mail_mode?: MailMode }[];
  letters: { id: number; bureau: string; status: string; created_at: string; mailed_at: string | null; mail_mode?: MailMode }[];
}

/** [Pill value, label] for a letter: "Mailed" only when a live provider mailed it (C2-02). */
export function letterStatus(l: { status: string; mail_mode?: MailMode }): [string, string] {
  const mode = l.mail_mode !== undefined ? l.mail_mode : l.status === "mailed_mock" ? "mock" : l.status === "mailed" ? "live" : null;
  if (mode === "mock") return ["mock", mockLabel("postal_mail")];
  if (mode === "live") return ["sent", "Mailed"];
  return ["info", l.status.replace(/_/g, " ")];
}

/** An item in a letter that was only recorded by the mock mailer is not under dispute with the bureau (C2-02). */
export function itemStatus(i: { status: string; mail_mode?: MailMode }): [string, string] {
  if (i.status === "disputed" && i.mail_mode === "mock") return ["mock", "Dispute recorded (mock), not mailed"];
  if (i.status === "disputed" && i.mail_mode === "live") return ["sent", "Disputed (mailed)"];
  return ["info", i.status.replace(/_/g, " ")];
}

function ClientModal({ id, onClose }: { id: number; onClose: () => void }) {
  const detail = usePoll(() => api.get<ClientDetail>(`/api/credit-repair/clients/${id}`), 0, [id]);
  const [item, setItem] = useState({ bureau: "experian", creditor: "", account_last4: "", reason: "not_mine", detail: "" });
  const [letter, setLetter] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const c = detail.data?.client;

  async function act(fn: () => Promise<unknown>, done?: string) {
    setError(null);
    try {
      await fn();
      if (done) setNote(done);
      await detail.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function runTask(capability: string, params: Record<string, unknown>) {
    const r = await api.post<{ task: Task }>("/api/agents/credit_repair/tasks", { capability, params });
    return r.task;
  }

  return (
    <Modal title={c ? `${c.reference} · ${c.full_name}` : "Client"} onClose={onClose} wide>
      <ErrorNote error={detail.error ?? error} />
      {note ? <div className="alert alert-good">{note}</div> : null}
      {c ? (
        <>
          <dl className="kv">
            <dt>Status</dt>
            <dd>
              <Pill value="info" label={c.status} />
            </dd>
            <dt>Contract signed</dt>
            <dd>
              <input
                type="date"
                aria-label="Contract signed on"
                value={c.contract_signed_on ?? ""}
                onChange={(e) => void act(() => api.patch(`/api/credit-repair/clients/${id}`, { contract_signed_on: e.target.value || null, status: "active" }))}
              />
            </dd>
            <dt>Address</dt>
            <dd className="pre-wrap">{c.address || "—"}</dd>
            <dt>DOB · SSN last 4</dt>
            <dd>
              {c.dob || "—"} · {c.ssn_last4 || "—"}
            </dd>
          </dl>
          <h3>Disputed items</h3>
          <div className="table-wrap">
            <table className="table compact cr-items">
              <tbody>
                {detail.data!.items.map((i) => (
                  <tr key={i.id}>
                    <td>{i.bureau}</td>
                    <td>
                      {i.creditor} {i.account_last4 ? `…${i.account_last4}` : ""}
                    </td>
                    <td>{i.reason.replace(/_/g, " ")}</td>
                    <td>
                      <Pill value={itemStatus(i)[0]} label={itemStatus(i)[1]} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <form
            className="grid-form cr-item-form"
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              void act(() => api.post(`/api/credit-repair/clients/${id}/items`, item).then(() => setItem({ ...item, creditor: "", account_last4: "", detail: "" })));
            }}
          >
            <select aria-label="bureau" value={item.bureau} onChange={(e) => setItem({ ...item, bureau: e.target.value })}>
              <option>equifax</option>
              <option>experian</option>
              <option>transunion</option>
            </select>
            <input aria-label="creditor" placeholder="creditor" value={item.creditor} onChange={(e) => setItem({ ...item, creditor: e.target.value })} required />
            <input aria-label="account last 4" placeholder="account last 4" value={item.account_last4} onChange={(e) => setItem({ ...item, account_last4: e.target.value })} />
            <select aria-label="reason" value={item.reason} onChange={(e) => setItem({ ...item, reason: e.target.value })}>
              {["not_mine", "inaccurate_balance", "inaccurate_status", "duplicate", "outdated", "other"].map((r) => (
                <option key={r} value={r}>
                  {r.replace(/_/g, " ")}
                </option>
              ))}
            </select>
            <button className="btn small" type="submit">
              Add item
            </button>
          </form>
          <div className="row-actions">
            <button
              className="btn primary small"
              onClick={() => void act(() => runTask("draft_dispute_letters", { client_id: id }), "Drafting letters; refresh in a moment.")}
            >
              Draft dispute letters
            </button>
            <button className="btn small" onClick={() => void act(() => runTask("compliance_review", { client_id: id }), "Compliance review queued (see Tasks).")}>
              Compliance review
            </button>
          </div>
          <h3>Letters</h3>
          {detail.data!.letters.length ? (
            <ul className="list cr-letters">
              {detail.data!.letters.map((l) => (
                <li key={l.id}>
                  #{l.id} {l.bureau} <Pill value={letterStatus(l)[0]} label={letterStatus(l)[1]} /> drafted <Time iso={l.created_at} />
                  {l.mailed_at ? (
                    <>
                      {" "}
                      · {letterStatus(l)[0] === "sent" ? "mailed" : "recorded"} <Time iso={l.mailed_at} />
                    </>
                  ) : null}{" "}
                  <button className="btn small" onClick={() => void api.get<{ letter: { content: string } }>(`/api/credit-repair/letters/${l.id}`).then((r) => setLetter(r.letter.content))}>
                    Read
                  </button>
                  {l.status === "draft" ? (
                    <button className="btn small" onClick={() => void act(() => runTask("mail_dispute_letter", { letter_id: l.id }), "Mailing is waiting for approval.")}>
                      Mail (needs approval)
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">No letters yet.</p>
          )}
          {letter ? <pre className="plain boxed cr-letter">{letter}</pre> : null}
        </>
      ) : (
        <p className="muted">Loading…</p>
      )}
    </Modal>
  );
}

export default function CreditRepair() {
  const { user } = useAuth();
  const status = usePoll(() => api.get<CRStatus>("/api/credit-repair/status"), 0);
  const s = status.data;
  const ready = !!(s?.configured && s.entitled && s.enabled && s.can_access);
  const clients = usePoll(() => (ready ? api.get<{ clients: CRClient[] }>("/api/credit-repair/clients") : Promise.resolve({ clients: [] })), 0, [ready]);
  const [form, setForm] = useState({ full_name: "", address: "", dob: "", ssn_last4: "" });
  const [ack, setAck] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);

  async function enable() {
    setError(null);
    try {
      await api.post("/api/credit-repair/enable", { acknowledge: ack });
      await status.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  async function add(e: FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.post("/api/credit-repair/clients", form);
      setForm({ full_name: "", address: "", dob: "", ssn_last4: "" });
      await clients.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="page cr-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Sensitive module</div>
          <h1>Credit Repair</h1>
          <p className="muted">
            An isolated module: client data lives in its own encrypted database, every access is logged, and nothing personal reaches the AI, other agents or the
            activity feed.
          </p>
        </div>
      </header>
      <ErrorNote error={status.error ?? clients.error ?? error} />
      {s && !s.configured ? (
        <Card className="cr-state">
          <Icon name="database" size={18} className="cr-state-icon" />
          The Credit Repair store is not configured on this installation.
        </Card>
      ) : null}
      {s && !s.entitled ? (
        <Card className="cr-state">
          <Icon name="lock" size={18} className="cr-state-icon" />
          The Credit Repair module is not included in this edition.
        </Card>
      ) : null}
      {s && s.configured && s.entitled && !s.enabled ? (
        <Card
          className="cr-compliance"
          title={
            <>
              <Icon name="shield" size={18} className="card-icon" /> Compliance notice
            </>
          }
        >
          <p className="cr-notice">{s.notice}</p>
          {user.role === "admin" ? (
            <>
              <label className="check">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I have read this notice and accept responsibility for compliance.
              </label>
              <div className="cr-enable">
                <button className="btn primary" disabled={!ack} onClick={() => void enable()}>
                  <Icon name="shield" size={16} /> Enable the module
                </button>
              </div>
            </>
          ) : (
            <p className="muted">An admin must enable the module.</p>
          )}
        </Card>
      ) : null}
      {s?.enabled && !s.can_access ? (
        <Card className="cr-state">
          <Icon name="lock" size={18} className="cr-state-icon" />
          Your account does not have the sensitive-data permission. An admin can grant it on the Settings page.
        </Card>
      ) : null}
      {ready ? (
        <>
          <p className="muted small cr-enabled">
            <Icon name="shield" size={16} className="cr-enabled-icon" />
            <span>
              Enabled by {s!.enabled!.acknowledged_by} <Time iso={s!.enabled!.acknowledged_at} />. Use test data only until the module has had legal review.
            </span>
          </p>
          <Card
            title={
              <>
                <Icon name="user" size={18} className="card-icon" /> Add a client
              </>
            }
          >
            <form className="grid-form cr-add" onSubmit={add}>
              <input aria-label="full name" placeholder="full name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required />
              <input aria-label="address" placeholder="address" value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} />
              <input aria-label="date of birth" placeholder="date of birth (YYYY-MM-DD)" value={form.dob} onChange={(e) => setForm({ ...form, dob: e.target.value })} />
              <input aria-label="SSN last 4" placeholder="SSN last 4 only" value={form.ssn_last4} onChange={(e) => setForm({ ...form, ssn_last4: e.target.value })} />
              <button className="btn primary" type="submit">
                Add client
              </button>
            </form>
          </Card>
          <Card
            title={
              <>
                <Icon name="users" size={18} className="card-icon" /> Clients
              </>
            }
          >
            {clients.data?.clients.length ? (
              <div className="table-wrap">
                <table className="table cr-clients">
                  <tbody>
                    {clients.data.clients.map((c) => (
                      <tr key={c.id} className="clickable" onClick={() => setOpenId(c.id)}>
                        <td className="cr-ref tabular">{c.reference}</td>
                        <td className="cr-name">
                          {/* The row opens on a click; this button is the keyboard path to the same dialog. */}
                          <button
                            type="button"
                            className="link-button"
                            onClick={(e) => {
                              e.stopPropagation();
                              setOpenId(c.id);
                            }}
                          >
                            {c.full_name}
                          </button>
                        </td>
                        <td>
                          <Pill value="info" label={c.status} />
                        </td>
                        <td className="small cr-contract">contract: {c.contract_signed_on ?? "not signed"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty>No clients.</Empty>
            )}
          </Card>
        </>
      ) : null}
      {openId ? <ClientModal id={openId} onClose={() => setOpenId(null)} /> : null}
    </div>
  );
}
