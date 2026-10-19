// Recent Tasks: the latest five tasks, without the AI Supervisor's bookkeeping and, unless asked, without the scheduled
// checks that run every few minutes. A row opens the task's details; "Needs approval" goes to the Approvals page.
import { useState } from "react";
import { Link } from "react-router-dom";
import type { Server, Site, Task, User } from "../api";
import { AgentChip, agentStyle } from "../agentStyle";
import PanelError from "./PanelError";
import { TaskDetail } from "../work";
import { clock, formatDuration } from "./model";
import { isInternal, statusPill, taskDuration, taskText } from "./taskText";

export const RECENT_ROWS = 5;
/** How many of the newest tasks are read to find the rows. */
export const RECENT_WINDOW = 50;

function when(iso: string, now: number): string {
  const d = new Date(iso);
  const today = new Date(now);
  const sameDay = d.toDateString() === today.toDateString();
  return sameDay ? clock(iso) : `${d.toLocaleDateString([], { month: "short", day: "numeric" })}, ${clock(iso)}`;
}

export default function RecentTasks({
  tasks,
  error,
  sites,
  servers,
  user,
  now,
}: {
  tasks: Task[] | null;
  error: string | null;
  sites: Site[];
  servers: Server[];
  user: User;
  now: number;
}) {
  const [scheduled, setScheduled] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const ctx = { sites: new Map(sites.map((s) => [s.id, s])), servers: new Map(servers.map((s) => [s.id, s])), user };
  const work = (tasks ?? []).filter((t) => !isInternal(t));
  const hidden = work.filter((t) => t.schedule_id != null).length;
  const rows = work.filter((t) => scheduled || t.schedule_id == null).slice(0, RECENT_ROWS);

  return (
    <section className="dash-panel tasks-panel" aria-labelledby="tasks-title">
      <div className="dash-panel-head">
        <h2 id="tasks-title">Recent Tasks</h2>
        <div className="dash-panel-actions">
          <button
            type="button"
            className="btn outline small"
            aria-pressed={scheduled}
            onClick={() => setScheduled((s) => !s)}
            title={`Checks that schedules start every few minutes, among the latest ${RECENT_WINDOW} tasks`}
          >
            Scheduled checks{hidden && !scheduled ? ` (${hidden} hidden)` : ""}
          </button>
          <Link className="btn outline small" to="/tasks">
            All tasks
          </Link>
        </div>
      </div>
      <PanelError error={error} />
      {tasks === null ? (
        !error ? <p className="empty">Loading tasks…</p> : null
      ) : rows.length ? (
        <div className="recent-wrap">
          <table className="recent-table" role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Time</th>
                <th role="columnheader" scope="col">Agent</th>
                <th role="columnheader" scope="col">Task</th>
                <th role="columnheader" scope="col">Status</th>
                <th role="columnheader" scope="col">Duration</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map((t) => {
                const pill = statusPill(t.status);
                const duration = taskDuration(t, now);
                const style = agentStyle(t.agent);
                return (
                  <tr key={t.id} role="row">
                    <td role="cell" className="recent-time tabular">
                      <time dateTime={t.created_at} title={new Date(t.created_at).toLocaleString()}>
                        {when(t.created_at, now)}
                      </time>
                    </td>
                    <td role="cell" className="recent-agent">
                      <AgentChip name={t.agent} />
                      <span>{style.short.replace(/ Agent$/, "")}</span>
                    </td>
                    <td role="cell" className="recent-task">
                      <button type="button" className="link-button" onClick={() => setOpen(t.id)} title={`Task #${t.id}: open the details`}>
                        {taskText(t, ctx)}
                      </button>
                    </td>
                    <td role="cell" className="recent-status">
                      {t.status === "awaiting_approval" ? (
                        <Link className={`solid-pill pill-${pill.tone}`} to="/approvals" title="Open the Approvals page">
                          {pill.label}
                        </Link>
                      ) : (
                        <span className={`solid-pill pill-${pill.tone}`}>{pill.label}</span>
                      )}
                    </td>
                    <td role="cell" className="recent-duration tabular">
                      {duration === null ? <span aria-label="not started">—</span> : formatDuration(duration)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="empty">
          {hidden ? `Only scheduled checks among the latest ${RECENT_WINDOW} tasks.` : "No tasks yet. Run a quick action or type a command above."}
        </p>
      )}
      {open !== null ? <TaskDetail id={open} onClose={() => setOpen(null)} /> : null}
    </section>
  );
}
