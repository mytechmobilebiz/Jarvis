// 0.5.0 dashboard: the reference layout with real data only, honest empty / stale / unavailable states, and the
// schedule switches' confirmation and rollback.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Agent, Integration, Server, Site, SystemStatus, Task, TaskActivity, User } from "../api";
import { agentKeywords, AGENT_STYLE } from "../agentStyle";
import App from "../App";
import WorkflowActivityChart, { seriesOf, yTicks } from "../charts/WorkflowActivityChart";
import { agentActivity, agentStatus, formatDuration, formatUptime, successRate, systemLine, tasksTrend, websiteUptime } from "../dashboard/model";
import { serviceRows } from "../dashboard/service";
import { isInternal, statusPill, taskDuration, taskText } from "../dashboard/taskText";
import { mockFetch } from "./fetchMock";

const NOW = Date.now();
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

const admin: User = { id: 1, email: "owner@test.local", display_name: "Owner", role: "admin" };
const viewer: User = { ...admin, role: "viewer" };
const operator: User = { ...admin, role: "operator" };

function makeStatus(over: Partial<SystemStatus> = {}): SystemStatus {
  return {
    app: "Infinite AI Command Center",
    version: "0.5.0",
    environment: "test",
    time: new Date().toISOString(),
    database: { ok: true },
    workers: [{ worker_id: "w1", last_seen_at: new Date().toISOString(), alive: true, info: {} }],
    llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200, reason: "no Anthropic API key set" },
    workflow_engine: "n8n",
    n8n: { name: "n8n", last_ok_at: new Date().toISOString(), last_error_at: null, last_error: "", detail: { ready: true } },
    n8n_editor_url: "http://localhost:5680",
    counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
    ...over,
  };
}

const cap = (name: string) => ({ name, description: name, risk: "read" as const, requires_approval: false, params_schema: {}, examples: [] });

function agent(name: string, over: Partial<Agent> = {}): Agent {
  return {
    name,
    role: "agent",
    title: `${name} title`,
    description: "",
    enabled: true,
    entitled: true,
    sensitive: name === "credit_repair",
    integrations: [],
    capabilities: (AGENT_STYLE[name]?.keywords ?? []).flatMap((k) => k.caps.map((c) => cap(c.replace("*", "restore_site_url")))),
    stats_24h: {},
    last_run_at: null,
    active: { queued: 0, running: 0, awaiting_approval: 0 },
    ...over,
  };
}

const AGENTS = ["wordpress_security", "seo_content", "social_marketing", "server_management", "communications", "monitoring", "credit_repair"];

function site(id: number, over: Partial<Site> = {}): Site {
  return {
    id,
    name: `Site ${id}`,
    url: `https://s${id}.example`,
    client: "",
    notes: "",
    enabled: true,
    last_status: "up",
    last_checked_at: iso(MIN),
    last_check: null,
    environment: "production",
    server_id: null,
    server_site_key: "",
    uptime: { "24h": 100, "7d": 100, "30d": 100, checks_30d: 100, avg_response_ms_24h: 120 },
    last_diagnosis: {},
    last_diagnosis_at: null,
    ...over,
  };
}

function task(id: number, over: Partial<Task> = {}): Task {
  return {
    id,
    created_at: iso(5 * MIN),
    updated_at: iso(5 * MIN),
    request_id: null,
    schedule_id: null,
    agent: "monitoring",
    capability: "check_site",
    params: { site_id: 1 },
    risk: "read",
    status: "succeeded",
    attempts: 1,
    started_at: iso(5 * MIN),
    finished_at: iso(5 * MIN - 134_000),
    source: "dashboard",
    created_by: "owner@test.local",
    summary: "",
    result: null,
    error: "",
    ...over,
  };
}

