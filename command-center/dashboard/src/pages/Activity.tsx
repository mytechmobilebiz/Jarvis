import { api, type AuditEntry } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import "./Activity.css";

/** Colour family of an audit action, from its prefix ("approval.approved" → approval). */
function actionTone(action: string): string {
  const head = action.split(".")[0];
  if (/(deleted|rejected|failed|mismatch)$/.test(action)) return "bad";
  if (head === "auth" || head === "user") return "purple";
  if (head === "approval") return "warn";
  if (head === "task" || head === "supervisor") return "info";
  if (head === "server" || head === "site" || head === "ssh") return "good";
  return "muted";
}

export default function Activity() {
  const { user } = useAuth();
  const list = usePoll(() => (user.role === "admin" ? api.get<{ entries: AuditEntry[] }>("/api/audit?limit=200") : Promise.resolve({ entries: [] })), 15_000);
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Activity log</h1>
          <p className="muted">Who signed in, changed a setting, requested work or decided an approval.</p>
        </div>
      </header>
      {user.role !== "admin" ? (
        <p className="alert alert-info ac-locked">
          <Icon name="lock" size={15} /> Only admins can read the activity log.
        </p>
      ) : (
        <Card
          title={
            <>
              <Icon name="history" size={18} className="card-icon" /> Audit trail
            </>
          }
          actions={list.data?.entries.length ? <span className="muted small">Latest {list.data.entries.length} entries</span> : null}
        >
          <ErrorNote error={list.error} />
          {list.data?.entries.length ? (
            <div className="table-wrap">
              <table className="table compact ac-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Who</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Detail</th>
                  </tr>
                </thead>
                <tbody>
                  {list.data.entries.map((e) => (
                    <tr key={e.id}>
                      <td className="ac-when small">
                        <Time iso={e.at} />
                      </td>
                      <td className="ac-who">
                        <span className="ac-actor" title={e.actor}>
                          <span className="ac-initial" aria-hidden>
                            {(e.actor || "?").charAt(0).toUpperCase()}
                          </span>
                          <span className="ac-actor-name">{e.actor}</span>
                        </span>
                      </td>
                      <td className="ac-nowrap ac-action-cell">
                        <code className={`ac-action ac-tone-${actionTone(e.action)}`}>{e.action}</code>
                      </td>
                      <td className="ac-target small">
                        {e.target_type} {e.target_id}
                      </td>
                      <td className="ac-detail small">
                        <span className="ac-clip mono" title={Object.keys(e.detail).length ? JSON.stringify(e.detail, null, 2) : undefined}>
                          {Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty>Nothing logged yet.</Empty>
          )}
        </Card>
      )}
    </div>
  );
}
