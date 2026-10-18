import { useState } from "react";
import { api, type OutboxRecord } from "../api";
import { Card, Empty, ErrorNote, JsonView, Modal, Pill, Time } from "../components";
import { usePoll } from "../hooks";

export default function Outbox() {
  const list = usePoll(() => api.get<{ entries: OutboxRecord[] }>("/api/outbox"), 8_000);
  const [open, setOpen] = useState<OutboxRecord | null>(null);
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Outbox</h1>
          <p className="muted">
            Every outward action that was approved: emails, posts, budget changes, letters. In this build all of these integrations are <strong>mocks</strong>,
            so each entry is a record of what would have been sent. Nothing here left the machine.
          </p>
        </div>
      </header>
      <ErrorNote error={list.error} />
      <Card>
        {list.data?.entries.length ? (
          <table className="table">
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
                  <td>{e.id}</td>
                  <td>
                    <code>
                      {e.integration}.{e.action}
                    </code>{" "}
                    <Pill value={e.mode} label={e.mode.toUpperCase()} />
                  </td>
                  <td className="small">{e.recipient}</td>
                  <td className="truncate small">{e.subject}</td>
                  <td className="small">{e.approved_by}</td>
                  <td className="small">{e.result}</td>
                  <td>
                    <Time iso={e.created_at} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>Nothing yet. Approved emails, posts and other outward actions appear here.</Empty>
        )}
      </Card>
      {open ? (
        <Modal title={`Outbox #${open.id}: ${open.integration}.${open.action}`} onClose={() => setOpen(null)} wide>
          <p>
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
