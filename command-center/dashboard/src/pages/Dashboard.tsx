// The dashboard, in the reference design's rows: title (and pending approvals) → stat cards → AI agents → Workflow
// Activity / PC resources / Service Status → Recent Tasks / Active Workflows. Every value is live API data; anything
// missing, stale or not measurable says so (docs/REFERENCE_DESIGN_GAP.md).
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type ActivityRange,
  type Agent,
  type Integration,
  type ResearchHealth,
  type Schedule,
  type Server,
  type Site,
  type SystemStatus,
  type Task,
  type TaskActivity,
} from "../api";
import { useAuth } from "../App";
import ActiveWorkflows from "../dashboard/ActiveWorkflows";
import AgentCards from "../dashboard/AgentCards";
import RecentTasks, { RECENT_WINDOW } from "../dashboard/RecentTasks";
import { serviceRows } from "../dashboard/service";
import { PcResources, ServiceStatus } from "../dashboard/ServicePanels";
import StatCards from "../dashboard/StatCards";
import WorkflowActivity from "../dashboard/WorkflowActivity";
import { useNow, usePoll } from "../hooks";
import { isStale, n8nEditorUrl, type StatusMeta } from "../status";
import "./Dashboard.css";

const RANGE_KEY = "cc.dashboard.range";

function savedRange(): ActivityRange {
  try {
    const v = window.localStorage.getItem(RANGE_KEY);
    return v === "7d" || v === "30d" ? v : "24h";
  } catch {
    return "24h";
  }
}

function ApprovalsStrip({ pending }: { pending: number }) {
  if (pending <= 0) return null;
  return (
    <div className="dash-approvals" role="status">
      <svg width={18} height={18} viewBox="0 0 24 24" aria-hidden>
        <path d="M12 3.5 2.5 20h19L12 3.5Z" fill="currentColor" fillOpacity={0.2} stroke="currentColor" strokeWidth={1.8} strokeLinejoin="round" />
        <path d="M12 10v4.5M12 17.2v.1" stroke="currentColor" strokeWidth={2} strokeLinecap="round" />
      </svg>
      <span>
        <strong>
          {pending} action{pending === 1 ? " is" : "s are"} waiting for your approval
        </strong>{" "}
        — <Link to="/approvals">Review</Link>
      </span>
    </div>
  );
}

export default function Dashboard({ status, statusMeta }: { status: SystemStatus | null; statusMeta: StatusMeta }) {
  const { user, canOperate } = useAuth();
  const now = useNow(5_000);
  const [range, setRangeState] = useState<ActivityRange>(savedRange);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 15_000);
  const sites = usePoll(() => api.get<{ sites: Site[] }>("/api/sites"), 30_000);
  const activity = usePoll(
    () => api.get<TaskActivity>(`/api/stats/task-activity?range=${range}&tz=${encodeURIComponent(zone)}`),
    range === "24h" ? 30_000 : 60_000,
    [range, zone],
  );
  const tasks = usePoll(() => api.get<{ tasks: Task[]; total: number }>(`/api/tasks?limit=${RECENT_WINDOW}`), 15_000);
  const schedules = usePoll(() => api.get<{ schedules: Schedule[] }>("/api/schedules"), 30_000);
  const servers = usePoll(() => api.get<{ servers: Server[] }>("/api/servers"), 60_000);
  // The integrations list asks n8n for its workflows on every call, so it is read only once a minute.
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 60_000);
  // Web research's state is for admins and operators only (the API refuses viewers): a viewer's dashboard never asks.
  const research = usePoll(() => (canOperate ? api.get<ResearchHealth>("/api/research/health") : Promise.resolve(null)), canOperate ? 60_000 : 0, [canOperate]);

  function setRange(r: ActivityRange) {
    setRangeState(r);
    try {
      window.localStorage.setItem(RANGE_KEY, r);
    } catch {
      /* a remembered range is a convenience only */
    }
  }

  const fresh = status !== null && !isStale(statusMeta, now) && statusMeta.lastSuccessAt !== null;
  const workerAlive = fresh ? status.workers.some((w) => w.alive) : null;
  const rows = serviceRows(
    status,
    statusMeta,
    { data: servers.data?.servers ?? null, error: servers.error },
    { data: integrations.data?.integrations ?? null, error: integrations.error },
    now,
    canOperate ? { data: research.data, error: research.error } : undefined,
  );

  return (
    <div className="page dash">
      <header className="dash-head">
        <h1>Welcome to Infinite AI Command Center</h1>
        <p className="dash-subtitle">Your AI workforce for websites, marketing, servers, email, and credit repair.</p>
      </header>
      <ApprovalsStrip pending={status?.counts.approvals_pending ?? 0} />

      <StatCards
        agents={{ data: agents.data?.agents ?? null, error: agents.error }}
        sites={{ data: sites.data?.sites ?? null, error: sites.error }}
        activity={{ data: activity.data, error: activity.error }}
        status={status}
        statusMeta={statusMeta}
        now={now}
      />

      <AgentCards agents={agents.data?.agents ?? null} error={agents.error} user={user} workerAlive={workerAlive} now={now} />

      <div className="dash-panels">
        <WorkflowActivity data={activity.data} error={activity.error} range={range} onRange={setRange} />
        <PcResources />
        <ServiceStatus rows={rows} />
        <RecentTasks
          tasks={tasks.data?.tasks ?? null}
          error={tasks.error}
          sites={sites.data?.sites ?? []}
          servers={servers.data?.servers ?? []}
          user={user}
          now={now}
        />
        <ActiveWorkflows
          schedules={schedules.data?.schedules ?? null}
          schedulesError={schedules.error}
          onChanged={schedules.refresh}
          integrations={integrations.data?.integrations ?? null}
          canOperate={canOperate}
          editorUrl={user.role === "admin" ? n8nEditorUrl(status) : null}
          now={now}
        />
      </div>
    </div>
  );
}
