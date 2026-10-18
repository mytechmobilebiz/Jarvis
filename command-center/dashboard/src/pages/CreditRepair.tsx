import { FormEvent, useState } from "react";
import { api, type CRClient, type CRStatus, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";

interface ClientDetail {
  client: CRClient;
  items: { id: number; bureau: string; creditor: string; account_last4: string; reason: string; detail: string; status: string }[];
  letters: { id: number; bureau: string; status: string; created_at: string; mailed_at: string | null }[];
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
          <table className="table compact">
            <tbody>
              {detail.data!.items.map((i) => (
                <tr key={i.id}>
                  <td>{i.bureau}</td>
                  <td>
                    {i.creditor} {i.account_last4 ? `…${i.account_last4}` : ""}
                  </td>
                  <td>{i.reason.replace(/_/g, " ")}</td>
                  <td>
                    <Pill value="info" label={i.status.replace(/_/g, " ")} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <form
            className="grid-form"
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
            <ul className="list">
              {detail.data!.letters.map((l) => (
                <li key={l.id}>
                  #{l.id} {l.bureau} <Pill value={l.status === "mailed_mock" ? "succeeded" : "info"} label={l.status.replace("_", " ")} /> <Time iso={l.created_at} />{" "}
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
          {letter ? <pre className="plain boxed">{letter}</pre> : null}
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
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Credit Repair</h1>
          <p className="muted">
            An isolated module: client data lives in its own encrypted database, every access is logged, and nothing personal reaches the AI, other agents or the
            activity feed.
          </p>
        </div>
      </header>
      <ErrorNote error={status.error ?? clients.error ?? error} />
      {s && !s.configured ? <Card>The Credit Repair store is not configured on this installation.</Card> : null}
      {s && !s.entitled ? <Card>The Credit Repair module is not included in this edition.</Card> : null}
      {s && s.configured && s.entitled && !s.enabled ? (
        <Card title="Compliance notice">
          <p>{s.notice}</p>
          {user.role === "admin" ? (
            <>
              <label className="check">
                <input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} /> I have read this notice and accept responsibility for compliance.
              </label>
              <button className="btn primary" disabled={!ack} onClick={() => void enable()}>
                Enable the module
              </button>
            </>
          ) : (
            <p className="muted">An admin must enable the module.</p>
          )}
        </Card>
      ) : null}
      {s?.enabled && !s.can_access ? (
        <Card>Your account does not have the sensitive-data permission. An admin can grant it on the Settings page.</Card>
      ) : null}
      {ready ? (
        <>
          <p className="muted small">
            Enabled by {s!.enabled!.acknowledged_by} <Time iso={s!.enabled!.acknowledged_at} />. Use test data only until the module has had legal review.
          </p>
          <Card title="Add a client">
            <form className="grid-form" onSubmit={add}>
              <input aria-label="full name" placeholder="full name" value={form.full_name} onChange={(e) => setForm({ ...form, full_name: e.target.value })} required />
              <input aria-label="address" placeholder="address" value={form.address} onChange={(e) => setForm({ ...form, address: e.target.value })} />
              <input aria-label="date of birth" placeholder="date of birth (YYYY-MM-DD)" value={form.dob} onChange={(e) => setForm({ ...form, dob: e.target.value })} />
              <input aria-label="SSN last 4" placeholder="SSN last 4 only" value={form.ssn_last4} onChange={(e) => setForm({ ...form, ssn_last4: e.target.value })} />
              <button className="btn primary" type="submit">
                Add client
              </button>
            </form>
          </Card>
          <Card title="Clients">
            {clients.data?.clients.length ? (
              <table className="table">
                <tbody>
                  {clients.data.clients.map((c) => (
                    <tr key={c.id} className="clickable" onClick={() => setOpenId(c.id)}>
                      <td>{c.reference}</td>
                      <td>{c.full_name}</td>
                      <td>
                        <Pill value="info" label={c.status} />
                      </td>
                      <td className="small">contract: {c.contract_signed_on ?? "not signed"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
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
