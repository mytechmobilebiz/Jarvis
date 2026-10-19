import { useEffect, useMemo, useState, type MouseEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type CCEvent, type Integration, type Severity } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, JsonView, Modal, Pill, SeverityIcon, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { TaskDetail } from "../work";
import "./AutomationLogs.css";

/** The API also returns when the worker will next try to forward a pending event. */
type LogEvent = CCEvent & { forward_next_at?: string | null };
type ForwardStatus = CCEvent["forward_status"];

const SEVERITIES: Severity[] = ["critical", "warning", "info"];
const SEVERITY_LABEL: Record<Severity, string> = { critical: "Critical", warning: "Warning", info: "Info" };
const LIMITS = [100, 200, 500];
const DEFAULT_LIMIT = 200;
const REFRESH_MS = 15_000;
const ENGINES = ["n8n", "webhook", "none"];
// Credit-repair event details stay on the Credit Repair page. Their linked task opens like any other: the task dialog
// itself hides a credit-repair task's details from users without the sensitive-data permission.
const CREDIT_REPAIR = "credit_repair";

const FORWARD_LABEL: Record<ForwardStatus, string> = { sent: "Sent", pending: "Pending", failed: "Failed", skipped: "Skipped" };
const FORWARD_HELP: Record<ForwardStatus, string> = {
  sent: "Handed to the workflow engine.",
  pending: "Waiting for the worker to hand it on.",
  failed: "The worker gave up after repeated delivery failures.",
  skipped: "Recorded here only: below the forwarding severity threshold, or no workflow engine is set up.",
};

const clockFmt = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const dayFmt = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" });

