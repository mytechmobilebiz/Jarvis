import { api, type AuditEntry } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Time } from "../components";
import { usePoll } from "../hooks";

export default function Activity() {
  const { user } = useAuth();
  const list = usePoll(() => (user.role === "admin" ? api.get<{ entries: AuditEntry[] }>("/api/audit?limit=200") : Promise.resolve({ entries: [] })), 15_000);
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Activity log</h1>
          <p className="muted">Who signed in, changed a setting, requested work or decided an approval.</p>
        </div>
      </header>
      {user.role !== "admin" ? (
        <p className="muted">Only admins can read the activity log.</p>
      ) : (
        <Card>
          <ErrorNote error={list.error} />
          {list.data?.entries.length ? (
            <table className="table compact">
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
                    <td>
                      <Time iso={e.at} />
                    </td>
                    <td>{e.actor}</td>
                    <td>
                      <code>{e.action}</code>
                    </td>
                    <td>
                      {e.target_type} {e.target_id}
                    </td>
                    <td className="truncate small">{Object.keys(e.detail).length ? JSON.stringify(e.detail) : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <Empty>Nothing logged yet.</Empty>
          )}
        </Card>
      )}
    </div>
  );
}