function activity(over: Partial<TaskActivity> = {}): TaskActivity {
  const cats = [
    ["website_scans", "Website scans"],
    ["seo", "SEO"],
    ["content", "Content & marketing"],
    ["email", "Email & clients"],
    ["server_checks", "Server checks"],
    ["repairs", "Repairs"],
    ["reports", "Reports"],
    ["credit_repair", "Credit repair"],
  ];
  const zero = Object.fromEntries(cats.map(([k]) => [k, 0]));
  const start = new Date(2026, 9, 19, 8);
  const buckets = [0, 1, 2].map((h) => ({ start: new Date(start.getTime() + h * HOUR).toISOString(), counts: { ...zero, website_scans: h * 2, seo: h }, failed: h === 2 ? 1 : 0 }));
  return {
    range: "24h",
    bucket: "hour",
    tz: "UTC",
    since: iso(DAY),
    categories: cats.map(([key, label]) => ({ key, label })),
    buckets,
    totals: { ...zero, website_scans: 6, seo: 3 },
    failed: 1,
    today: { succeeded: 12, failed: 1 },
    yesterday_same_time: { succeeded: 10, failed: 0 },
    ...over,
  };
}

const schedules = [
  { id: 1, name: "wordpress-uptime-sweep", description: "Uptime check of every site", agent: "monitoring", capability: "check_all_sites", params: {}, cron: "*/15 * * * *", enabled: true, last_run_at: iso(5 * MIN), next_run_at: new Date(NOW + 10 * MIN).toISOString() },
  { id: 2, name: "wordpress-security-report", description: "Weekly report", agent: "wordpress_security", capability: "security_report", params: {}, cron: "43 6 * * 1", enabled: false, last_run_at: null, next_run_at: null },
];

const integrations: Integration[] = [
  { name: "n8n", title: "n8n", mode: "live", description: "", live_requirements: "", status: null, workflows: [{ id: "a", name: "Morning briefing", active: true }], workflow_error: "" },
  { name: "email", title: "Email", mode: "mock", description: "", live_requirements: "", status: null },
  { name: "postonomous", title: "Postonomous", mode: "mock", description: "", live_requirements: "", status: null },
];

function routes(over: Record<string, unknown> = {}) {
  return {
    "GET /api/auth/me": { user: admin },
    "GET /api/system/status": makeStatus(),
    "GET /api/agents": { agents: [agent("supervisor", { role: "supervisor" }), ...AGENTS.map((n) => agent(n))] },
    "GET /api/sites": { sites: [site(1), site(2, { last_status: "down" })] },
    "GET /api/stats/task-activity": activity(),
    "GET /api/tasks": { tasks: [], total: 0 },
    "GET /api/schedules": { schedules },
    "GET /api/servers": { servers: [] },
    "GET /api/integrations": { integrations },
    "GET /api/events": { events: [] },
    "GET /api/approvals": { approvals: [] },
    "GET /api/supervisor/requests": { requests: [] },
    ...over,
  };
}

const region = (name: string) => screen.getByRole("region", { name });
const statLink = (name: RegExp) => within(document.querySelector(".stat-row") as HTMLElement).getByRole("link", { name });

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  window.history.pushState({}, "", "/");
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

