import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, JsonView, Pill, RiskBadge, Time } from "../components";
import { usePoll } from "../hooks";

function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: () => void }) {
  const { canOperate } = useAuth();
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/approvals/${approval.id}/decision`, { approve, note });
      onDecided();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      title={
        <>
          #{approval.id} {approval.summary}
        </>
      }
      actions={<RiskBadge risk={approval.risk} />}
    >
      <p className="muted small">
        {approval.agent}.{approval.capability} · requested <Time iso={approval.created_at} /> · <Link to={`/tasks?id=${approval.task_id}`}>task #{approval.task_id}</Link>
      </p>
      {approval.details.capability_description ? <p>{approval.details.capability_description}</p> : null}
      <details>
        <summary>Exact parameters</summary>
        <JsonView value={approval.details.params ?? {}} />
      </details>
      {approval.status === "pending" ? (
        canOperate ? (
          <div className="decision">
            <input aria-label="Decision note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} />
            <button className="btn primary" disabled={busy} onClick={() => void decide(true)}>
              Approve
            </button>
            <button className="btn danger" disabled={busy} onClick={() => void decide(false)}>
              Reject
            </button>
          </div>
        ) : (
          <p className="muted">An operator or admin must decide.</p>
        )
      ) : (
        <p>
          <Pill value={approval.status} /> by {approval.decided_by} <Time iso={approval.decided_at} />
          {approval.decision_note ? <> — “{approval.decision_note}”</> : null}
        </p>
      )}
      <ErrorNote error={error} />
    </Card>
  );
}

export default function Approvals() {
  const [tab, setTab] = useState<"pending" | "all">("pending");
  const list = usePoll(() => api.get<{ approvals: Approval[] }>(`/api/approvals?status=${tab}`), 5_000, [tab]);
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Approvals</h1>
          <p className="muted">
            Actions that would reach the outside world, spend money or delete something wait here. Approving runs exactly the parameters shown; in this build
            every outward integration is a mock, so approved actions are recorded but not sent.
          </p>
        </div>
        <div className="tabs">
          <button className={tab === "pending" ? "active" : ""} onClick={() => setTab("pending")}>
            Pending
          </button>
          <button className={tab === "all" ? "active" : ""} onClick={() => setTab("all")}>
            History
          </button>
        </div>
      </header>
      <ErrorNote error={list.error} />
      {list.data?.approvals.length ? (
        list.data.approvals.map((a) => <ApprovalCard key={a.id} approval={a} onDecided={() => void list.refresh()} />)
      ) : (
        <Empty>{tab === "pending" ? "Nothing is waiting for you." : "No approvals yet."}</Empty>
      )}
    </div>
  );
}
