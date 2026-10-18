import { useState } from "react";
import { api, type Artifact } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, JsonView, Markdown, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";

function ArtifactView({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { canOperate } = useAuth();
  const detail = usePoll(() => api.get<{ artifact: Artifact }>(`/api/artifacts/${id}`), 0, [id]);
  const [error, setError] = useState<string | null>(null);
  const a = detail.data?.artifact;

  async function mark(status: string) {
    setError(null);
    try {
      await api.patch(`/api/artifacts/${id}`, { status });
      await detail.refresh();
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <Modal title={a?.title ?? `Draft #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error ?? error} />
      {a ? (
        <>
          <p className="muted small">
            {a.kind} by {a.agent} · <Time iso={a.created_at} /> · <Pill value={a.status} />
          </p>
          {canOperate ? (
            <div className="row-actions">
              <button className="btn small" onClick={() => void mark("approved")}>
                Mark reviewed
              </button>
              <button className="btn small" onClick={() => void mark("rejected")}>
                Reject
              </button>
              <button className="btn small" onClick={() => void mark("archived")}>
                Archive
              </button>
              <span className="muted small">Marking a draft reviewed does not publish or send it.</span>
            </div>
          ) : null}
          {a.content_type === "text/html" ? (
            // Rendered with no scripts, forms or same-origin access.
            <iframe className="html-preview" title={a.title} sandbox="" srcDoc={a.content ?? ""} />
          ) : a.content_type === "application/json" ? (
            <JsonView value={(() => { try { return JSON.parse(a.content ?? "null"); } catch { return a.content; } })()} />
          ) : (
            <Markdown text={a.content ?? ""} />
          )}
        </>
      ) : (
        <p className="muted">Loading…</p>
      )}
    </Modal>
  );
}

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