describe("dashboard rules (pure)", () => {
  it("weights 30-day website uptime by checks, leaves paused sites out and says since when on a young install", () => {
    const sites = [
      site(1, { uptime: { "24h": 100, "7d": 100, "30d": 100, checks_30d: 99, n_30d: 12, avg_response_ms_24h: 1, first_check_at: iso(2 * DAY) } }),
      site(2, { uptime: { "24h": 90, "7d": 90, "30d": 90.91, checks_30d: 11, avg_response_ms_24h: 1, first_check_at: iso(DAY) } }),
      site(3, { enabled: false, uptime: { "24h": 0, "7d": 0, "30d": 0, checks_30d: 500, avg_response_ms_24h: 1 } }),
    ];
    const u = websiteUptime(sites, NOW);
    // n_30d (12) wins over checks_30d (99): (12 * 100 + 11 * 90.91) / 23
    expect(u.checks).toBe(23);
    expect(u.value).toBeCloseTo((1200 + 11 * 90.91) / 23, 5);
    expect(u.since).toBe(new Date(sites[0].uptime!.first_check_at!).toISOString());
    expect(websiteUptime([site(1, { uptime: { "24h": 1, "7d": 1, "30d": 99, checks_30d: 10, avg_response_ms_24h: 1, first_check_at: iso(40 * DAY) } })], NOW).since).toBeNull();
    expect(websiteUptime([], NOW).value).toBeNull();
    expect(formatUptime(99.96)).toBe("99.9%");
    expect(formatUptime(100)).toBe("100%");
  });

  it("compares today with yesterday by the same time only from 5 tasks up", () => {
    const a = (t: number, y: number) => activity({ today: { succeeded: t, failed: 0 }, yesterday_same_time: { succeeded: y, failed: 0 } });
    expect(tasksTrend(a(48, 4))).toEqual({ text: "No comparison yet", dir: null });
    expect(tasksTrend(a(12, 10))).toEqual({ text: "20% vs yesterday", dir: "up" });
    expect(tasksTrend(a(8, 10))).toEqual({ text: "20% vs yesterday", dir: "down" });
    expect(tasksTrend(a(10, 10))).toEqual({ text: "Same as yesterday", dir: "same" });
  });

  it("says System Online only after a fresh poll with the database answering and a worker alive", () => {
    const fresh = { error: null, lastSuccessAt: NOW - 5_000 };
    expect(systemLine(makeStatus(), fresh, NOW)).toEqual({ tone: "good", text: "System Online" });
    expect(systemLine(null, { error: null, lastSuccessAt: null }, NOW).text).toBe("Checking…");
    expect(systemLine(makeStatus(), { error: "HTTP 502", lastSuccessAt: NOW - 60_000 }, NOW)).toMatchObject({ tone: "bad", text: expect.stringMatching(/^Cannot reach the Command Center · last update/) });
    expect(systemLine(makeStatus(), { error: null, lastSuccessAt: NOW - 45_000 }, NOW)).toMatchObject({ tone: "warn", text: expect.stringMatching(/^Not updated since/) });
    expect(systemLine(makeStatus({ workers: [{ worker_id: "w", last_seen_at: iso(10 * MIN), alive: false, info: {} }] }), fresh, NOW)).toEqual({ tone: "warn", text: "Worker not reporting" });
    expect(systemLine(makeStatus({ database: { ok: false } }), fresh, NOW)).toEqual({ tone: "bad", text: "Database not answering" });
  });

  it("derives agent status, current work and the 24-hour success rate from the API only", () => {
    expect(agentStatus(agent("seo_content", { entitled: false }))).toEqual({ tone: "muted", text: "Not in this edition" });
    expect(agentStatus(agent("seo_content", { enabled: false }))).toEqual({ tone: "muted", text: "Switched off" });
    expect(agentStatus(agent("seo_content", { stats_24h: { failed: 1, succeeded: 3 } }))).toEqual({ tone: "warn", text: "Needs attention" });
    expect(agentStatus(agent("seo_content", { last_run_at: iso(HOUR) }))).toEqual({ tone: "good", text: "Active" });
    // Switched on but never run: no green dot (C2-06).
    expect(agentStatus(agent("seo_content"))).toEqual({ tone: "muted", text: "Active" });

    const busy = agent("monitoring", { active: { running: 2, queued: 1, awaiting_approval: 1 } });
    expect(agentActivity(busy, true, NOW).text).toBe("2 running · 1 queued · 1 awaiting approval");
    expect(agentActivity(busy, false, NOW)).toEqual({ text: "2 running · 1 queued · 1 awaiting approval · worker not reporting", tone: "warn" });
    expect(agentActivity(agent("monitoring", { last_run_at: iso(5 * MIN) }), true, NOW).text).toBe("Idle · ran 5 min ago");
    expect(agentActivity(agent("monitoring"), true, NOW).text).toBe("Idle · not run yet");
    // An older API without `active` claims no idleness it cannot know.
    expect(agentActivity(agent("monitoring", { active: undefined }), true, NOW).text).toBe("Not run yet");

    expect(successRate(agent("x", { stats_24h: { succeeded: 9, failed: 1, queued: 4 } }))).toEqual({ rate: 0.9, runs: 10, failed: 1 });
    expect(successRate(agent("x", { stats_24h: { queued: 2 } })).rate).toBeNull();
  });

  it("shows keywords only for capabilities the agent has", () => {
    const wp = agent("wordpress_security", { capabilities: [cap("security_scan"), cap("repair_flush_caches")] });
    expect(agentKeywords(wp)).toEqual(["Security scans", "Repairs"]);
    expect(agentKeywords({ name: "newcomer", capabilities: [cap("count_widgets")] })).toEqual(["Count widgets"]);
  });

  it("writes readable task text, hides credit-repair detail and measures durations honestly", () => {
    const ctx = { sites: new Map([[1, site(1)]]), servers: new Map([[4, { id: 4, name: "cwp-1" } as Server]]), user: viewer };
    expect(taskText(task(1), ctx)).toBe("Check website: Site 1");
    expect(taskText(task(2, { agent: "server_management", capability: "server_overview", params: { server_id: 4 } }), ctx)).toBe("Server overview: cwp-1");
    expect(taskText(task(3, { agent: "wordpress_security", capability: "repair_update_plugin", params: { site_id: 1, slug: "akismet" } }), ctx)).toBe("Repair: Update plugin akismet on Site 1");
    expect(taskText(task(4, { agent: "seo_content", capability: "draft_blog_post", params: { topic: "Spring tips" } }), ctx)).toBe("Draft blog post: Spring tips");
    expect(taskText(task(5, { agent: "credit_repair", capability: "draft_dispute_letters", params: { client_id: 7 } }), ctx)).toBe("Credit repair task");
    expect(taskText(task(5, { agent: "credit_repair", capability: "draft_dispute_letters" }), { ...ctx, user: admin })).toBe("Draft dispute letters");
    expect(isInternal({ agent: "supervisor", capability: "plan_request" })).toBe(true);
    expect(isInternal({ agent: "supervisor", capability: "daily_briefing" })).toBe(false);

    expect(statusPill("awaiting_approval")).toEqual({ label: "Needs approval", tone: "approval" });
    expect(statusPill("succeeded").label).toBe("Completed");
    expect(taskDuration(task(1, { status: "queued", started_at: null, finished_at: null }), NOW)).toBeNull();
    expect(taskDuration(task(1, { status: "running", started_at: iso(90_000), finished_at: null }), NOW)).toBe(90_000);
    expect(formatDuration(134_000)).toBe("2m 14s");
    expect(formatDuration(400)).toBe("<1s");
  });
});

