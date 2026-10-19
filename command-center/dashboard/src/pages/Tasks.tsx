import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { TaskDetail, hidesTaskDetail } from "../work";
import "./Tasks.css";

const STATUSES = ["", "queued", "running", "awaiting_approval", "succeeded", "failed", "rejected", "cancelled"];

export default function Tasks() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const [agent, setAgent] = useState("");
  const query = new URLSearchParams({ limit: "100" });
  if (status) query.set("status", status);
  if (agent) query.set("agent", agent);
  const list = usePoll(() => api.get<{ tasks: Task[]; total: number }>(`/api/tasks?${query}`), 5_000, [status, agent]);
  const openId = params.get("id") ? Number(params.get("id")) : null;
  // Tiles count the tasks the list already loaded (the newest 100 that match the filters) and say when that is not all of them.
  const shown = list.data?.tasks ?? null;
  const count = (...statuses: string[]) => (shown ? shown.filter((t) => statuses.includes(t.status)).length : undefined);
  const scope = (
    <>
      {agent ? (
        <>
          agent <code>{agent}</code>
        </>
      ) : (
        "all agents"
      )}
      {status ? ` · ${status.replace(/_/g, " ")} only` : ""}
    </>
  );
  const loaded = list.data;
  const tasksWord = loaded ? `${loaded.total.toLocaleString()} task${loaded.total === 1 ? "" : "s"}` : "";
  const basis = !loaded ? (
    list.error ? "The list did not load" : "Loading…"
  ) : (
    <>
      {loaded.total > loaded.tasks.length ? `Newest ${loaded.tasks.length} of ${tasksWord}` : `All ${tasksWord}`} · {scope}
    </>
  );
  const value = (n: number | undefined) => <span className="tile-value tabular">{n == null ? "—" : n.toLocaleString()}</span>;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Tasks</h1>
          <p className="muted">Every unit of agent work: what ran, who asked for it, what it found.</p>
        </div>
      </header>

      <div className="tiles cols-4 tk-tiles">
        <Tile icon="play" tone="blue" label="Running or queued" hint={basis}>
          {value(count("running", "queued"))}
        </Tile>
        <Tile icon="approvals" tone={count("awaiting_approval") ? "warn" : "orange"} label="Awaiting approval" to="/approvals" hint={basis}>
          {value(count("awaiting_approval"))}
        </Tile>
        <Tile icon="alert" tone={count("failed") ? "bad" : "muted"} label="Failed" hint={basis}>
          {value(count("failed"))}
        </Tile>
        <Tile icon="check" tone="green" label="Succeeded" hint={basis}>
          {value(count("succeeded"))}
        </Tile>
      </div>

      <Card
        className="tk-card"
        title={
          <>
            <Icon name="tasks" size={18} className="card-icon" /> {list.data ? `${list.data.total} task(s)` : "Tasks"}
          </>
        }
        actions={
          <div className="filters tk-filters">
            <select aria-label="Status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s ? s.replace(/_/g, " ") : "all statuses"}
                </option>
              ))}
            </select>
            <span className="tk-search">
              <Icon name="search" size={16} className="tk-search-icon" />
              <input aria-label="Agent filter" placeholder="agent" value={agent} onChange={(e) => setAgent(e.target.value.trim())} />
            </span>
          </div>
        }
      >
        <ErrorNote error={list.error} />
        {list.data?.tasks.length ? (
          <div className="table-wrap">
            <table className="table tk-table">
              <thead>
                <tr>
                  <th className="tk-id">#</th>
                  <th>Action</th>
                  <th>Status</th>
                  <th>Risk</th>
                  <th>Created</th>
                  <th className="tk-summary">Summary</th>
                </tr>
              </thead>
              <tbody>
                {list.data.tasks.map((t) => {
                  // A credit-repair task's summary and error stay hidden from users without the sensitive-data permission.
                  const hidden = hidesTaskDetail(t, user);
                  const summary = hidden ? "—" : t.error || t.summary;
                  const open = () => setParams({ id: String(t.id) });
                  return (
                    <tr key={t.id} className="clickable" onClick={open}>
                      <td className="tk-id tabular">{t.id}</td>
                      <td className="tk-action">
                        {/* The row opens on a click; this button is the keyboard path to the same dialog. */}
                        <button
                          type="button"
                          className="link-button tk-open"
                          onClick={(e) => {
                            e.stopPropagation();
                            open();
                          }}
                        >
                          <code className="tk-cap" title={`${t.agent}.${t.capability}`}>
                            {t.capability}
                          </code>
                        </button>
                        <span className="tk-agent">{t.agent}</span>
                      </td>
                      <td className="tk-status">
                        <Pill value={t.status} />
                      </td>
                      <td className="tk-risk">
                        <RiskBadge risk={t.risk} />
                      </td>
                      <td className="tk-when small">
                        <Time iso={t.created_at} />
                        <span className="tk-source">via {t.source}</span>
                      </td>
                      <td className={`tk-summary small ${t.error && !hidden ? "text-bad" : ""}`}>
                        <span className="tk-clip" title={summary}>
                          {summary}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No tasks match.</Empty>
        )}
      </Card>
      {openId ? <TaskDetail id={openId} onClose={() => setParams({})} /> : null}
    </div>
  );
}
