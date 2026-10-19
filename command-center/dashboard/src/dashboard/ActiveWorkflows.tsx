// Active Workflows: the real schedules (operators can pause or resume them: an internal, audited change; pausing asks
// first), then the n8n workflows read-only. Nothing here can switch on a workflow that publishes or sends.
import { useState } from "react";
import { api, type Integration, type Schedule } from "../api";
import { AgentChip, humanize } from "../agentStyle";
import { ErrorNote, Modal } from "../components";
import PanelError from "./PanelError";
import { Icon } from "../icons";
import { ago, span } from "./model";

function nextRun(s: Schedule, now: number): string {
  if (!s.enabled) return "Paused";
  const next = s.next_run_at ? Date.parse(s.next_run_at) : NaN;
  if (Number.isNaN(next)) return s.last_run_at ? `Last ran ${ago(now - Date.parse(s.last_run_at))}` : "Not run yet";
  const wait = next - now;
  return wait <= 30_000 ? "Due now" : `Next run in ${span(wait)}`;
}

/** Schedules whose pause stops automatic monitoring of websites or servers. */
function monitors(s: Schedule): boolean {
  return s.agent === "monitoring" || /scan|check|health|uptime/.test(`${s.name} ${s.capability}`);
}

export default function ActiveWorkflows({
  schedules,
  schedulesError,
  onChanged,
  integrations,
  canOperate,
  editorUrl,
  now,
}: {
  schedules: Schedule[] | null;
  schedulesError: string | null;
  onChanged: () => Promise<void>;
  integrations: Integration[] | null;
  canOperate: boolean;
  /** The n8n editor link (admins only), or null. */
  editorUrl: string | null;
  now: number;
}) {
  const [override, setOverride] = useState<Record<number, boolean>>({});
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Schedule | null>(null);
  const engine = integrations?.find((i) => i.name === "n8n");

  async function setEnabled(s: Schedule, enabled: boolean) {
    setConfirm(null);
    setError(null);
    setBusy(s.id);
    setOverride((o) => ({ ...o, [s.id]: enabled })); // shown at once; put back if the change fails
    try {
      await api.patch(`/api/schedules/${s.id}`, { enabled });
      await onChanged();
    } catch (err) {
      setError(`Could not ${enabled ? "resume" : "pause"} ${humanize(s.name)}: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setOverride((o) => {
        const rest = { ...o };
        delete rest[s.id];
        return rest;
      });
      setBusy(null);
    }
  }

  return (
    <section className="dash-panel flows-panel" aria-labelledby="flows-title">
      <div className="dash-panel-head">
        <h2 id="flows-title">Active Workflows</h2>
        {editorUrl ? (
          <a className="btn outline small" href={editorUrl} target="_blank" rel="noopener noreferrer" title="Opens the n8n editor on this PC (new tab)">
            View in n8n <Icon name="external" size={13} />
          </a>
        ) : null}
      </div>
      <PanelError error={schedulesError} />
      <ErrorNote error={error} />
      {schedules === null ? (
        !schedulesError ? <p className="empty">Loading schedules…</p> : null
      ) : (
        <ul className="flow-list" aria-label="Schedules">
          {schedules.length ? null : <li className="flow-empty">No scheduled automations</li>}
          {schedules.map((s) => {
            const on = override[s.id] ?? s.enabled;
            const title = humanize(s.name);
            return (
              <li key={s.id} title={s.description || undefined}>
                <AgentChip name={s.agent} />
                <span className="flow-name">{title}</span>
                <span className="flow-when">{busy === s.id ? "Saving…" : nextRun({ ...s, enabled: on }, now)}</span>
                <button
                  type="button"
                  role="switch"
                  aria-checked={on}
                  aria-label={`Run ${title} on schedule`}
                  className={`flow-switch ${on ? "on" : ""}`}
                  disabled={!canOperate || busy === s.id}
                  title={canOperate ? undefined : "Only operators and admins can pause or resume schedules"}
                  onClick={() => (on ? setConfirm(s) : void setEnabled(s, true))}
                >
                  <span className="flow-switch-thumb" aria-hidden />
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {engine && engine.mode !== "disabled" ? (
        engine.workflows ? (
          <ul className="flow-list flow-n8n" aria-label="n8n workflows (change them in n8n)">
            {engine.workflows.map((w) => (
              <li key={w.id} title="Change this in n8n">
                <span className="agent-chip n8n-chip" aria-hidden>
                  <Icon name="workflows" size={14} />
                </span>
                <span className="flow-name">{w.name}</span>
                <span className="flow-when">n8n</span>
                <span className={`flow-switch read-only ${w.active ? "on" : ""}`} aria-hidden>
                  <span className="flow-switch-thumb" />
                </span>
                <span className="sr-only">{w.active ? "Active" : "Inactive"} in n8n</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="flow-note">{engine.workflow_error ? `n8n workflows unavailable: ${engine.workflow_error}` : "Add N8N_API_KEY to list n8n workflows"}</p>
        )
      ) : null}
      {confirm ? (
        <Modal title={`Pause ${humanize(confirm.name)}?`} onClose={() => setConfirm(null)}>
          <p>{confirm.description || `${humanize(confirm.capability)} on a schedule.`}</p>
          <p>
            It will not run again until someone switches it back on.
            {monitors(confirm) ? " While it is paused, nothing checks for problems automatically." : ""}
          </p>
          <div className="row-actions">
            <button type="button" className="btn danger" onClick={() => void setEnabled(confirm, false)}>
              Pause
            </button>
            <button type="button" className="btn" onClick={() => setConfirm(null)}>
              Keep running
            </button>
          </div>
        </Modal>
      ) : null}
    </section>
  );
}