describe("service status rows", () => {
  const fresh = { error: null, lastSuccessAt: NOW - 2_000 };
  const loaded = <T,>(data: T) => ({ data, error: null });

  it("says what was checked and when, and never claims more than the data shows", () => {
    const status = makeStatus({ time: new Date(NOW).toISOString(), n8n: { name: "n8n", last_ok_at: iso(40_000), last_error_at: null, last_error: "", detail: {} } });
    const servers = [{ id: 1, name: "cwp", enabled: true, last_seen_at: iso(12 * MIN) } as Server];
    const rows = serviceRows(status, fresh, loaded(servers), loaded(integrations), NOW);
    const by = Object.fromEntries(rows.map((r) => [r.key, r]));
    expect(rows.map((r) => r.name)).toEqual(["n8n", "PostgreSQL / API", "Worker", "AI planner", "CWP servers", "Email", "Postonomous"]);
    expect(by.engine).toMatchObject({ tone: "good", state: "Running", detail: "Probed 40 s ago" });
    expect(by.database).toMatchObject({ tone: "good", state: "Running", detail: "Answered 2 s ago" });
    expect(by.ai).toMatchObject({ tone: "muted", state: "Rule-based", detail: "No Anthropic API key set" });
    expect(by.servers).toMatchObject({ tone: "good", state: "Verified", detail: "1 server · SSH contact 12 min ago" });
    expect(by.email).toMatchObject({ state: "Mock", detail: "Recorded, not sent" });
    expect(by.postonomous).toMatchObject({ state: "Mock", detail: "Recorded, not published" });
  });

  it("covers n8n not checked recently, AI failures, servers never or long ago contacted, and no servers", () => {
    const status = makeStatus({
      time: new Date(NOW).toISOString(),
      n8n: { name: "n8n", last_ok_at: iso(10 * MIN), last_error_at: null, last_error: "", detail: {} },
      llm: { configured: true, provider: "anthropic", model: "claude-x", requests_today: 3, daily_cap: 200, last_ok_at: iso(HOUR), last_error_at: iso(MIN), last_error: "overloaded" },
    });
    const rows = (servers: Server[]) => Object.fromEntries(serviceRows(status, fresh, loaded(servers), loaded(integrations), NOW).map((r) => [r.key, r]));
    const r = rows([]);
    expect(r.engine).toMatchObject({ tone: "unknown", state: "Not checked recently" });
    expect(r.ai).toMatchObject({ tone: "warn", state: "Last call failed", title: "overloaded" });
    expect(r.servers).toMatchObject({ state: "None added", detail: "No servers added" });
    expect(rows([{ id: 1, name: "a", enabled: true, last_seen_at: null } as Server]).servers).toMatchObject({ tone: "unknown", state: "Not checked" });
    expect(rows([{ id: 1, name: "a", enabled: true, last_seen_at: iso(3 * DAY) } as Server]).servers).toMatchObject({ tone: "unknown", state: "Not checked in 3 days" });
  });

  it("turns rows judged from the status into Unknown when the status is stale, and the database row red when the API fails", () => {
    const status = makeStatus();
    const stale = serviceRows(status, { error: null, lastSuccessAt: NOW - 60_000 }, loaded([]), loaded(integrations), NOW);
    expect(stale.filter((r) => ["engine", "worker", "ai", "database"].includes(r.key)).map((r) => r.state)).toEqual(["Unknown", "Unknown", "Unknown", "Unknown"]);
    const failing = serviceRows(status, { error: "HTTP 502", lastSuccessAt: NOW - 60_000 }, loaded([]), loaded(integrations), NOW);
    expect(failing.find((r) => r.key === "database")).toMatchObject({ tone: "bad", state: "Not answering" });
    expect(failing.find((r) => r.key === "worker")!.state).toBe("Unknown");
    const never = serviceRows(null, { error: "HTTP 502", lastSuccessAt: null }, loaded([]), loaded(integrations), NOW);
    expect(never.slice(0, 4).map((r) => r.state)).toEqual(["Unknown", "Not answering", "Unknown", "Unknown"]);
    const unavailable = serviceRows(status, fresh, { data: null, error: "HTTP 500" }, { data: null, error: "HTTP 500" }, NOW);
    expect(unavailable.find((r) => r.key === "servers")!.state).toBe("Unavailable");
    expect(unavailable.find((r) => r.key === "email")!.state).toBe("Unavailable");
  });
});

