import { useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, JsonView, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import "./Approvals.css";

function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: () => void }) {
  const { user, canOperate } = useAuth();
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState("");
  const production = approval.details.production === true;
  const confirmText = approval.details.confirm ?? "";
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/approvals/${approval.id}/decision`, { approve, note, confirm: approve ? confirm : "" });
      onDecided();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card
      className={`ap-card ap-${approval.status} ${production ? "ap-production" : ""}`}
      title={
        <>
          <span className="ap-id tabular">#{approval.id}</span> <span className="ap-title">{approval.summary}</span>
        </>
      }
      actions={
        <>
          {production ? <Pill value="critical" label="PRODUCTION" /> : null} <RiskBadge risk={approval.risk} />
        </>
      }
    >
      <p className="muted small ap-meta">
        <span className="ap-action">
          <Icon name="agents" size={14} className="ap-meta-icon" />
          <code title={`${approval.agent}.${approval.capability}`}>
            {approval.agent}.{approval.capability}
          </code>
        </span>{" "}
        · requested <Time iso={approval.created_at} /> · <Link to={`/tasks?id=${approval.task_id}`}>task #{approval.task_id}</Link>
      </p>
      {approval.details.capability_description ? <p className="ap-description">{approval.details.capability_description}</p> : null}
      {approval.details.target ? (
        <p className="small ap-target">
          <Icon name="server" size={16} className="ap-target-icon" />
          <span>
            Target: <strong>{approval.details.target}</strong> <span className="muted">({approval.details.environment})</span>
          </span>
        </p>
      ) : null}
      <details className="ap-params">
        <summary>
          <Icon name="chevronRight" size={14} className="ap-chevron" /> Exact parameters
        </summary>
        <JsonView value={approval.details.params ?? {}} />
      </details>
      {approval.status === "pending" ? (
        canOperate ? (
          <div className="decision ap-decision">
            <input aria-label="Decision note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} />
            {production ? (
              user.role === "admin" ? (
                <input
                  aria-label="Type the name to confirm"
                  placeholder={`type ${confirmText} to approve`}
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                />
              ) : (
                <span className="small muted">Production change: an admin must approve it.</span>
              )
            ) : null}
            <button
              className="btn primary"
              disabled={busy || (production && (user.role !== "admin" || confirm !== confirmText))}
              onClick={() => void decide(true)}
            >
              <Icon name="check" size={16} /> Approve
            </button>
            <button className="btn danger" disabled={busy} onClick={() => void decide(false)}>
              <Icon name="close" size={16} /> Reject
            </button>
          </div>
        ) : (
          <p className="muted ap-note">
            <Icon name="lock" size={15} /> An operator or admin must decide.
          </p>
        )
      ) : (
        <p className="ap-decided">
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
  // Tiles summarise the pending approvals in the list already loaded. History holds the newest 100 of every status,
  // so when it is full an older pending approval could sit outside it and the tiles say so.
  const pending = list.data ? list.data.approvals.filter((a) => a.status === "pending") : null;
  const partial = tab === "all" && (list.data?.approvals.length ?? 0) >= 100;
  const productionPending = pending?.filter((a) => a.details.production === true) ?? [];
  const changes = pending?.filter((a) => a.risk === "change" || a.risk === "destructive").length ?? 0;
  const outbound = pending?.filter((a) => a.risk === "external").length ?? 0;
  const spending = pending?.filter((a) => a.risk === "financial").length ?? 0;
  const oldest = pending?.reduce<Approval | null>((o, a) => (!o || a.created_at < o.created_at ? a : o), null) ?? null;
  const value = (n: number | undefined) => <span className="tile-value tabular">{n == null ? "—" : n}</span>;
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Approvals</h1>
          <p className="muted">
            Actions that would reach the outside world, change a website or server, spend money or delete something wait here. Approving runs exactly the
            parameters shown. Production changes need an admin who types the target's name. Email, publishing and ad changes are mocks in this build (recorded,
            not sent); server and WordPress repairs are real and go through the restricted cc-gate.
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
      <div className="tiles ap-tiles">
        <Tile
          icon="approvals"
          tone={!pending ? "muted" : pending.length ? "warn" : "green"}
          label="Waiting for a decision"
          hint={
            !pending ? (
              list.error ? "The list did not load" : "Loading…"
            ) : partial ? (
              "Among the newest 100 approvals; Pending lists them all"
            ) : oldest ? (
              <>
                Oldest requested <Time iso={oldest.created_at} />
              </>
            ) : (
              "The queue is clear"
            )
          }
        >
          {value(pending?.length)}
        </Tile>
        <Tile
          icon="shield"
          tone={productionPending.length ? "bad" : "purple"}
          label="Production changes"
          hint={
            productionPending.length ? (
              <span className="ap-clip" title={productionPending.map((a) => a.details.target || a.summary).join("\n")}>
                {productionPending.map((a) => a.details.target || `#${a.id}`).join(" · ")}
              </span>
            ) : (
              "An admin approves these by typing the target's name"
            )
          }
        >
          {value(pending ? productionPending.length : undefined)}
        </Tile>
        <Tile icon="server" tone="cyan" label="Site and server changes" hint={pending ? `${outbound} to send or publish · ${spending} to spend money` : undefined}>
          {value(pending ? changes : undefined)}
        </Tile>
      </div>
      <ErrorNote error={list.error} />
      {list.data?.approvals.length ? (
        <div className="ap-list">
          {list.data.approvals.map((a) => (
            <ApprovalCard key={a.id} approval={a} onDecided={() => void list.refresh()} />
          ))}
        </div>
      ) : (
        <div className="card ap-empty">
          <span className="ap-empty-icon" aria-hidden>
            <Icon name={tab === "pending" ? "check" : "history"} size={22} />
          </span>
          <Empty>{tab === "pending" ? "Nothing is waiting for you." : "No approvals yet."}</Empty>
        </div>
      )}
    </div>
  );
}