function useMedia(query: string): boolean {
  const supported = typeof window !== "undefined" && typeof window.matchMedia === "function";
  const [match, setMatch] = useState(() => supported && window.matchMedia(query).matches);
  useEffect(() => {
    if (!supported) return;
    const m = window.matchMedia(query);
    const on = () => setMatch(m.matches);
    on();
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, [query, supported]);
  return match;
}

/** Distinct values with how often each occurs, alphabetical; `keep` stays listed even when absent (a deep-linked filter). */
function tally(values: string[], keep: string): [string, number][] {
  const counts = new Map<string, number>();
  for (const v of values) counts.set(v, (counts.get(v) ?? 0) + 1);
  if (keep && !counts.has(keep)) counts.set(keep, 0);
  return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

/** `text` with every case-insensitive occurrence of `needle` (already lower-case) marked. */
function Highlight({ text, needle }: { text: string; needle: string }) {
  if (!needle) return <>{text}</>;
  const lower = text.toLowerCase();
  const parts: ReactNode[] = [];
  let from = 0;
  for (let at = lower.indexOf(needle); at >= 0; at = lower.indexOf(needle, from)) {
    if (at > from) parts.push(text.slice(from, at));
    parts.push(<mark key={at}>{text.slice(at, at + needle.length)}</mark>);
    from = at + needle.length;
  }
  parts.push(text.slice(from));
  return <>{parts}</>;
}

/** Absolute clock time with the day and relative time underneath. */
function When({ iso }: { iso: string }) {
  const d = new Date(iso);
  return (
    <span className="lg-when">
      <time className="lg-clock" dateTime={iso} title={fullFmt.format(d)}>
        {clockFmt.format(d)}
      </time>
      <span className="lg-ago">
        {dayFmt.format(d)} · <Time iso={iso} />
      </span>
    </span>
  );
}

function SeverityText({ severity }: { severity: Severity }) {
  return (
    <span className={`lg-sev lg-sev-${severity}`}>
      <span aria-hidden>
        <SeverityIcon severity={severity} />
      </span>
      {SEVERITY_LABEL[severity]}
    </span>
  );
}

function ForwardStatusPill({ event }: { event: LogEvent }) {
  const s = event.forward_status;
  return (
    <span className="lg-fwd" title={FORWARD_HELP[s] + (event.n8n_ack_at ? " The workflow confirmed it." : "")}>
      <Pill value={s} label={FORWARD_LABEL[s]} />
      {event.n8n_ack_at ? (
        <span className="lg-ack">
          <Icon name="check" size={12} /> acked
        </span>
      ) : null}
    </span>
  );
}

function EventDetail({ id, engineLabel, onClose, onOpenTask }: { id: number; engineLabel: string; onClose: () => void; onOpenTask: (taskId: number) => void }) {
  const detail = usePoll(() => api.get<{ event: LogEvent }>(`/api/events/${id}`), 0, [id]);
  const e = detail.data?.event;
  const full = (iso: string | null | undefined) =>
    iso ? (
      <>
        {fullFmt.format(new Date(iso))} <span className="muted">(<Time iso={iso} />)</span>
      </>
    ) : null;
  return (
    <Modal title={`Event #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error} />
      {e ? (
        <div className="lg-detail">
          <div className="lg-detail-head">
            <Pill value={e.severity} />
            <code>{e.kind}</code>
            <span className="muted small">from {e.source}</span>
          </div>
          <p className="lg-detail-title">{e.title}</p>
          <dl className="kv">
            <dt>Recorded</dt>
            <dd>{full(e.at)}</dd>
            <dt>Source</dt>
            <dd>{e.source}</dd>
            <dt>Kind</dt>
            <dd>
              <code>{e.kind}</code>
            </dd>
            <dt>Task</dt>
            <dd>
              {e.task_id ? (
                <button className="btn small" onClick={() => onOpenTask(e.task_id as number)}>
                  <Icon name="tasks" size={14} /> Open task #{e.task_id}
                </button>
              ) : (
                <span className="muted">Not linked to a task</span>
              )}
            </dd>
          </dl>

          <h3>Forwarding to {engineLabel}</h3>
          <dl className="kv">
            <dt>Status</dt>
            <dd>
              <Pill value={e.forward_status} label={FORWARD_LABEL[e.forward_status]} /> <span className="muted small">{FORWARD_HELP[e.forward_status]}</span>
            </dd>
            <dt>Forwarded</dt>
            <dd>{full(e.forwarded_at) ?? <span className="muted">Not yet</span>}</dd>
            {e.forward_status === "pending" && e.forward_next_at ? (
              <>
                <dt>Next attempt</dt>
                <dd>{full(e.forward_next_at)}</dd>
              </>
            ) : null}
            <dt>Failed attempts</dt>
            <dd className="tabular">{e.forward_attempts}</dd>
            <dt>Acknowledged</dt>
            <dd>{full(e.n8n_ack_at) ?? <span className="muted">No acknowledgement from the workflow</span>}</dd>
            <dt>Execution ID</dt>
            <dd>{e.n8n_execution_id ? <code>{e.n8n_execution_id}</code> : <span className="muted">—</span>}</dd>
            {e.forward_error ? (
              <>
                <dt>Last error</dt>
                <dd className="text-bad">{e.forward_error}</dd>
              </>
            ) : null}
          </dl>

          <h3>Detail</h3>
          {e.source === CREDIT_REPAIR ? (
            <p className="alert alert-info">Details of credit-repair events are only shown on the Credit Repair page.</p>
          ) : Object.keys(e.detail ?? {}).length ? (
            <JsonView value={e.detail} />
          ) : (
            <p className="muted">No extra detail was recorded.</p>
          )}
        </div>
      ) : !detail.error ? (
        <p className="muted">Loading…</p>
      ) : null}
    </Modal>
  );
}

export default function AutomationLogs() {
  const { user } = useAuth();
  const [params, setParams] = useSearchParams();
  const sevParam = params.get("severity") as Severity | null;
  const severity = sevParam && SEVERITIES.includes(sevParam) ? sevParam : null;
  const source = params.get("source") ?? "";
  const kind = params.get("kind") ?? "";
  const q = params.get("q") ?? "";
  const limitParam = Number(params.get("limit"));
  const limit = LIMITS.includes(limitParam) ? limitParam : DEFAULT_LIMIT;
  const openId = Number(params.get("event")) || null;

  const [auto, setAuto] = useState(true);
  const [busy, setBusy] = useState(false);
  const [taskId, setTaskId] = useState<number | null>(null);
  const phone = useMedia("(max-width: 700px)");

  const key = `${severity ?? "all"}:${limit}`;
  const feed = usePoll(
    () =>
      api
        .get<{ events: LogEvent[] }>(`/api/events?limit=${limit}${severity ? `&severity=${severity}` : ""}`)
        .then((r) => ({ key, at: new Date().toISOString(), events: r.events })),
    auto ? REFRESH_MS : 0,
    [key],
  );
  // Read once: only used to name the workflow engine and say whether it is live.
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 0);
  const engine = integrations.data?.integrations.find((i) => ENGINES.includes(i.name));
  const engineLabel = engine?.name === "webhook" ? "webhook" : "n8n";

  const events = feed.data?.events ?? [];
  const stale = !!feed.data && feed.data.key !== key;
  // A response for the previous filter can land after the current one; fetch again rather than stay dimmed
  // (with auto-refresh off nothing else would).
  const { refresh } = feed;
  useEffect(() => {
    if (!stale) return;
    const id = setTimeout(() => void refresh(), 1500);
    return () => clearTimeout(id);
  }, [stale, refresh]);

  function update(changes: Record<string, string | null>) {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(changes)) {
          if (v) next.set(k, v);
          else next.delete(k);
        }
        return next;
      },
      { replace: true },
    );
  }
  function hrefWith(changes: Record<string, string | null>): string {
    const next = new URLSearchParams(params);
    next.delete("event");
    for (const [k, v] of Object.entries(changes)) {
      if (v) next.set(k, v);
      else next.delete(k);
    }
    const qs = next.toString();
    return qs ? `/logs?${qs}` : "/logs";
  }

  const stats = useMemo(() => {
    const n = { critical: 0, warning: 0, info: 0, sent: 0, pending: 0, failed: 0, skipped: 0, acked: 0 };
    const newest: Partial<Record<Severity, string>> = {};
    for (const e of events) {
      n[e.severity] += 1;
      n[e.forward_status] += 1;
      if (e.n8n_ack_at) n.acked += 1;
      newest[e.severity] ??= e.at;
    }
    return { n, newest };
  }, [events]);

  const sources = useMemo(() => tally(events.map((e) => e.source), source), [events, source]);
  const kinds = useMemo(() => tally(events.map((e) => e.kind), kind), [events, kind]);
  const needle = q.trim().toLowerCase();
  const shown = useMemo(
    () => events.filter((e) => (!source || e.source === source) && (!kind || e.kind === kind) && (!needle || e.title.toLowerCase().includes(needle))),
    [events, source, kind, needle],
  );
  const filtered = !!(severity || source || kind || needle);

  // Fewer rows than asked for means this is everything recorded (for the chosen severity).
  const scope = severity ? `${SEVERITY_LABEL[severity].toLowerCase()} ` : "";
  const windowText = !feed.data
    ? "Loading…"
    : events.length < limit
      ? `In all ${plural(events.length, `recorded ${scope}event`)}`
      : `In the latest ${events.length} ${scope}events`;

  async function refreshNow() {
    setBusy(true);
    await feed.refresh();
    setBusy(false);
  }
  const openEvent = (id: number | null) => update({ event: id ? String(id) : null });
  const openTask = (id: number) => {
    openEvent(null);
    setTaskId(id);
  };
  function rowClick(e: LogEvent) {
    // Let people select and copy text from a row without opening it.
    if (window.getSelection()?.toString()) return;
    openEvent(e.id);
  }
  const taskButton = (e: LogEvent) =>
    e.task_id ? (
      <button
        className="btn small lg-task"
        aria-label={`Open task ${e.task_id}`}
        onClick={(ev: MouseEvent) => {
          ev.stopPropagation();
          setTaskId(e.task_id);
        }}
      >
        <Icon name="tasks" size={13} />#{e.task_id}
      </button>
    ) : (
      <span className="muted">
        <span aria-hidden>—</span>
        <span className="sr-only">No task</span>
      </span>
    );

  const severityTile = (s: Severity, icon: "alert" | "bell" | "pulse", tone: string, label: string, fill: string) => {
    const hidden = !!severity && severity !== s;
    const count = stats.n[s];
    return (
      <Tile
        icon={icon}
        tone={tone}
        label={label}
        to={hrefWith({ severity: s })}
        hint={
          hidden ? (
            `Not loaded: the ${SEVERITY_LABEL[severity].toLowerCase()} filter is on`
          ) : (
            <>
              {windowText}
              {stats.newest[s] ? (
                <>
                  {" "}
                  · newest <Time iso={stats.newest[s]} />
                </>
              ) : null}
            </>
          )
        }
      >
        <span className="tile-value tabular">{feed.data && !hidden ? count.toLocaleString() : "—"}</span>
        {feed.data && !hidden ? <span className="tile-of">of {events.length.toLocaleString()}</span> : null}
        {feed.data && !hidden && events.length ? (
          <div className="meter thin" aria-hidden>
            <span className={`meter-fill ${fill}`} style={{ width: `${(count / events.length) * 100}%` }} />
          </div>
        ) : null}
      </Tile>
    );
  };

  const forwardable = events.length - stats.n.skipped;
  const pct = (v: number) => `${forwardable ? (v / forwardable) * 100 : 0}%`;

  return (
    <div className="page lg-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Operations</div>
          <h1>Automation Logs</h1>
          <p className="muted">
            Every alert, task result, approval request and monitoring finding, and whether it reached {engineLabel}.
          </p>
        </div>
        <div className="row-actions">
          <button className={`btn lg-live${auto ? " on" : ""}`} aria-pressed={auto} onClick={() => setAuto((a) => !a)} title={`Refresh every ${REFRESH_MS / 1000} seconds`}>
            <span className={`dot ${auto ? "dot-good" : "dot-muted"}`} aria-hidden /> Auto-refresh
            <span className="lg-live-state">{auto ? `${REFRESH_MS / 1000}s` : "off"}</span>
          </button>
          <button className={`btn${busy ? " lg-busy" : ""}`} onClick={() => void refreshNow()} disabled={busy}>
            <Icon name="refresh" size={16} /> Refresh
          </button>
          {user.role === "admin" ? (
            <Link className="btn" to="/activity">
              <Icon name="history" size={16} /> Admin activity log
            </Link>
          ) : null}
        </div>
      </header>

      <div className={`tiles cols-4${stale ? " lg-stale" : ""}`}>
        {severityTile("critical", "alert", "bad", "Critical alerts", "tone-bad")}
        {severityTile("warning", "bell", "warn", "Warnings", "tone-warn")}
        {severityTile("info", "pulse", "cyan", "Info events", "lg-fill-info")}
        <Tile
          icon="send"
          tone={!feed.data ? "muted" : stats.n.failed ? "bad" : stats.n.pending ? "warn" : "green"}
          label={`Forwarded to ${engineLabel}`}
          to="/integrations"
          hint={
            feed.data ? (
              <>
                {engine?.mode === "disabled" ? "No workflow engine is set up, so nothing is forwarded" : `${stats.n.pending} pending · ${stats.n.skipped} skipped · ${stats.n.acked} acked`}
                <br />
                {windowText}
              </>
            ) : (
              "Loading…"
            )
          }
        >
          <span className="tile-value tabular">{feed.data ? stats.n.sent.toLocaleString() : "—"}</span>
          <span className="tile-of">sent</span>
          {feed.data ? (
            <>
              <span className={`lg-tile-failed tabular${stats.n.failed ? " text-bad" : ""}`}>{stats.n.failed.toLocaleString()}</span>
              <span className="tile-of">failed</span>
            </>
          ) : null}
          {feed.data && forwardable > 0 ? (
            <div className="meter thin lg-stack" aria-hidden>
              <span className="meter-fill tone-good" style={{ width: pct(stats.n.sent) }} />
              <span className="meter-fill tone-warn" style={{ width: pct(stats.n.pending) }} />
              <span className="meter-fill tone-bad" style={{ width: pct(stats.n.failed) }} />
            </div>
          ) : null}
        </Tile>
      </div>

      <Card
        className="lg-card"
        title={
          <>
            <Icon name="logs" size={18} className="card-icon" /> Event stream
          </>
        }
        actions={
          <>
            {feed.data ? (
              <span className="lg-count small" role="status">
                <strong className="tabular">{shown.length.toLocaleString()}</strong> of {plural(events.length, "event")}
                <span className="muted">
                  {" "}
                  · updated <Time iso={feed.data.at} />
                </span>
              </span>
            ) : null}
            {filtered ? (
              <button className="btn ghost small lg-clear" onClick={() => update({ severity: null, source: null, kind: null, q: null })}>
                <Icon name="close" size={14} /> Clear filters
              </button>
            ) : null}
          </>
        }
      >
        <div className="lg-filters" role="group" aria-label="Filter events">
          <div className="tabs lg-seg" role="group" aria-label="Severity">
            <button className={!severity ? "active" : ""} aria-pressed={!severity} onClick={() => update({ severity: null })}>
              All
            </button>
            {SEVERITIES.map((s) => (
              <button key={s} className={severity === s ? "active" : ""} aria-pressed={severity === s} onClick={() => update({ severity: s })}>
                <span aria-hidden>
                  <SeverityIcon severity={s} />
                </span>
                {SEVERITY_LABEL[s]}
              </button>
            ))}
          </div>
          <label className="lg-field lg-field-source">
            <span className="sr-only">Source</span>
            <select value={source} onChange={(e) => update({ source: e.target.value })}>
              <option value="">All sources</option>
              {sources.map(([v, n]) => (
                <option key={v} value={v}>
                  {v} ({n})
                </option>
              ))}
            </select>
          </label>
          <label className="lg-field lg-field-kind">
            <span className="sr-only">Kind</span>
            <select value={kind} onChange={(e) => update({ kind: e.target.value })}>
              <option value="">All kinds</option>
              {kinds.map(([v, n]) => (
                <option key={v} value={v}>
                  {v} ({n})
                </option>
              ))}
            </select>
          </label>
          <label className="lg-field lg-search">
            <span className="sr-only">Search event titles</span>
            <Icon name="search" size={16} className="lg-search-icon" />
            <input type="search" value={q} placeholder="Search titles" onChange={(e) => update({ q: e.target.value })} />
          </label>
          <label className="lg-field lg-field-limit">
            <span className="sr-only">How many events to load</span>
            <select value={limit} onChange={(e) => update({ limit: Number(e.target.value) === DEFAULT_LIMIT ? null : e.target.value })}>
              {LIMITS.map((n) => (
                <option key={n} value={n}>
                  Latest {n}
                </option>
              ))}
            </select>
          </label>
        </div>

        <ErrorNote error={feed.error ? `Could not load events: ${feed.error}` : null} />

        <div className={stale ? "lg-stale" : undefined} aria-busy={stale || !feed.data}>
          {!feed.data ? (
            feed.error ? null : <Empty>Loading events…</Empty>
          ) : !events.length ? (
            <Empty>
              {severity
                ? `No ${SEVERITY_LABEL[severity].toLowerCase()} events have been recorded.`
                : "No events recorded yet. Alerts, task failures, approval requests and monitoring results appear here as the agents work."}
            </Empty>
          ) : !shown.length ? (
            <div className="empty lg-empty">
              No events match these filters.{" "}
              <button className="btn small" onClick={() => update({ source: null, kind: null, q: null })}>
                Clear source, kind and search
              </button>
            </div>
          ) : phone ? (
            <ul className="lg-list">
              {shown.map((e) => (
                <li key={e.id} className={`lg-item lg-tone-${e.severity}`}>
                  <button className="lg-item-main" onClick={() => openEvent(e.id)}>
                    <span className="lg-item-top">
                      <SeverityText severity={e.severity} />
                      <span className="lg-item-time">
                        <time dateTime={e.at} className="tabular">
                          {clockFmt.format(new Date(e.at))}
                        </time>{" "}
                        · <Time iso={e.at} />
                      </span>
                    </span>
                    <span className="lg-item-title">
                      <Highlight text={e.title} needle={needle} />
                    </span>
                    <span className="lg-item-meta">
                      {e.source} · <span className="mono">{e.kind}</span>
                    </span>
                  </button>
                  <div className="lg-item-foot">
                    {e.task_id ? taskButton(e) : <span className="muted small">No task</span>}
                    <ForwardStatusPill event={e} />
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <div className="table-wrap">
              <table className="table lg-table">
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Severity</th>
                    <th scope="col" className="lg-c-src">
                      Source / kind
                    </th>
                    <th scope="col">Event</th>
                    <th scope="col">Task</th>
                    <th scope="col">Forwarding</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((e) => (
                    <tr key={e.id} className={`lg-row lg-tone-${e.severity}`} onClick={() => rowClick(e)}>
                      <td className="lg-c-time">
                        <When iso={e.at} />
                      </td>
                      <td className="lg-c-sev">
                        <SeverityText severity={e.severity} />
                      </td>
                      <td className="lg-c-src">
                        <span className="lg-source">{e.source}</span>
                        <span className="lg-kind mono">{e.kind}</span>
                      </td>
                      <td className="lg-c-title">
                        <button
                          className="lg-title"
                          onClick={(ev: MouseEvent) => {
                            ev.stopPropagation();
                            openEvent(e.id);
                          }}
                        >
                          <Highlight text={e.title} needle={needle} />
                        </button>
                        <span className="lg-title-meta">
                          {e.source} · <span className="mono">{e.kind}</span>
                        </span>
                      </td>
                      <td className="lg-c-task">{taskButton(e)}</td>
                      <td className="lg-c-fwd">
                        <ForwardStatusPill event={e} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>

      {openId ? <EventDetail id={openId} engineLabel={engineLabel} onClose={() => openEvent(null)} onOpenTask={openTask} /> : null}
      {taskId ? <TaskDetail id={taskId} onClose={() => setTaskId(null)} /> : null}
    </div>
  );
}
