import { useState } from "react";
import { api, type Artifact } from "../api";
import { Card, Empty, ErrorNote, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { ArtifactView } from "../work";
import "./Drafts.css";

const LIMIT = 100;

export default function Drafts() {
  const [kind, setKind] = useState("");
  const list = usePoll(() => api.get<{ artifacts: Artifact[] }>(`/api/artifacts?limit=${LIMIT}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}`), 10_000, [kind]);
  const [openId, setOpenId] = useState<number | null>(null);
  const kinds = Array.from(new Set((list.data?.artifacts ?? []).map((a) => a.kind)));

  // Summary of the same list the table shows (the latest LIMIT documents, of the selected kind if one is chosen).
  const all = list.data?.artifacts ?? [];
  const loaded = !!list.data;
  const pending = list.error ?? "…";
  const byStatus = (status: Artifact["status"]) => all.filter((a) => a.status === status).length;
  const perKind = kinds.map((k) => ({ kind: k, count: all.filter((a) => a.kind === k).length })).sort((a, b) => b.count - a.count);
  const scope = `${kind ? `${kind} only` : "all kinds"}${all.length >= LIMIT ? `, latest ${LIMIT}` : ""}`;

  return (
    <div className="page dr-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Content</div>
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

      <div className="tiles">
        <Tile icon="file" tone="blue" label="Awaiting review" hint={loaded ? `Not approved or rejected yet · ${scope}` : pending}>
          <span className="tile-value tabular">{loaded ? byStatus("draft") : "—"}</span>
          {loaded ? <span className="tile-of">of {all.length}</span> : null}
        </Tile>
        <Tile
          icon="check"
          tone="green"
          label="Approved"
          hint={loaded ? `${byStatus("rejected")} rejected · ${byStatus("archived")} archived` : pending}
        >
          <span className="tile-value tabular">{loaded ? byStatus("approved") : "—"}</span>
        </Tile>
        <Tile icon="sparkles" tone="purple" label="By kind" hint={loaded ? (perKind.length ? null : "No documents yet") : pending}>
          <span className="tile-value tabular">{loaded ? perKind.length : "—"}</span>
          {loaded ? <span className="tile-of">{perKind.length === 1 ? "kind" : "kinds"}</span> : null}
          {perKind.length ? (
            <ul className="dr-kinds">
              {perKind.map((k) => (
                <li key={k.kind}>
                  <span className="dr-kind">{k.kind}</span> <span className="tabular">{k.count}</span>
                </li>
              ))}
            </ul>
          ) : null}
        </Tile>
      </div>

      <Card
        title={
          <>
            <Icon name="file" size={18} className="card-icon" /> Documents ({all.length})
          </>
        }
      >
        <ErrorNote error={list.error} />
        {list.data?.artifacts.length ? (
          <div className="table-wrap">
            <table className="table dr-table">
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
                    <td>
                      {/* The row opens on a click; this button is the keyboard path to the same dialog. */}
                      <button
                        type="button"
                        className="link-button dr-open"
                        onClick={(e) => {
                          e.stopPropagation();
                          setOpenId(a.id);
                        }}
                      >
                        <span className="dr-title" title={a.title}>
                          {a.title}
                        </span>
                      </button>
                    </td>
                    <td>
                      <span className="dr-kind">{a.kind}</span>
                    </td>
                    <td className="muted">{a.agent}</td>
                    <td>
                      <Pill value={a.status} />
                    </td>
                    <td className="dr-nowrap">
                      <Time iso={a.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No drafts yet. Ask the supervisor for a daily briefing to create one.</Empty>
        )}
      </Card>
      {openId ? <ArtifactView id={openId} onClose={() => setOpenId(null)} onChanged={() => void list.refresh()} /> : null}
    </div>
  );
}