describe("Workflow Activity chart", () => {
  it("orders series by the validated legend order and draws one line per category with tasks", () => {
    const data = activity();
    expect(seriesOf(data).map((s) => s.key)).toEqual(["website_scans", "seo", "email", "content", "reports", "server_checks", "credit_repair", "repairs"]);
    expect(seriesOf(data)[0].color).toBe("var(--agent-monitoring)");
    expect(seriesOf({ ...data, categories: [...data.categories, { key: "later", label: "Later" }] }).at(-1)).toMatchObject({ key: "later", color: "var(--agent-other)" });
    expect(yTicks(3)).toEqual([0, 1, 2, 3, 4]);
    expect(yTicks(37)).toEqual([0, 10, 20, 30, 40]);
    const { container } = render(<WorkflowActivityChart data={data} />);
    expect(container.querySelectorAll(".wfa-line")).toHaveLength(2);
    const legend = screen.getByRole("list", { name: "Tasks finished, last 24 hours, by kind" });
    expect(within(legend).getAllByRole("listitem")).toHaveLength(8);
    expect(within(legend).getByText("Website scans").parentElement).toHaveTextContent("6");
  });

  it("is one tab stop read with the arrow keys, Home and End", () => {
    const { container } = render(<WorkflowActivityChart data={activity()} />);
    const plot = screen.getByRole("group", { name: /^Tasks finished per hour, last 24 hours: 9 in total/ });
    expect(container.querySelectorAll('[tabindex="0"]')).toHaveLength(1);
    act(() => plot.focus());
    const live = container.querySelector('[aria-live="polite"]')!;
    expect(live).toHaveTextContent(/10 AM.*4 Website scans, 2 SEO.*1 failed/);
    fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(live).toHaveTextContent(/9 AM.*2 Website scans, 1 SEO/);
    fireEvent.keyDown(plot, { key: "Home" });
    expect(live).toHaveTextContent(/8 AM.*0 Website scans/);
    fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(live).toHaveTextContent(/8 AM/);
    fireEvent.keyDown(plot, { key: "End" });
    expect(live).toHaveTextContent(/10 AM/);
    expect(container.querySelector(".wfa-tooltip")).not.toBeNull();
  });

  it("has a table view and an empty state", () => {
    const empty = activity({ totals: {}, failed: 0, buckets: activity().buckets.map((b) => ({ ...b, counts: {}, failed: 0 })) });
    render(<WorkflowActivityChart data={empty} />);
    expect(screen.getByText("No tasks finished in the last 24 hours")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Starting", "Website scans", "SEO", "Email & clients", "Content & marketing", "Reports", "Server checks", "Credit repair", "Repairs", "Failed",
    ]);
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Show chart" }));
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("dashboard page", () => {
  it("shows the reference title, the approvals strip only while approvals wait, and live stat cards", async () => {
    const calls = mockFetch(routes({ "GET /api/system/status": makeStatus({ counts: { ...makeStatus().counts, approvals_pending: 2 } }) }));
    render(<App />);
    expect(await screen.findByRole("heading", { level: 1, name: "Welcome to Infinite AI Command Center" })).toBeInTheDocument();
    expect(screen.getByText("Your AI workforce for websites, marketing, servers, email, and credit repair.")).toBeInTheDocument();
    const strip = await screen.findByText("2 actions are waiting for your approval");
    expect(within(strip.closest(".dash-approvals") as HTMLElement).getByRole("link", { name: "Review" })).toHaveAttribute("href", "/approvals");

    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("7AI AgentsActive"));
    expect(statLink(/Websites/)).toHaveTextContent("2Websites1 down");
    await waitFor(() => expect(statLink(/Tasks Today/)).toHaveTextContent("12, 1 failedTasks Today20% vs yesterday"));
    expect(statLink(/Website Uptime/)).toHaveTextContent("100%Website UptimeLast 30 days");
    expect(within(region("Date, time and system status")).getByRole("status")).toHaveTextContent("System Online");
    // The chart and the Tasks Today card ask in the browser's time zone.
    const asked = calls.find((c) => c.path.startsWith("/api/stats/task-activity"))!.path;
    expect(asked).toMatch(/^\/api\/stats\/task-activity\?range=24h&tz=/);
  });

  it("hides the approvals strip when nothing waits and never invents figures before the data arrives", async () => {
    mockFetch(routes({ "GET /api/agents": { status: 500, json: { detail: "boom" } }, "GET /api/stats/task-activity": { status: 500, json: { detail: "down" } } }));
    render(<App />);
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByText(/waiting for your approval/)).toBeNull();
    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("—AI AgentsUnavailable"));
    await waitFor(() => expect(statLink(/Tasks Today/)).toHaveTextContent("—Tasks TodayUnavailable"));
    expect(within(region("Workflow Activity")).getByText("Activity unavailable: down")).toBeInTheDocument();
    // Background poll failures are shown in their panel without interrupting as alerts.
    expect(screen.queryAllByRole("alert").filter((a) => a.closest(".dash"))).toHaveLength(0);
  });

  it("states uptime coverage, low uptime in words, and too few checks", async () => {
    const young = site(1, { uptime: { "24h": 97, "7d": 97, "30d": 97, checks_30d: 40, n_30d: 40, avg_response_ms_24h: 1, first_check_at: new Date(2026, 9, 12).toISOString() } });
    mockFetch(routes({ "GET /api/sites": { sites: [young] } }));
    const { unmount } = render(<App />);
    await waitFor(() => expect(statLink(/Website Uptime/)).toHaveTextContent(/97\.0%Website UptimeBelow 98% · since Oct 12/));
    unmount();
    mockFetch(routes({ "GET /api/sites": { sites: [site(1, { uptime: { "24h": 100, "7d": 100, "30d": 100, checks_30d: 3, avg_response_ms_24h: 1 } })] } }));
    render(<App />);
    await waitFor(() => expect(statLink(/Website Uptime/)).toHaveTextContent("—Website UptimeNot enough data yet"));
  });

  it("shows the seven real agents with status, work in progress and the success rate, and locks Credit Repair without sensitive access", async () => {
    const agents = [
      agent("supervisor", { role: "supervisor" }),
      ...AGENTS.map((n) =>
        n === "seo_content"
          ? agent(n, { enabled: false })
          : n === "monitoring"
            ? agent(n, { active: { running: 2, queued: 1, awaiting_approval: 0 }, stats_24h: { succeeded: 9, failed: 1 } })
            : agent(n),
      ),
    ];
    mockFetch(routes({ "GET /api/auth/me": { user: operator }, "GET /api/agents": { agents } }));
    render(<App />);
    const section = await screen.findByRole("region", { name: "AI Agents" });
    const cards = await within(section).findAllByRole("listitem");
    expect(cards.map((c) => within(c).getByRole("heading", { level: 3 }).textContent)).toEqual([
      "WordPress Agent", "SEO Agent", "Marketing Agent", "Server Agent", "Email Agent", "Monitoring Agent", "Credit Repair Agent",
    ]);
    expect(cards[1]).toHaveTextContent("Switched off");
    expect(cards[5]).toHaveTextContent("Needs attention");
    expect(cards[5]).toHaveTextContent("2 running · 1 queued");
    expect(cards[5]).toHaveTextContent("24 h success rate90%");
    expect(cards[0]).toHaveTextContent("No runs in 24 h");
    expect(within(cards[0]).getByRole("link", { name: "Open WordPress Agent" })).toHaveAttribute("href", "/websites");
    const locked = within(cards[6]).getByRole("button", { name: "Open" });
    expect(locked).toBeDisabled();
    expect(locked).toHaveAccessibleDescription("Needs sensitive-data access");
    expect(statLink(/AI Agents/)).toHaveTextContent("6 of 7 active");
    expect(within(section).getByRole("link", { name: "Manage Agents" })).toHaveAttribute("href", "/agents");
  });

  it("lets admins open Credit Repair and shows the PC resources honestly, without gauges", async () => {
    mockFetch(routes());
    render(<App />);
    const section = await screen.findByRole("region", { name: "AI Agents" });
    expect(await within(section).findByRole("link", { name: "Open Credit Repair Agent" })).toHaveAttribute("href", "/credit-repair");
    expect(region("PC Resources")).toHaveTextContent("Not connected");
    expect(screen.queryAllByRole("meter")).toHaveLength(0);
    expect(within(region("Service Status")).getByRole("link", { name: /View all/i })).toHaveAttribute("href", "/integrations");
  });

  it("lists recent tasks without bookkeeping, hides scheduled checks until asked, and opens a task's details", async () => {
    const tasks = [
      task(9, { agent: "supervisor", capability: "plan_request", params: { request_id: 1 } }),
      task(8, { schedule_id: 1, agent: "monitoring", capability: "check_all_sites", params: {} }),
      task(7, { status: "awaiting_approval", agent: "wordpress_security", capability: "repair_flush_caches", started_at: null, finished_at: null }),
      task(6, { status: "queued", started_at: null, finished_at: null }),
      task(5),
    ];
    mockFetch(
      routes({
        "GET /api/tasks/5": { task: tasks[4], logs: [], approval: null },
        "GET /api/tasks": { tasks, total: tasks.length },
      }),
    );
    render(<App />);
    const panel = await screen.findByRole("region", { name: "Recent Tasks" });
    const table = await within(panel).findByRole("table");
    const rows = () => within(table).getAllByRole("row").slice(1);
    expect(rows()).toHaveLength(3);
    expect(rows()[0]).toHaveTextContent("Repair: Flush caches on Site 1");
    expect(within(rows()[0]).getByRole("link", { name: "Needs approval" })).toHaveAttribute("href", "/approvals");
    expect(rows()[0]).toHaveTextContent("—");
    expect(rows()[1]).toHaveTextContent("Queued");
    expect(rows()[2]).toHaveTextContent("Completed");
    expect(rows()[2]).toHaveTextContent("2m 14s");
    expect(table).not.toHaveTextContent("Plan request");

    const toggle = within(panel).getByRole("button", { name: /Scheduled checks \(1 hidden\)/ });
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(rows()).toHaveLength(4);
    expect(rows()[0]).toHaveTextContent("Check all websites");

    fireEvent.click(within(rows()[3]).getByRole("button", { name: "Check website: Site 1" }));
    expect(await screen.findByRole("dialog", { name: "Task #5" })).toBeInTheDocument();
  });

  it("asks before pausing a schedule, then patches it; a failed change is rolled back", async () => {
    let fail = true;
    const calls = mockFetch(
      routes({
        "PATCH /api/schedules/1": () => (fail ? { status: 500, json: { detail: "database busy" } } : { json: { schedule: { ...schedules[0], enabled: false } } }),
        "PATCH /api/schedules/2": { json: { schedule: { ...schedules[1], enabled: true } } },
      }),
    );
    render(<App />);
    const panel = await screen.findByRole("region", { name: "Active Workflows" });
    const sweep = await within(panel).findByRole("switch", { name: "Run WordPress uptime sweep on schedule" });
    expect(sweep).toHaveAttribute("aria-checked", "true");

    fireEvent.click(sweep);
    const dialog = await screen.findByRole("dialog", { name: "Pause WordPress uptime sweep?" });
    expect(dialog).toHaveTextContent("nothing checks for problems automatically");
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep running" }));
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);

    fireEvent.click(sweep);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Pause" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Could not pause WordPress uptime sweep: database busy");
    expect(sweep).toHaveAttribute("aria-checked", "true");
    expect(calls.find((c) => c.method === "PATCH" && c.path === "/api/schedules/1")!.body).toEqual({ enabled: false });

    // Resuming a paused schedule needs no confirmation.
    fireEvent.click(within(panel).getByRole("switch", { name: "Run WordPress security report on schedule" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.path === "/api/schedules/2")).toBe(true));
    expect(calls.find((c) => c.path === "/api/schedules/2")!.body).toEqual({ enabled: true });
    expect(screen.queryByRole("dialog")).toBeNull();
    fail = false;
  });

  it("shows n8n workflows read-only, links admins to the n8n editor, and gives viewers no switches", async () => {
    mockFetch(routes());
    const { unmount } = render(<App />);
    const panel = await screen.findByRole("region", { name: "Active Workflows" });
    const n8n = await within(panel).findByRole("list", { name: "n8n workflows (change them in n8n)" });
    expect(n8n).toHaveTextContent("Morning briefing");
    expect(n8n).toHaveTextContent("Active in n8n");
    expect(within(n8n).queryByRole("switch")).toBeNull();
    const link = within(panel).getByRole("link", { name: /View in n8n/ });
    expect(link).toHaveAttribute("href", "http://localhost:5680");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    unmount();

    mockFetch(routes({ "GET /api/auth/me": { user: viewer }, "GET /api/integrations": { integrations: [{ ...integrations[0], workflows: null }] } }));
    render(<App />);
    const viewerPanel = await screen.findByRole("region", { name: "Active Workflows" });
    for (const s of await within(viewerPanel).findAllByRole("switch")) expect(s).toBeDisabled();
    expect(within(viewerPanel).queryByRole("link", { name: /View in n8n/ })).toBeNull();
    expect(await within(viewerPanel).findByText("Add N8N_API_KEY to list n8n workflows")).toBeInTheDocument();
  });

  it("counts monitored sites only, says when all are paused, and names the workflow engine as configured (replaces the 0.4.0 tile tests)", async () => {
    mockFetch(routes({ "GET /api/sites": { sites: [site(1), site(2, { enabled: false, last_status: "down" })] } }));
    const { unmount } = render(<App />);
    await waitFor(() => expect(statLink(/Websites/)).toHaveTextContent("1WebsitesMonitored"));
    unmount();

    mockFetch(
      routes({
        "GET /api/sites": { sites: [site(2, { enabled: false, last_status: "down" })] },
        "GET /api/system/status": makeStatus({ workflow_engine: "webhook", n8n: null }),
      }),
    );
    const second = render(<App />);
    await waitFor(() => expect(statLink(/Websites/)).toHaveTextContent("0WebsitesAll paused"));
    const service = within(region("Service Status")).getAllByRole("listitem");
    expect(service[0]).toHaveTextContent("WebhookNo delivery yet");
    second.unmount();

    mockFetch(routes({ "GET /api/system/status": makeStatus({ workflow_engine: "none", n8n: null }) }));
    render(<App />);
    await waitFor(() => expect(within(region("Service Status")).getAllByRole("listitem")[0]).toHaveTextContent("Workflow engineNot configured"));
  });

  it("asks for another range and remembers it", async () => {
    const calls = mockFetch(routes({ "GET /api/stats/task-activity": (_b: unknown, _i: RequestInit) => ({ json: activity() }) }));
    render(<App />);
    const select = await screen.findByRole("combobox", { name: "Time range" });
    fireEvent.change(select, { target: { value: "7d" } });
    await waitFor(() => expect(calls.some((c) => c.path.startsWith("/api/stats/task-activity?range=7d&tz="))).toBe(true));
    expect(localStorage.getItem("cc.dashboard.range")).toBe("7d");
  });
});
