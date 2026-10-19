import { useState } from "react";
import { api, type OutboxRecord } from "../api";
import { Card, Empty, ErrorNote, JsonView, Modal, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import "./Outbox.css";

/** The most the API returns in one list; when the list is that long the tiles count the latest entries, not all. */
const LIMIT = 500;

export default function Outbox() {
  const list = usePoll(() => api.get<{ entries: OutboxRecord[] }>(`/api/outbox?limit=${LIMIT}`), 8_000);
  const [open, setOpen] = useState<OutboxRecord | null>(null);

  // Summary of the same list the table shows.
  const all = list.data?.entries ?? [];
  const loaded = !!list.data;
  const pending = list.error ?? "…";
  const mock = all.filter((e) => e.mode === "mock").length;
  const live = all.filter((e) => e.mode === "live").length;
  const integrations = new Set(all.map((e) => e.integration)).size;
  const capped = all.length >= LIMIT;
  const windowNote = capped ? ` · in the latest ${LIMIT} entries` : "";
  const latest = all.reduce<string | null>((max, e) => (max === null || new Date(e.created_at).getTime() > new Date(max).getTime() ? e.created_at : max), null);

  return (
    <div className="page ob-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Communications</div>
          <h1>Outbox</h1>
          <p className="muted">
            Every outward action that was approved: emails, posts, budget changes, letters. In this build all of these integrations are <strong>mocks</strong>,
            so each entry is a record of what would have been sent. Nothing here left the machine.
          </p>
        </div>
      </header>

      <div className="tiles">
        <Tile
          icon="send"
          tone="blue"
          label="Entries"
          hint={
            loaded ? (
              latest ? (
                <>
                  {integrations} integration{integrations === 1 ? "" : "s"} · latest <Time iso={latest} />
                  {windowNote}
                </>
              ) : (
                "No approved outward action yet"
              )
            ) : (
              pending
            )
          }
        >
          <span className="tile-value tabular">{loaded ? all.length : "—"}</span>
        </Tile>
        <Tile icon="file" tone="cyan" label="Mock, recorded only" hint={loaded ? `Recorded what would have been sent; nothing was delivered${windowNote}` : pending}>
          <span className="tile-value tabular">{loaded ? mock : "—"}</span>
          {loaded && all.length ? <span className="tile-of">{Math.round((mock / all.length) * 100)}%</span> : null}
        </Tile>
        <Tile icon="external" tone={live ? "orange" : "muted"} label="Live, really sent" hint={loaded ? `Delivered through a live integration${windowNote}` : pending}>
          <span className="tile-value tabular">{loaded ? live : "—"}</span>
          {loaded && all.length ? <span className="tile-of">{Math.round((live / all.length) * 100)}%</span> : null}
        </Tile>
      </div>

      <ErrorNote error={list.error} />
      <Card
        title={
          <>
            <Icon name="send" size={18} className="card-icon" /> Approved outward actions
          </>
        }
      >
        {list.data?.entries.length ? (
          <div className="table-wrap">
            <table className="table ob-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Action</th>
                  <th>To</th>
                  <th>Subject</th>
                  <th>Approved by</th>
                  <th>Result</th>
                  <th>When</th>
                </tr>
              </thead>
              <tbody>
                {list.data.entries.map((e) => (
                  <tr key={e.id} className="clickable" onClick={() => setOpen(e)}>
                    <td className="muted tabular">{e.id}</td>
                    <td>
                      <div className="ob-action">
                        {/* The row opens on a click; this button is the keyboard path to the same dialog. */}
                        <button
                          type="button"
                          className="link-button"
                          onClick={(ev) => {
                            ev.stopPropagation();
                            setOpen(e);
                          }}
                        >
                          <code>
                            {e.integration}.{e.action}
                          </code>
                        </button>{" "}
                        <Pill value={e.mode} label={e.mode.toUpperCase()} />
                      </div>
                    </td>
                    <td className="small">
                      <span className="ob-clip" title={e.recipient}>
                        {e.recipient}
                      </span>
                    </td>
                    <td className="small">
                      <span className="ob-subject" title={e.subject}>
                        {e.subject}
                      </span>
                    </td>
                    <td className="small">
                      <span className="ob-clip" title={e.approved_by}>
                        {e.approved_by}
                      </span>
                    </td>
                    <td className="small ob-result">{e.result}</td>
                    <td className="ob-nowrap">
                      <Time iso={e.created_at} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>Nothing yet. Approved emails, posts and other outward actions appear here.</Empty>
        )}
      </Card>
      {open ? (
        <Modal title={`Outbox #${open.id}: ${open.integration}.${open.action}`} onClose={() => setOpen(null)} wide>
          <p className="ob-detail-head">
            <Pill value={open.mode} label={open.mode.toUpperCase()} /> {open.result} · approved by {open.approved_by} · task #{open.task_id}
          </p>
          <dl className="kv">
            <dt>To</dt>
            <dd>{open.recipient}</dd>
            <dt>Subject</dt>
            <dd>{open.subject || "—"}</dd>
          </dl>
          {open.body ? <pre className="plain boxed">{open.body}</pre> : null}
          <JsonView value={open.payload} />
        </Modal>
      ) : null}
    </div>
  );
}
