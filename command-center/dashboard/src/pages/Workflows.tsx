import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Agent, type Integration, type Schedule, type SupervisorRequest, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, RiskBadge, Tile, Time, timeAgo } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { TaskDetail } from "../work";
import "./Workflows.css";

const DAY = 86_400_000;
const REQUEST_WINDOW = 50;
/** Names the backend gives its workflow engines (app/workflow.py). */
const ENGINE_NAMES = new Set(["n8n", "webhook", "none"]);
/** Integrations that are not outbound delivery channels. */
const NOT_CHANNELS = new Set([...ENGINE_NAMES, "anthropic"]);

const WORDS: Record<string, string> = { wordpress: "WordPress", seo: "SEO", ai: "AI", n8n: "n8n", cwp: "CWP" };
const DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

/** "wordpress-uptime-sweep" -> "WordPress uptime sweep". */
function humanize(slug: string): string {
  const s = slug
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map((w) => WORDS[w.toLowerCase()] ?? w.toLowerCase())
    .join(" ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * Plain-English reading of the common cron forms, or null when it can't be read reliably.
 * The worker evaluates cron in UTC (croniter on a UTC time), hence the UTC suffix.
 */
function cronHint(cron: string): string | null {
  const f = cron.trim().split(/\s+/);
  if (f.length !== 5) return null;
  const [mi, h, dom, mon, dow] = f;
  const num = (v: string, min: number, max: number) => (/^\d{1,2}$/.test(v) && +v >= min && +v <= max ? +v : null);
  const step = (v: string) => /^\*\/(\d{1,2})$/.exec(v)?.[1];
  const pad = (n: number) => String(n).padStart(2, "0");
  if (mon !== "*") return null;
  if (h === "*" && dom === "*" && dow === "*") {
    if (mi === "*") return "Every minute";
    const n = Number(step(mi));
    // Only steps that divide the hour fire at an even interval.
    if (n >= 1 && 60 % n === 0) return n === 1 ? "Every minute" : `Every ${n} minutes`;
    const m = num(mi, 0, 59);
    if (m !== null) return m === 0 ? "Hourly, on the hour" : `Hourly at :${pad(m)}`;
    return null;
  }
  const m = num(mi, 0, 59);
  if (m === null) return null;
  const hs = Number(step(h));
  if (hs >= 1 && 24 % hs === 0 && dom === "*" && dow === "*") return hs === 1 ? `Hourly at :${pad(m)}` : `Every ${hs} hours at :${pad(m)} UTC`;
  const hr = num(h, 0, 23);
  if (hr === null) return null;
  const at = `${pad(hr)}:${pad(m)} UTC`;
  if (dom === "*" && dow === "*") return `Daily at ${at}`;
  if (dom === "*") {
    if (dow === "1-5") return `Weekdays at ${at}`;
    if (dow === "0,6" || dow === "6,0") return `Weekends at ${at}`;
    const d = num(dow, 0, 7);
    return d === null ? null : `${DAYS[d % 7]} at ${at}`;
  }
  const day = num(dom, 1, 31);
  if (dow === "*" && day !== null) return `Monthly on day ${day} at ${at}`;
  return null;
}

/** Absolute local time, e.g. "Mon, Oct 19, 06:43 AM". */
function when(iso: string): string {
  return new Date(iso).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

type Tone = "good" | "warn" | "bad" | "muted";

/** Engine connection state from its integration record, judged the same way as the sidebar's n8n dot. */
function engineState(e: Integration | undefined): { label: string; tone: Tone } {
  if (!e) return { label: "Unknown", tone: "muted" };
  if (e.mode === "disabled") return { label: "Not connected", tone: "muted" };
  const st = e.status;
  if (!st) return { label: "Not checked yet", tone: "muted" };
  if (st.detail?.reachable === false) return { label: "Unreachable", tone: "bad" };
  if (!st.last_ok_at || (st.last_error_at && st.last_error_at > st.last_ok_at)) return { label: "Needs attention", tone: "warn" };
  return { label: "Connected", tone: "good" };
}

const TILE_TONE: Record<Tone, string> = { good: "green", warn: "warn", bad: "bad", muted: "muted" };

/** Latest task started by each schedule, from each scheduled agent's most recent tasks. */
async function lastScheduledTasks(schedules: Schedule[]): Promise<Record<number, Task>> {
  const agents = [...new Set(schedules.map((s) => s.agent))];
  const lists = await Promise.all(agents.map((a) => api.get<{ tasks: Task[] }>(`/api/tasks?agent=${encodeURIComponent(a)}&limit=100`)));
  const out: Record<number, Task> = {};
  for (const t of lists.flatMap((l) => l.tasks)) {
    if (t.schedule_id != null && (!out[t.schedule_id] || out[t.schedule_id].id < t.id)) out[t.schedule_id] = t;
  }
  return out;
}

function ScheduleSwitch({ schedule, title, busy, onToggle }: { schedule: Schedule; title: string; busy: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={schedule.enabled}
      aria-label={`Run ${title} on schedule`}
      className={`wf-switch ${schedule.enabled ? "on" : ""}`}
      disabled={busy}
      onClick={onToggle}
    >
      <span className="wf-switch-track" aria-hidden>
        <span className="wf-switch-thumb" />
      </span>
      <span className="wf-switch-text" aria-hidden>
        {busy ? "Saving…" : schedule.enabled ? "On" : "Paused"}
      </span>
    </button>
  );
}

export default function Workflows() {
  const { user, canOperate } = useAuth();
  const now = useNow();
  const isAdmin = user.role === "admin";
  const schedules = usePoll(() => api.get<{ schedules: Schedule[] }>("/api/schedules"), 15_000);
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 60_000);
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 30_000);
  const requests = usePoll(
    () => api.get<{ requests: SupervisorRequest[]; total: number }>(`/api/supervisor/requests?limit=${REQUEST_WINDOW}`),
    15_000,
  );
  const scheduleKey = (schedules.data?.schedules ?? []).map((s) => `${s.id}:${s.agent}`).join(",");
  const lastTasks = usePoll(() => lastScheduledTasks(schedules.data?.schedules ?? []), 30_000, [scheduleKey]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [openTask, setOpenTask] = useState<number | null>(null);

  // Same behaviour as the Schedules page: clear the error, patch, refresh, show any failure.
  async function patch(s: Schedule, body: Partial<Schedule>) {
    setError(null);
    setBusy(s.id);
    try {
      await api.patch(`/api/schedules/${s.id}`, body);
      await schedules.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  const agentByName = useMemo(() => new Map((agents.data?.agents ?? []).map((a) => [a.name, a])), [agents.data]);
  const list = schedules.data?.schedules ?? [];
  const enabled = list.filter((s) => s.enabled);
  const lastRun = list.reduce<string | null>((acc, s) => (s.last_run_at && (!acc || s.last_run_at > acc) ? s.last_run_at : acc), null);
  const next = enabled
    .filter((s) => s.next_run_at)
    .sort((a, b) => new Date(a.next_run_at!).getTime() - new Date(b.next_run_at!).getTime())[0];
  const nextAt = next?.next_run_at ? new Date(next.next_run_at).getTime() : null;
  const overdue = nextAt !== null && nextAt < now - 5 * 60_000;

  const all = integrations.data?.integrations ?? [];
  const engine = all.find((i) => ENGINE_NAMES.has(i.name));
  const claude = all.find((i) => i.name === "anthropic");
  const channels = all.filter((i) => !NOT_CHANNELS.has(i.name));
  const state = engineState(engine);
  const flows = engine?.workflows ?? null;
  const activeFlows = flows?.filter((w) => w.active).length ?? 0;
  const st = engine?.status;
  const recovered = !!(st?.last_error_at && st.last_ok_at && st.last_ok_at > st.last_error_at);
  const health = st?.detail as { reachable?: boolean | null; ready?: boolean | null; status_code?: number; last_ack_event_id?: number; last_ack_workflow?: string } | undefined;

  const reqs = requests.data?.requests ?? [];
  const recent = reqs.filter((r) => new Date(r.created_at).getTime() >= now - DAY);
  // The window is cut short when all of the latest 50 fall inside the last 24 hours and more exist.
  const truncated = reqs.length === REQUEST_WINDOW && recent.length === REQUEST_WINDOW && (requests.data?.total ?? 0) > REQUEST_WINDOW;
  const byStatus = (s: SupervisorRequest["status"][]) => recent.filter((r) => s.includes(r.status)).length;
  const completed = byStatus(["completed"]);
  const inFlight = byStatus(["planning", "running"]);
  const waiting = byStatus(["awaiting_approval"]);
  const failed = byStatus(["failed"]);

  const scheduleTitle = (s: Schedule) => humanize(s.name);

  return (
    <div className="page wf-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Automation</div>
          <h1>Workflows</h1>
          <p className="muted">Every automation that runs the platform: schedules, the workflow engine and AI Supervisor requests.</p>
        </div>
        <div className="row-actions">
          <Link className="btn" to="/integrations">
            <Icon name="plug" size={16} /> Workflow engine
          </Link>
          <Link className={`btn ${canOperate ? "primary" : ""}`} to="/supervisor">
            <Icon name="sparkles" size={16} /> {canOperate ? "New request" : "AI Supervisor"}
          </Link>
        </div>
      </header>

      <div className="tiles cols-4">
        <Tile
          icon="calendar"
          tone="blue"
          label="Scheduled automations"
          hint={
            schedules.data ? (
              list.length ? (
                <>
                  {list.length - enabled.length} paused · last ran {timeAgo(lastRun, now)}
                </>
              ) : (
                "No schedules are set up"
              )
            ) : (
              schedules.error ?? "…"
            )
          }
        >
          <span className="tile-value tabular">{schedules.data ? enabled.length : "—"}</span>
          <span className="tile-of">{schedules.data ? `of ${list.length} enabled` : ""}</span>
          {list.length ? (
            <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={list.length} aria-valuenow={enabled.length} aria-label="Schedules enabled">
              <span className="meter-fill tone-good" style={{ width: `${Math.max(2, (enabled.length / list.length) * 100)}%` }} />
            </div>
          ) : null}
        </Tile>

        <Tile
          icon="clock"
          tone={overdue ? "warn" : "cyan"}
          label="Next scheduled run"
          hint={
            !schedules.data ? (
              schedules.error ?? "…"
            ) : next ? (
              <span className="wf-clamp" title={next.description}>
                {overdue ? "Overdue: is the worker running? · " : ""}
                {scheduleTitle(next)} · {agentByName.get(next.agent)?.title ?? next.agent}
              </span>
            ) : (
              "No enabled schedule has a next run"
            )
          }
        >
          {next?.next_run_at ? (
            <>
              <span className="tile-value wf-when">{nextAt! <= now ? <>due {timeAgo(next.next_run_at, now)}</> : <Time iso={next.next_run_at} />}</span>
              <span className="tile-date">{when(next.next_run_at)}</span>
            </>
          ) : (
            <span className="tile-value">—</span>
          )}
        </Tile>

        <Tile
          icon="workflows"
          tone={TILE_TONE[state.tone]}
          label="Workflow engine"
          to="/integrations"
          hint={
            !integrations.data ? (
              integrations.error ?? "…"
            ) : !engine ? (
              "Not reported by the server"
            ) : flows ? (
              `${activeFlows} of ${flows.length} n8n workflow${flows.length === 1 ? "" : "s"} active`
            ) : engine.mode === "disabled" ? (
              "Events are recorded but not handed on"
            ) : st?.last_ok_at ? (
              <>Last success {timeAgo(st.last_ok_at, now)}</>
            ) : (
              "No successful contact yet"
            )
          }
        >
          <span className={`tile-status ${state.tone === "muted" ? "muted" : `text-${state.tone}`}`}>{integrations.data ? state.label : "Checking…"}</span>
          {engine ? (
            <span className="wf-tile-sub">
              {engine.name === "none" ? "No engine configured" : engine.title} <Pill value={engine.mode} label={engine.mode.toUpperCase()} />
            </span>
          ) : null}
        </Tile>

        <Tile
          icon="sparkles"
          tone="purple"
          label="Supervisor requests"
          to="/supervisor"
          hint={
            !requests.data ? (
              requests.error ?? "…"
            ) : recent.length ? (
              <>
                {truncated ? "There may be more · " : ""}
                {completed} completed · {inFlight ? `${inFlight} in progress · ` : ""}
                {waiting ? <span className="text-warn">{waiting} awaiting approval · </span> : null}
                {failed ? <span className="text-bad">{failed} failed</span> : "0 failed"}
              </>
            ) : reqs.length ? (
              <>Last request {timeAgo(reqs[0].created_at, now)}</>
            ) : (
              "No requests yet"
            )
          }
        >
          <span className="tile-value tabular">{requests.data ? recent.length : "—"}</span>
          <span className="tile-of">{truncated ? `of the latest ${REQUEST_WINDOW}, last 24 h` : "in the last 24 h"}</span>
        </Tile>
      </div>

      <Card
        className="wf-schedules"
        title={
          <>
            <Icon name="calendar" size={18} className="card-icon" /> Scheduled automations
          </>
        }
        actions={
          canOperate ? (
            <Link className="small" to="/schedules">
              Edit timings
            </Link>
          ) : null
        }
      >
        <p className="muted small wf-lead">Agent routines the worker starts on a timer. Cron times are in UTC; next runs are shown in your local time.</p>
        <ErrorNote error={schedules.error ?? error} />
        {list.length ? (
          <div className="table-wrap">
            <table className="table wf-table">
              <thead>
                <tr>
                  <th scope="col">Automation</th>
                  <th scope="col">Runs</th>
                  <th scope="col">Schedule</th>
                  <th scope="col">Last run</th>
                  <th scope="col">Next run</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {list.map((s) => {
                  const agent = agentByName.get(s.agent);
                  const cap = agent?.capabilities.find((c) => c.name === s.capability);
                  const hint = cronHint(s.cron);
                  const last = lastTasks.data?.[s.id];
                  const title = scheduleTitle(s);
                  return (
                    <tr key={s.id} className={s.enabled ? "" : "wf-paused"}>
                      <td className="wf-col-name">
                        <strong title={s.name}>{title}</strong>
                        {s.description ? <div className="muted small">{s.description}</div> : null}
                      </td>
                      <td className="wf-col-runs" data-label="Runs">
                        <div className="wf-agent">{agent?.title ?? s.agent}</div>
                        <div className="wf-cap">
                          <code title={cap?.description}>
                            {s.agent}.<wbr />
                            {s.capability}
                          </code>
                          {cap ? <RiskBadge risk={cap.risk} /> : null}
                        </div>
                      </td>
                      <td data-label="Schedule">
                        {hint ? <div className="wf-cron-hint">{hint}</div> : null}
                        <code className="wf-cron">{s.cron}</code>
                      </td>
                      <td data-label="Last run">
                        {s.last_run_at ? <Time iso={s.last_run_at} /> : <span className="muted">Never</span>}
                        {last ? (
                          <div className="wf-last">
                            {agent?.sensitive ? (
                              <Pill value={last.status} />
                            ) : (
                              <button type="button" className="wf-task-link" onClick={() => setOpenTask(last.id)} aria-label={`Task #${last.id}: ${last.status.replace(/_/g, " ")}`}>
                                <Pill value={last.status} />
                                <span className="muted small">#{last.id}</span>
                              </button>
                            )}
                          </div>
                        ) : null}
                      </td>
                      <td data-label="Next run">
                        {s.enabled ? (
                          s.next_run_at ? (
                            <>
                              <Time iso={s.next_run_at} />
                              <div className="muted small">{when(s.next_run_at)}</div>
                            </>
                          ) : (
                            <span className="muted">Not planned yet</span>
                          )
                        ) : (
                          <span className="muted">Paused</span>
                        )}
                      </td>
                      <td className="wf-col-state" data-label="State">
                        {canOperate ? (
                          <ScheduleSwitch schedule={s} title={title} busy={busy === s.id} onToggle={() => void patch(s, { enabled: !s.enabled })} />
                        ) : (
                          <Pill value={s.enabled ? "up" : "disabled"} label={s.enabled ? "enabled" : "paused"} />
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : schedules.data ? (
          <Empty>No scheduled automations yet.</Empty>
        ) : !schedules.error ? (
          <Empty>Loading…</Empty>
        ) : null}
        {!canOperate && list.length ? <p className="muted small wf-foot">Operators and admins can pause or enable automations.</p> : null}
      </Card>

      <div className="wf-grid">
        <Card
          title={
            <>
              <Icon name="workflows" size={18} className="card-icon" /> Workflow engine{engine?.name === "n8n" ? " (n8n)" : ""}
            </>
          }
          actions={
            <Link className="small" to="/integrations">
              Integrations
            </Link>
          }
        >
          <ErrorNote error={integrations.error} />
          {!integrations.data ? (
            integrations.error ? null : <Empty>Loading…</Empty>
          ) : !engine ? (
            <Empty>The server did not report a workflow engine.</Empty>
          ) : (
            <>
              <div className="wf-engine-head">
                <span className={`dot dot-${state.tone}`} aria-hidden />
                <strong className={state.tone === "muted" ? "" : `text-${state.tone}`}>{state.label}</strong>
                <Pill value={engine.mode} label={engine.mode.toUpperCase()} />
              </div>
              <p className="muted small">{engine.description}</p>
              {engine.mode === "disabled" ? (
                <div className="alert alert-info small">
                  No workflow engine is connected, so alerts and events stay in the Command Center and no n8n workflow runs.
                  {engine.live_requirements ? <> {engine.live_requirements}</> : null}
                </div>
              ) : null}
              <dl className="kv wf-kv">
                <dt>Engine</dt>
                <dd>{engine.title}</dd>
                {health && (health.reachable != null || health.ready != null) ? (
                  <>
                    <dt>Health check</dt>
                    <dd>
                      {health.reachable ? "reachable" : "not reachable"}
                      {health.ready != null ? (health.ready ? " · ready" : " · not ready") : ""}
                      {health.status_code ? ` · HTTP ${health.status_code}` : ""}
                    </dd>
                  </>
                ) : null}
                {engine.mode !== "disabled" ? (
                  <>
                    <dt>Last success</dt>
                    <dd>{st?.last_ok_at ? <Time iso={st.last_ok_at} /> : <span className="muted">none recorded</span>}</dd>
                  </>
                ) : null}
                {st?.last_error_at ? (
                  <>
                    <dt>Last error</dt>
                    <dd>
                      <Time iso={st.last_error_at} />
                      {recovered ? <span className="muted"> · succeeded since</span> : null}
                      <div className={`small wf-error ${recovered ? "muted" : "text-bad"}`} title={st.last_error}>
                        {st.last_error.split("\n")[0]}
                      </div>
                    </dd>
                  </>
                ) : null}
                {health?.last_ack_event_id ? (
                  <>
                    <dt>Last acknowledged</dt>
                    <dd>
                      event #{health.last_ack_event_id}
                      {health.last_ack_workflow ? ` by ${health.last_ack_workflow}` : ""}
                    </dd>
                  </>
                ) : null}
                {isAdmin && engine.webhook_url ? (
                  <>
                    <dt>Events webhook</dt>
                    <dd className="mono wf-break">{engine.webhook_url}</dd>
                  </>
                ) : null}
              </dl>

              {engine.name === "n8n" ? (
                <>
                  <h3 className="wf-sub">
                    n8n workflows
                    {flows ? (
                      <span className="muted small">
                        {activeFlows} of {flows.length} active
                      </span>
                    ) : null}
                  </h3>
                  {flows ? (
                    flows.length ? (
                      <ul className="list wf-flows">
                        {flows.map((w) => (
                          <li key={w.id}>
                            <Pill value={w.active ? "up" : "disabled"} label={w.active ? "active" : "inactive"} />
                            <span className="wf-flow-name">{w.name}</span>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <Empty>n8n reports no workflows.</Empty>
                    )
                  ) : engine.workflow_error ? (
                    <p className="small text-bad wf-break">Could not list the n8n workflows: {engine.workflow_error}</p>
                  ) : (
                    <p className="small muted">n8n only shares its workflow list once an n8n API key (N8N_API_KEY) is configured, so the list isn't shown here yet.</p>
                  )}
                </>
              ) : engine.name !== "none" ? (
                <p className="small muted">This engine does not report a list of workflows.</p>
              ) : null}
            </>
          )}
        </Card>

        <Card
          title={
            <>
              <Icon name="sparkles" size={18} className="card-icon" /> On-demand workflows
            </>
          }
          actions={
            <Link className="small" to="/supervisor">
              Open console
            </Link>
          }
        >
          <p className="muted small wf-lead">
            Type a request in the command bar at the top{canOperate ? "" : " (operators and admins)"} and the AI Supervisor plans it into agent tasks. Anything
            that publishes, sends or spends money waits for approval before it runs.
            {claude ? (
              claude.mode === "live" ? (
                <> Plans are made by Claude{claude.model ? ` (${claude.model})` : ""}.</>
              ) : (
                <> Claude isn't connected, so the built-in rule-based planner is used.</>
              )
            ) : null}
          </p>
          <ErrorNote error={requests.error} />
          {reqs.length ? (
            <ul className="list wf-requests">
              {reqs.slice(0, 6).map((r) => {
                const steps = r.plan.steps ?? [];
                const titles = [...new Set(steps.map((s) => agentByName.get(s.agent)?.title ?? s.agent))];
                return (
                  <li key={r.id}>
                    <Pill value={r.status} />
                    <div className="wf-req-main">
                      <Link className="wf-req-msg" to={`/supervisor?id=${r.id}`} title={r.message}>
                        {r.message}
                      </Link>
                      <div className="muted small wf-req-meta">
                        <span className="wf-req-who">{r.requested_by || r.source}</span>
                        {r.requested_by && r.source && !r.requested_by.includes(r.source) ? <span> via {r.source}</span> : null}
                        <span>
                          {" "}
                          · <Time iso={r.created_at} />
                        </span>
                      </div>
                      {titles.length ? (
                        <div className="wf-steps">
                          {titles.slice(0, 3).map((t) => (
                            <span key={t} className="wf-step">
                              {t}
                            </span>
                          ))}
                          {titles.length > 3 ? <span className="wf-step">+{titles.length - 3} more</span> : null}
                        </div>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : requests.data ? (
            <Empty>No requests yet. {canOperate ? "Type one in the command bar above." : ""}</Empty>
          ) : !requests.error ? (
            <Empty>Loading…</Empty>
          ) : null}

          {channels.length ? (
            <div className="wf-channels">
              <h3 className="wf-sub">Where approved actions go</h3>
              <ul className="wf-channel-list">
                {channels.map((c) => (
                  <li key={c.name} title={c.description}>
                    <span className="wf-channel-name">{c.title.replace(/\s*\(.*\)$/, "")}</span>
                    <Pill value={c.mode} label={c.mode === "mock" ? "Mock" : c.mode === "live" ? "Live" : "Off"} />
                  </li>
                ))}
              </ul>
              {channels.some((c) => c.mode === "mock") ? (
                <p className="muted small">Mock channels record what they would do in the Outbox and send nothing.</p>
              ) : null}
            </div>
          ) : null}
        </Card>
      </div>

      {openTask !== null ? <TaskDetail id={openTask} onClose={() => setOpenTask(null)} /> : null}
    </div>
  );
}
