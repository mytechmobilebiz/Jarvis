import { useState } from "react";
import { api, type Artifact } from "../api";
import { Card, Empty, ErrorNote, Pill, Time } from "../components";
import { usePoll } from "../hooks";
import { ArtifactView } from "../work";

export default function Drafts() {
  const [kind, setKind] = useState("");
  const list = usePoll(() => api.get<{ artifacts: Artifact[] }>(`/api/artifacts?limit=100${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`), 10_000, [kind]);
  const [openId, setOpenId] = useState<number | null>(null);
  const kinds = Array.from(new Set((list.data?.artifacts ?? []).map((a) => a.kind)));
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Drafts &amp; reports</h1>
          <p className="muted">Briefings, audits, proposals and content the agents wrote. Nothing here has been published or sent.</p>
        </div>
        <div className="filters">
          <select aria-label="Kind filter" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">all kinds</option>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </div>
      </header>
      <Card>
        <ErrorNote error={list.error} />
        {list.data?.artifacts.length ? (
          <table className="table">
            <thead>
              <tr>
                <th>Title</th>
                <th>Kind</th>
                <th>Agent</th>
                <th>Status</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {list.data.artifacts.map((a) => (
                <tr key={a.id} className="clickable" onClick={() => setOpenId(a.id)}>
                  <td>{a.title}</td>
                  <td>{a.kind}</td>
                  <td>{a.agent}</td>
                  <td>
                    <Pill value={a.status} />
                  </td>
                  <td>
                    <Time iso={a.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No drafts yet. Ask the supervisor for a daily briefing to create one.</Empty>
        )}
      </Card>
      {openId ? <ArtifactView id={openId} onClose={() => setOpenId(null)} onChanged={() => void list.refresh()} /> : null}
    </div>
  );
}
