// Workflow Activity panel: tasks finished per category from /api/stats/task-activity, with a range menu.
import type { ActivityRange, TaskActivity } from "../api";
import WorkflowActivityChart, { RANGE_LABEL } from "../charts/WorkflowActivityChart";
import PanelError from "./PanelError";

export const RANGES: ActivityRange[] = ["24h", "7d", "30d"];

export default function WorkflowActivity({
  data,
  error,
  range,
  onRange,
}: {
  data: TaskActivity | null;
  error: string | null;
  range: ActivityRange;
  onRange: (r: ActivityRange) => void;
}) {
  // While another range loads, the previous chart stays, dimmed, so nothing jumps.
  const current = data && data.range === range;
  return (
    <section className="dash-panel activity-panel" aria-labelledby="activity-title">
      <div className="dash-panel-head">
        <div>
          <h2 id="activity-title">Workflow Activity</h2>
          <p className="dash-panel-sub">Tasks finished by the agents, by kind of work.</p>
        </div>
        <label className="range-select">
          <span className="sr-only">Time range</span>
          <select value={range} onChange={(e) => onRange(e.target.value as ActivityRange)}>
            {RANGES.map((r) => (
              <option key={r} value={r}>
                {RANGE_LABEL[r].charAt(0).toUpperCase() + RANGE_LABEL[r].slice(1)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {error ? <PanelError error={`Activity unavailable: ${error}`} /> : null}
      {data ? <WorkflowActivityChart data={data} dim={!current} /> : !error ? <p className="empty">Loading activity…</p> : null}
    </section>
  );
}
