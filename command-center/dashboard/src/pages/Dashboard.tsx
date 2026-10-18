import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Agent, type Approval, type CCEvent, type Site, type SupervisorRequest, type SystemStatus, type Task } from "../api";
import { useAuth } from "../App";
import TaskActivityChart, { type HourBucket } from "../charts/TaskActivityChart";
import { Card, Empty, ErrorNote, Pill, SeverityIcon, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import { serviceHealth } from "../shell/Sidebar";
import "./Dashboard.css";

const PAGE = 200;
const MAX_PAGES = 10;
const HOUR = 3_600_000;

/** Finished tasks (completed or failed) since `since`, newest first, paging the task list (ordered by id). */
async function finishedSince(since: Date): Promise<{ tasks: Task[]; truncated: boolean }> {
  const out: Task[] = [];
  // Tasks can take up to about an hour, so keep reading until a whole page started well before `since`.
  const stopBefore = since.getTime() - HOUR;
  for (let page = 0; page < MAX_PAGES; page++) {
    const r = await api.get<{ tasks: Task[]; total: number }>(`/api/tasks?status=succeeded,failed&limit=${PAGE}&offset=${page * PAGE}`);
    out.push(...r.tasks);
    if (r.tasks.length < PAGE || r.tasks.every((t) => new Date(t.created_at).getTime() < stopBefore)) {
      return { tasks: out, truncated: false };
    }
  }
  return { tasks: out, truncated: true };
}

function startOfToday(now: Date): Date {
  return new Date(now.getFullYear(), now.getMonth(), now.getDate());
}

/** Locale time with seconds and AM/PM set smaller: "11:00" + ":54 PM". */
function clockParts(d: Date): { value: string; small: boolean }[] {
  const parts = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(d);
  const firstSmall = parts.findIndex((p, i) => p.type === "second" || (p.type === "literal" && parts[i + 1]?.type === "second"));
  // Some locales separate AM/PM with a narrow no-break space; widen it so ":54 PM" reads cleanly at the smaller size.
  return parts.map((p, i) => ({ value: p.value.replace(/\u202f/g, "\u00a0"), small: p.type === "dayPeriod" || (firstSmall >= 0 && i >= firstSmall) }));
}

function useClock(): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return now;
}

