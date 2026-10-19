import { ReactNode, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Approval, type User } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, JsonView, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { mockLabel } from "./shared";
import "./Approvals.css";

/** The most the API returns in one list. */
const LIMIT = 500;

/** Who or what asked for the change (0.5.0 API: approval.details.requested, C3-08). */
export interface Requested {
  source: string;
  by: string;
  request_id: number | null;
  request_source: string | null;
  requested_by: string | null;
  message: string | null;
  schedule: string | null;
  retry_of: number | null;
}

/** The draft an email or publish approval would send (0.5.0 API: approval.details.draft, C3-02, C3-03). */
export interface DraftFacts {
  artifact_id: number;
  kind: string;
  title: string;
  status: string;
  sha256: string;
  excerpt: string;
}

/** approval.details as the 0.5.0 API fills it; every field is optional because older approvals lack them. */
export type ApprovalDetails = Approval["details"] & {
  requested?: Requested;
  draft?: DraftFacts;
  /** "mock" or "live": how the integration that would carry it is set up. */
  delivery?: "mock" | "live" | "unknown";
  /** The exact version or address a repair will apply, from the latest server-side check (C3-07). */
  expected?: { summary?: string; checked_at?: string | null };
  binding?: Record<string, unknown> | null;
};

const EMAIL_SENDS = new Set(["send_reply", "send_proposal"]);
const REQUEST_SOURCE: Record<string, string> = { n8n: "n8n, with the service token", dashboard: "dashboard" };

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** "You", a person, the AI Supervisor for request #N (and where that came from), schedule X, or a retry of task #M. */
export function requestedBy(r: Requested | undefined, me: Pick<User, "email">): ReactNode {
  if (!r) return null;
  const who = (name: string) => (name && name === me.email ? `you (${name})` : name || "unknown");
  if (r.source === "supervisor" && r.request_id != null) {
    const origin = r.request_source ? REQUEST_SOURCE[r.request_source] ?? r.request_source : null;
    return (
      <>
        AI Supervisor from <Link to={`/supervisor?id=${r.request_id}`}>request #{r.request_id}</Link>
        {origin ? ` (${origin})` : ""}
        {r.requested_by ? `, asked by ${who(r.requested_by)}` : ""}
        {r.message ? <span className="ap-quote">“{r.message}”</span> : null}
      </>
    );
  }
  if (r.source === "schedule") return `Schedule ${r.schedule ?? r.by.replace(/^schedule:/, "")}`;
  if (r.source === "retry" && r.retry_of != null)
    return (
      <>
        Retry of <Link to={`/tasks?id=${r.retry_of}`}>task #{r.retry_of}</Link>, by {who(r.by)}
      </>
    );
  if (r.source === "workflow") return `n8n workflow (${r.by || "service token"})`;
  return who(r.by);
}

/** The rows that say exactly what would happen and to what. */
function Facts({ approval, me }: { approval: Approval; me: User }) {
  const d = approval.details as ApprovalDetails;
  const b = d.binding ?? {};
  const rows: [string, ReactNode][] = [];
  const requested = requestedBy(d.requested, me);
  rows.push(["Requested by", requested ?? <span className="muted">Not recorded (requested before version 0.5.0)</span>]);
  const email = EMAIL_SENDS.has(approval.capability);
  if (email && str(b.recipient)) {
    rows.push(["To", <strong>{str(b.recipient)}</strong>]);
    rows.push(["Subject", str(b.subject) || "—"]);
  } else if (approval.capability === "publish_draft" && str(b.url)) {
    rows.push([
      "Site",
      <>
        <strong>{str(b.name)}</strong> <span className="ap-url">{str(b.url)}</span>
      </>,
    ]);
  } else if (approval.capability === "publish_draft" && str(b.platform)) {
    rows.push(["Platform", <strong>{str(b.platform)}</strong>]);
    rows.push(["Account", str(b.account) || "—"]);
  } else if (d.target) {
    rows.push(["Target", <strong>{d.target}</strong>]);
  }
  // For sends and posts "environment" repeats the delivery note, shown as Delivery below.
  if (d.environment && !email && !/^(mock|live|unknown)\b/.test(d.environment))
    rows.push(["Environment", d.production ? <strong>production</strong> : d.environment]);
  if (d.expected?.summary)
    rows.push([
      "Will apply",
      <>
        {d.expected.summary}
        {d.expected.checked_at ? (
          <span className="muted">
            {" "}
            (from the server-side check <Time iso={d.expected.checked_at} />)
          </span>
        ) : null}
      </>,
    ]);
  if (d.draft)
    rows.push([
      "Draft",
      <>
        <Link to={`/drafts?id=${d.draft.artifact_id}`}>
          #{d.draft.artifact_id} {d.draft.title}
        </Link>{" "}
        <span className="muted small">({d.draft.status}; any change to it after this request refuses the action)</span>
        {d.draft.excerpt ? <blockquote className="ap-excerpt">{d.draft.excerpt}</blockquote> : null}
      </>,
    ]);
  if (d.delivery === "mock") {
    const integration = email ? "email" : approval.agent === "social_marketing" ? "postonomous" : "wordpress_publish";
    rows.push(["Delivery", <Pill value="mock" label={mockLabel(integration)} />]);
  } else if (d.delivery === "live") {
    rows.push(["Delivery", <Pill value="live" label="Live: really carried out once approved" />]);
  }
  return (
    <dl className="ap-facts">
      {rows.map(([k, v]) => (
        <div key={k} className="ap-fact">
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
      {!d.target && !d.draft ? (
        <div className="ap-fact">
          <dt>Target</dt>
          <dd className="muted">No target recorded for this action: check the exact parameters below before approving.</dd>
        </div>
      ) : null}
    </dl>
  );
}

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
      <Facts approval={approval} me={user} />
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
                  aria-label={`Type ${confirmText} to confirm this production change`}
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
  const list = usePoll(() => api.get<{ approvals: Approval[] }>(`/api/approvals?status=${tab}&limit=${LIMIT}`), 5_000, [tab]);
  // Tiles summarise the pending approvals in the list already loaded, which holds the newest LIMIT (pending ones, or
  // of every status on History). When it is full an older pending approval could sit outside it and the tiles say so.
  const pending = list.data ? list.data.approvals.filter((a) => a.status === "pending") : null;
  const partial = (list.data?.approvals.length ?? 0) >= LIMIT;
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
            Actions that would reach the outside world, change a website or server, spend money or delete something wait here. Each card says who asked,
            the exact target and, for email and publishing, whether the channel is a mock (recorded, not sent) or live. Approving runs exactly the parameters
            shown. Production changes need an admin who types the target's name. Server and WordPress repairs are real and go through the restricted cc-gate.
          </p>
        </div>
        <div className="tabs" role="group" aria-label="Approvals view">
          <button type="button" className={tab === "pending" ? "active" : ""} aria-pressed={tab === "pending"} onClick={() => setTab("pending")}>
            Pending
          </button>
          <button type="button" className={tab === "all" ? "active" : ""} aria-pressed={tab === "all"} onClick={() => setTab("all")}>
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
              `Among the newest ${LIMIT}${tab === "all" ? " approvals of every status" : " pending"}`
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
