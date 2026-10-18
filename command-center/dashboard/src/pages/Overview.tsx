import { Link } from "react-router-dom";
import { api, type CCEvent, type Site, type SupervisorRequest, type SystemStatus } from "../api";
import { Card, Empty, ErrorNote, Pill, SeverityIcon, Stat, Time } from "../components";
import { usePoll } from "../hooks";

export default function Overview({ status }: { status: SystemStatus | null }) {
  const events = usePoll(() => api.get<{ events: CCEvent[] }>("/api/events?limit=12"), 10_000);
  const sites = usePoll(() => api.get<{ sites: Site[] }>("/api/sites"), 15_000);
  const requests = usePoll(() => api.get<{ requests: SupervisorRequest[] }>("/api/supervisor/requests?limit=6"), 10_000);
  const c = status?.counts;
  const siteCount = (k: string) => c?.sites[k] ?? 0;
  const totalSites = c ? Object.values(c.sites).reduce((a, b) => a + b, 0) : 0;
  const problemSites = (sites.data?.sites ?? []).filter((s) => s.last_status === "down" || s.last_status === "degraded");
  const n8nOk = !!status?.n8n?.last_ok_at && !(status.n8n.last_error_at && status.n8n.last_error_at > status.n8n.last_ok_at);

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="muted">Everything the agents are watching, at a glance.</p>
        </div>
        <Link className="btn primary" to="/supervisor">
          Ask the supervisor
        </Link>
      </header>

      <div className="stats">
        <Stat
          label="Websites"
          value={totalSites ? `${siteCount("up")}/${totalSites} up` : "—"}
          tone={siteCount("down") ? "bad" : siteCount("degraded") ? "warn" : totalSites ? "good" : undefined}
          hint={totalSites ? `${siteCount("down")} down · ${siteCount("degraded")} degraded · ${siteCount("unknown")} unchecked` : "No sites yet"}
        />
        <Stat label="Waiting for approval" value={c?.approvals_pending ?? "—"} tone={c?.approvals_pending ? "warn" : undefined} hint={<Link to="/approvals">Review</Link>} />
        <Stat
          label="Alerts (24 h)"
          value={c ? (c.events_24h.critical ?? 0) + (c.events_24h.warning ?? 0) : "—"}
          tone={c?.events_24h.critical ? "bad" : c?.events_24h.warning ? "warn" : undefined}
          hint={c ? `${c.events_24h.critical ?? 0} critical · ${c.events_24h.warning ?? 0} warning` : ""}
        />
        <Stat
          label="Tasks (24 h)"
          value={c ? Object.values(c.tasks_24h).reduce((a, b) => a + b, 0) : "—"}
          hint={c ? `${c.tasks_running} running · ${c.tasks_queued} queued · ${c.tasks_24h.failed ?? 0} failed` : ""}
          tone={c?.tasks_24h.failed ? "warn" : undefined}
        />
      </div>

      <div className="grid-2">
        <Card title="Platform">
          <dl className="kv">
            <dt>Database</dt>
            <dd>{status ? <Pill value={status.database.ok ? "up" : "down"} label={status.database.ok ? "connected" : "error"} /> : "…"}</dd>
            <dt>Worker</dt>
            <dd>
              {status?.workers.length ? (
                <>
                  <Pill value={status.workers.some((w) => w.alive) ? "up" : "down"} label={status.workers.some((w) => w.alive) ? "running" : "not reporting"} />{" "}
                  <span className="muted small">
                    last seen <Time iso={status.workers[0].last_seen_at} />
                  </span>
                </>
              ) : (
                <Pill value="down" label="no worker yet" />
              )}
            </dd>
            <dt>n8n</dt>
            <dd>
              {status?.n8n ? (
                <>
                  <Pill value={n8nOk ? "up" : "degraded"} label={n8nOk ? "connected" : "problem"} />{" "}
                  {!n8nOk && status.n8n.last_error ? <span className="muted small">{status.n8n.last_error}</span> : null}
                </>
              ) : (
                <Pill value="unknown" label="not contacted yet" />
              )}
            </dd>
            <dt>AI supervisor</dt>
            <dd>
              {status ? (
                status.llm.configured ? (
                  <>
                    <Pill value="live" label={`Claude · ${status.llm.model ?? ""}`} />{" "}
                    <span className="muted small">
                      {status.llm.requests_today}/{status.llm.daily_cap} requests today
                    </span>
                  </>
                ) : (
                  <>
                    <Pill value="info" label="rule-based planner" />{" "}
                    <span className="muted small">
                      {status.llm.reason ?? "no AI key set"} · <Link to="/settings">Settings</Link>
                    </span>
                  </>
                )
              ) : (
                "…"
              )}
            </dd>
          </dl>
        </Card>

        <Card title="Sites needing attention" actions={<Link to="/websites">All sites</Link>}>
          <ErrorNote error={sites.error} />
          {problemSites.length ? (
            <ul className="list">
              {problemSites.map((s) => (
                <li key={s.id}>
                  <Pill value={s.last_status} /> <strong>{s.name}</strong> <span className="muted small">{s.url}</span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>{totalSites ? "All monitored sites are up." : "Add your first site on the Websites page."}</Empty>
          )}
        </Card>
      </div>

      <div className="grid-2">
        <Card title="Recent alerts and events">
          <ErrorNote error={events.error} />
          {events.data?.events.length ? (
            <ul className="events">
              {events.data.events.map((e) => (
                <li key={e.id}>
                  <SeverityIcon severity={e.severity} />
                  <div>
                    <div>{e.title}</div>
                    <div className="muted small">
                      {e.kind} · {e.source} · <Time iso={e.at} />
                      {e.forward_status === "sent" ? (e.n8n_ack_at ? " · handled by n8n" : " · sent to n8n") : null}
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No events yet.</Empty>
          )}
        </Card>
        <Card title="Recent supervisor requests" actions={<Link to="/supervisor">Open console</Link>}>
          {requests.data?.requests.length ? (
            <ul className="list">
              {requests.data.requests.map((r) => (
                <li key={r.id}>
                  <Pill value={r.status} /> <span className="truncate">{r.message}</span>{" "}
                  <span className="muted small">
                    {r.source} · <Time iso={r.created_at} />
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty>No requests yet.</Empty>
          )}
        </Card>
      </div>
    </div>
  );
}