function Sparkline({ values }: { values: number[] }) {
  const max = Math.max(1, ...values);
  const w = 120;
  const h = 32;
  const step = w / Math.max(1, values.length - 1);
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - 3 - (v / max) * (h - 6)).toFixed(1)}`).join(" ");
  return (
    <svg className="sparkline" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
      <polyline points={`0,${h} ${pts} ${w},${h}`} className="sparkline-area" />
      <polyline points={pts} className="sparkline-line" />
    </svg>
  );
}

function pct(v: number | null | undefined, digits = 2): string {
  return v == null ? "—" : `${v.toFixed(v >= 99.995 ? 0 : digits)}%`;
}

function uptimeTone(v: number | null): string {
  return v == null ? "muted" : v >= 99.5 ? "good" : v >= 98 ? "warn" : "bad";
}

function average(values: (number | null | undefined)[]): number | null {
  const real = values.filter((v): v is number => typeof v === "number");
  return real.length ? real.reduce((a, b) => a + b, 0) / real.length : null;
}

export default function Dashboard({ status }: { status: SystemStatus | null }) {
  const { user, canOperate } = useAuth();
  const now = useClock();
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 30_000);
  const sites = usePoll(() => api.get<{ sites: Site[] }>("/api/sites"), 20_000);
  const approvals = usePoll(() => api.get<{ approvals: Approval[] }>("/api/approvals?status=pending"), 15_000);
  const events = usePoll(() => api.get<{ events: CCEvent[] }>("/api/events?limit=5"), 15_000);
  const requests = usePoll(() => api.get<{ requests: SupervisorRequest[] }>("/api/supervisor/requests?limit=5"), 15_000);
  const finished = usePoll(() => {
    const t = new Date();
    const since = new Date(Math.min(startOfToday(t).getTime(), t.getTime() - 24 * HOUR));
    return finishedSince(since);
  }, 30_000);

  const midnight = startOfToday(now).getTime();
  const nowHour = new Date(now);
  nowHour.setMinutes(0, 0, 0);
  const hourKey = nowHour.getTime();

  const { buckets, completedToday, failedToday, todayByHour } = useMemo(() => {
    const first = hourKey - 23 * HOUR;
    const b: HourBucket[] = Array.from({ length: 24 }, (_, i) => ({ start: new Date(first + i * HOUR), completed: 0, failed: 0 }));
    const today = new Array(Math.max(1, Math.floor((hourKey - midnight) / HOUR) + 1)).fill(0);
    let done = 0;
    let failed = 0;
    for (const t of finished.data?.tasks ?? []) {
      if (!t.finished_at) continue;
      const at = new Date(t.finished_at).getTime();
      const i = Math.floor((at - first) / HOUR);
      if (i >= 0 && i < 24) b[i][t.status === "succeeded" ? "completed" : "failed"] += 1;
      if (at >= midnight) {
        if (t.status === "succeeded") {
          done += 1;
          const h = Math.floor((at - midnight) / HOUR);
          if (h >= 0 && h < today.length) today[h] += 1;
        } else failed += 1;
      }
    }
    return { buckets: b, completedToday: done, failedToday: failed, todayByHour: today };
  }, [finished.data, hourKey, midnight]);

  const specialists = (agents.data?.agents ?? []).filter((a) => a.role === "agent");
  const supervisor = agents.data?.agents.find((a) => a.role === "supervisor");
  const activeAgents = specialists.filter((a) => a.enabled && a.entitled).length;
  const siteList = sites.data?.sites ?? [];
  const count = (k: string) => status?.counts.sites[k] ?? 0;
  const monitored = siteList.filter((s) => s.enabled).length;
  const uptime24 = average(siteList.map((s) => s.uptime?.["24h"]));
  const uptime7 = average(siteList.map((s) => s.uptime?.["7d"]));
  const health = serviceHealth(status);
  const services: { name: string; state: string; detail: string }[] = status
    ? [
        { name: "Database", state: health.database, detail: status.database.ok ? "connected" : "error" },
        { name: "Worker", state: health.worker, detail: health.worker === "good" ? "running" : "not reporting" },
        { name: "n8n", state: health.n8n, detail: health.n8n === "good" ? "connected" : "problem" },
        { name: "AI planner", state: status.llm.configured ? "good" : "muted", detail: status.llm.configured ? `Claude ${status.llm.model ?? ""}` : "rule-based" },
      ]
    : [];
  const problems = services.filter((s) => s.state === "bad" || s.state === "warn").length;
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const problemSites = siteList.filter((s) => s.last_status === "down" || s.last_status === "degraded");
  const greeting = now.getHours() < 12 ? "Good morning" : now.getHours() < 18 ? "Good afternoon" : "Good evening";

  return (
    <div className="page dashboard">
      <header className="page-head">
        <div>
          <div className="eyebrow">Infinite AI Command Center</div>
          <h1>
            {greeting}, {user.display_name || user.email.split("@")[0]}
          </h1>
          <p className="muted">Your agents, websites and servers at a glance. Everything here is live.</p>
        </div>
        <div className="row-actions">
          <Link className="btn" to="/approvals">
            <Icon name="approvals" size={16} /> Approvals
            {status?.counts.approvals_pending ? <span className="btn-count">{status.counts.approvals_pending}</span> : null}
          </Link>
          {canOperate ? (
            <Link className="btn primary" to="/supervisor">
              <Icon name="sparkles" size={16} /> AI Supervisor
            </Link>
          ) : null}
        </div>
      </header>

      <div className="tiles">
        <Tile
          icon="agents"
          tone="purple"
          label="Active AI agents"
          to="/agents"
          hint={
            agents.data ? (
              <>
                AI Supervisor {supervisor?.enabled ? "on" : "off"} · {status?.counts.tasks_running ?? 0} running · {status?.counts.tasks_queued ?? 0} queued
              </>
            ) : (
              agents.error ?? "…"
            )
          }
        >
          <span className="tile-value">{agents.data ? activeAgents : "—"}</span>
          <span className="tile-of">of {specialists.length || 7}</span>
        </Tile>
        <Tile
          icon="globe"
          tone={count("down") ? "bad" : count("degraded") ? "warn" : "cyan"}
          label="Websites monitored"
          to="/websites"
          hint={
            sites.data ? (
              monitored ? (
                <>
                  <span className="text-good">{count("up")} up</span> · <span className={count("down") ? "text-bad" : ""}>{count("down")} down</span> ·{" "}
                  <span className={count("degraded") ? "text-warn" : ""}>{count("degraded")} degraded</span>
                </>
              ) : (
                "Add your first site on the Websites page"
              )
            ) : (
              sites.error ?? "…"
            )
          }
        >
          <span className="tile-value">{sites.data ? monitored : "—"}</span>
          {siteList.length > monitored ? <span className="tile-of">{siteList.length - monitored} paused</span> : null}
        </Tile>
        <Tile
          icon="tasks"
          tone="blue"
          label="Tasks completed today"
          to="/tasks"
          hint={
            finished.data ? (
              <>
                {failedToday ? <span className="text-bad">{failedToday} failed</span> : "0 failed"} · since 00:00
                {finished.data.truncated ? " · showing the latest 2,000" : ""}
              </>
            ) : (
              finished.error ?? "…"
            )
          }
        >
          <span className="tile-value">{finished.data ? completedToday.toLocaleString() : "—"}</span>
          {finished.data ? <Sparkline values={todayByHour} /> : null}
        </Tile>
        <Tile
          icon="pulse"
          tone={uptimeTone(uptime24) === "muted" ? "green" : uptimeTone(uptime24)}
          label="Website uptime (24 h)"
          to="/websites"
          hint={sites.data ? (uptime24 == null ? "No checks recorded yet" : `7 days: ${pct(uptime7)} · average of ${siteList.length} site(s)`) : "…"}
        >
          <span className="tile-value">{pct(uptime24)}</span>
          {uptime24 != null ? (
            <div className="meter" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Number(uptime24.toFixed(2))} aria-label="Uptime">
              <span className={`meter-fill tone-${uptimeTone(uptime24)}`} style={{ width: `${Math.max(2, uptime24)}%` }} />
            </div>
          ) : null}
        </Tile>
        <Tile icon="clock" tone="orange" label="Local date and time" hint={zone}>
          <span className="tile-value tile-clock tabular">
            {clockParts(now).map((p, i) => (
              <span key={i} className={p.small ? "tile-seconds" : undefined}>
                {p.value}
              </span>
            ))}
          </span>
          <span className="tile-date">{now.toLocaleDateString([], { weekday: "long", day: "numeric", month: "long", year: "numeric" })}</span>
        </Tile>
        <Tile icon="database" tone={!status ? "muted" : problems ? "warn" : "green"} label="Worker and service health" to={user.role === "admin" ? "/integrations" : undefined}>
          <span className={`tile-status ${problems ? "text-warn" : "text-good"}`}>
            {!status ? "Checking…" : problems ? `${problems} need${problems > 1 ? "" : "s"} attention` : "All systems operational"}
          </span>
          <ul className="tile-services">
            {services.map((s) => (
              <li key={s.name}>
                <span className={`dot dot-${s.state}`} /> {s.name} <span className="muted">{s.detail}</span>
              </li>
            ))}
          </ul>
        </Tile>
      </div>

      <div className="dash-grid">
        <Card
          title={
            <>
              <Icon name="tasks" size={18} className="card-icon" /> Task activity, last 24 hours
            </>
          }
          className="span-2 chart-card"
          actions={
            <Link className="small" to="/tasks">
              All tasks
            </Link>
          }
        >
          <ErrorNote error={finished.error} />
          <TaskActivityChart buckets={buckets} dim={!finished.data} />
        </Card>

        <Card
          title={
            <>
              <Icon name="agents" size={18} className="card-icon" /> AI agents
            </>
          }
          actions={
            <Link className="small" to="/agents">
              Manage
            </Link>
          }
        >
          <ErrorNote error={agents.error} />
          <ul className="agent-list">
            {specialists.map((a) => {
              const runs = Object.values(a.stats_24h ?? {}).reduce((x, y) => x + y, 0);
              return (
                <li key={a.name}>
                  <span className={`dot dot-${!a.entitled ? "muted" : a.enabled ? "good" : "muted"}`} />
                  <div className="agent-list-main">
                    <div className="agent-list-name">{a.title}</div>
                    <div className="muted small">
                      {!a.entitled ? "not in this edition" : !a.enabled ? "switched off" : a.last_run_at ? <>last ran <Time iso={a.last_run_at} /></> : "not run yet"}
                    </div>
                  </div>
                  <span className="agent-list-count tabular" title="Tasks in the last 24 hours">
                    {runs}
                  </span>
                </li>
              );
            })}
          </ul>
        </Card>

        <Card
          title={
            <>
              <Icon name="globe" size={18} className="card-icon" /> Websites
            </>
          }
          actions={
            <Link className="small" to="/websites">
              All sites
            </Link>
          }
        >
          <ErrorNote error={sites.error} />
          {siteList.length ? (
            <ul className="site-list">
              {[...problemSites, ...siteList.filter((s) => !problemSites.includes(s))].slice(0, 6).map((s) => (
                <li key={s.id}>
                  <Pill value={s.last_status} />
                  <div className="site-list-main">
                    <div className="site-list-name">{s.name}</div>
                    <div className="muted small truncate">{s.url}</div>
                  </div>
                  <div className="site-list-uptime">
                    <span className="tabular">{pct(s.uptime?.["24h"], 1)}</span>
                    <div className="meter thin">
                      <span className={`meter-fill tone-${uptimeTone(s.uptime?.["24h"] ?? null)}`} style={{ width: `${Math.max(2, s.uptime?.["24h"] ?? 0)}%` }} />
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No websites yet. Add one on the Websites page.</Empty>
          )}
        </Card>

        <Card
          title={
            <>
              <Icon name="approvals" size={18} className="card-icon" /> Waiting for approval
            </>
          }
          actions={
            <Link className="small" to="/approvals">
              Review
            </Link>
          }
        >
          <ErrorNote error={approvals.error} />
          {approvals.data?.approvals.length ? (
            <ul className="list">
              {approvals.data.approvals.slice(0, 5).map((a) => (
                <li key={a.id}>
                  {a.details.production ? <Pill value="production" label="PRODUCTION" /> : <Pill value="pending" label={a.risk} />}
                  <div className="approval-main">
                    <div className="clamp-2">{a.summary}</div>
                    <div className="muted small">
                      {a.agent} · <Time iso={a.created_at} />
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>Nothing is waiting for you.</Empty>
          )}
        </Card>

        <Card
          title={
            <>
              <Icon name="alert" size={18} className="card-icon" /> Recent alerts and events
            </>
          }
          actions={
            <Link className="small" to="/logs">
              Automation logs
            </Link>
          }
        >
          <ErrorNote error={events.error} />
          {events.data?.events.length ? (
            <ul className="events">
              {events.data.events.map((e) => (
                <li key={e.id}>
                  <SeverityIcon severity={e.severity} />
                  <div>
                    <div className="clamp-1" title={e.title}>
                      {e.title}
                    </div>
                    <div className="muted small">
                      {e.source} · <Time iso={e.at} />
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No events yet.</Empty>
          )}
        </Card>

        <Card
          title={
            <>
              <Icon name="sparkles" size={18} className="card-icon" /> AI Supervisor requests
            </>
          }
          className="span-3"
          actions={
            <Link className="small" to="/supervisor">
              Open console
            </Link>
          }
        >
          <ErrorNote error={requests.error} />
          {requests.data?.requests.length ? (
            <ul className="list">
              {requests.data.requests.map((r) => (
                <li key={r.id}>
                  <Pill value={r.status} />
                  <Link className="request-link truncate" to={`/supervisor?id=${r.id}`}>
                    {r.message}
                  </Link>
                  <span className="muted small request-meta">
                    {r.requested_by || r.source} · <Time iso={r.created_at} />
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No requests yet. Type one in the command bar above.</Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
