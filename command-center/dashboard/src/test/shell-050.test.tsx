// 0.5.0 shell: reference header and sidebar, Quick Actions, the Intern 2 card, and stale-status handling (C2-03, C2-04).
import { act, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import releaseFile from "../../../VERSION?raw";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { usePoll } from "../hooks";
import { deviceStatus } from "../shell/DeviceCard";
import { badgeTotal, pageTitle, type NavItem } from "../shell/nav";
import { checkableServers, quickActionBlockers } from "../shell/QuickActions";
import { engineState, isStale, serviceHealth, STATUS_STALE_MS } from "../status";
import { mockFetch } from "./fetchMock";

const T = Date.parse("2026-10-19T10:00:00Z");
const iso = (ms: number) => new Date(ms).toISOString();

const status = {
  app: "Infinite AI Command Center",
  version: "0.5.0",
  environment: "test",
  time: iso(Date.now()),
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: iso(Date.now()), alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200, last_ok_at: null, last_error_at: null, last_error: "" },
  workflow_engine: "n8n",
  n8n: { name: "n8n", last_ok_at: iso(Date.now()), last_error_at: null, last_error: "", detail: {} },
  n8n_editor_url: "http://localhost:5680",
  counts: { sites: { up: 1 }, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
};

const admin = { id: 1, email: "admin@test.local", display_name: "Administrator", role: "admin" };
const operator = { ...admin, display_name: "Olive", role: "operator" };

function agent(name: string, caps: string[], over: Record<string, unknown> = {}) {
  return {
    name, role: "agent", title: name, description: "", enabled: true, entitled: true, sensitive: false, integrations: [], stats_24h: {}, last_run_at: null,
    active: { queued: 0, running: 0, awaiting_approval: 0 },
    capabilities: caps.map((c) => ({ name: c, description: "", risk: "read", requires_approval: false, params_schema: {}, examples: [] })),
    ...over,
  };
}

const agents = [
  agent("monitoring", ["check_all_sites", "uptime_report"]),
  agent("seo_content", ["audit_page"]),
  agent("server_management", ["check_services"]),
  agent("wordpress_security", ["security_report"]),
];
const sites = [
  { id: 1, name: "Shop", url: "https://shop.example", enabled: true, last_status: "up" },
  { id: 2, name: "Old", url: "https://old.example", enabled: false, last_status: "unknown" },
];
const servers = [
  { id: 1, name: "cwp-1", enabled: true, host_key_pinned: true },
  { id: 2, name: "cwp-2", enabled: true, host_key_pinned: false },
  { id: 3, name: "cwp-3", enabled: false, host_key_pinned: true },
];

function task(id: number, agentName: string, capability: string, state = "queued") {
  return { id, agent: agentName, capability, status: state, created_at: iso(Date.now()), params: {}, risk: "read" };
}

function routes(over: Record<string, unknown> = {}) {
  return {
    "GET /api/auth/me": { user: admin },
    "GET /api/system/status": status,
    "GET /api/events": { events: [] },
    "GET /api/schedules": { schedules: [] },
    "GET /api/agents": { agents },
    "GET /api/sites": { sites },
    "GET /api/servers": { servers },
    "GET /api/tasks": { tasks: [], total: 0 },
    ...over,
  };
}

/** The app on a quiet page, so these tests depend on the shell only. */
async function renderShell(over: Record<string, unknown> = {}) {
  window.history.pushState({}, "", "/schedules");
  const calls = mockFetch(routes(over));
  render(<App />);
  const nav = await screen.findByRole("complementary", { name: "Main navigation" });
  return { calls, nav };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
  window.history.pushState({}, "", "/");
});

describe("usePoll", () => {
  it("records when a poll last succeeded and keeps the last data and the error after a failure", async () => {
    let fail = false;
    const { result } = renderHook(() => usePoll(async () => {
      if (fail) throw new Error("API down");
      return 1;
    }, 0));
    await waitFor(() => expect(result.current.lastSuccessAt).not.toBeNull());
    const first = result.current.lastSuccessAt!;
    expect(first).toBeLessThanOrEqual(Date.now());
    expect(result.current.error).toBeNull();

    fail = true;
    await act(() => result.current.refresh());
    expect(result.current.error).toBe("API down");
    expect(result.current.data).toBe(1);
    expect(result.current.lastSuccessAt).toBe(first);
  });
});

describe("status freshness", () => {
  it("is stale after a failed poll or 30 s without a success, not while the first poll is pending", () => {
    expect(isStale({ error: null, lastSuccessAt: null }, T)).toBe(false);
    expect(isStale({ error: null, lastSuccessAt: T - 5_000 }, T)).toBe(false);
    expect(isStale({ error: null, lastSuccessAt: T - STATUS_STALE_MS - 1 }, T)).toBe(true);
    expect(isStale({ error: "HTTP 502", lastSuccessAt: T - 1_000 }, T)).toBe(true);
    expect(isStale({ error: "HTTP 502", lastSuccessAt: null }, T)).toBe(true);
  });

  it("calls n8n connected only when it answered within 3 minutes and has not failed since (C2-04)", () => {
    const s = { ...status, time: iso(T) } as never as Parameters<typeof engineState>[0];
    const n8n = (ok: number | null, err: number | null) => ({ ...s, n8n: { ...status.n8n, last_ok_at: ok ? iso(ok) : null, last_error_at: err ? iso(err) : null } });
    expect(engineState(n8n(T - 60_000, null))).toBe("connected");
    expect(engineState(n8n(T - 4 * 60_000, null))).toBe("not-checked");
    expect(engineState(n8n(T - 60_000, T - 30_000))).toBe("failing");
    expect(engineState(n8n(T - 30_000, T - 60_000))).toBe("connected");
    expect(engineState({ ...s, n8n: null })).toBe("not-contacted");
    expect(engineState({ ...s, workflow_engine: "none" })).toBe("none");
    expect(serviceHealth(n8n(T - 4 * 60_000, null), undefined, T).n8n).toBe("warn");
    expect(serviceHealth(n8n(T - 60_000, null), undefined, T).n8n).toBe("good");
  });

  it("measures n8n's age on the server's clock, so a wrong PC clock cannot make an old check look fresh", () => {
    const s = { ...status, time: iso(T), n8n: { ...status.n8n, last_ok_at: iso(T - 10 * 60_000) } } as never as Parameters<typeof engineState>[0];
    expect(engineState(s, T - 10 * 60_000)).toBe("not-checked");
  });

  it("turns every service 'unknown' while the status is stale, never green", () => {
    const s = status as never as Parameters<typeof serviceHealth>[0];
    expect(serviceHealth(s, { error: null, lastSuccessAt: Date.now() }).worker).toBe("good");
    expect(serviceHealth(s, { error: "HTTP 502", lastSuccessAt: Date.now() })).toEqual({ worker: "unknown", n8n: "unknown", database: "unknown", ai: "unknown" });
    expect(serviceHealth(s, { error: null, lastSuccessAt: Date.now() - 60_000 }).database).toBe("unknown");
  });

  it("shows 'Cannot reach the Command Center' and unknown dots when the status poll fails", async () => {
    const { nav } = await renderShell({ "GET /api/system/status": { status: 503, json: { detail: "unavailable" } } });
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("Cannot reach the Command Center · no update received yet");
    // Never a status from before: no status has arrived, so the lines stay empty rather than green.
    expect(nav.querySelector(".dot-good")).toBeNull();
  });

  it("gives the time of the last good update once a later poll fails, and the dots turn unknown", async () => {
    let n = 0;
    const { nav } = await renderShell({ "GET /api/system/status": () => (n++ === 0 ? { json: status } : { status: 502, json: { detail: "bad gateway" } }) });
    await within(nav).findByText("Worker running");
    expect(screen.queryByText(/Cannot reach the Command Center/)).toBeNull();
    // A tab coming back to the front polls at once.
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    const banner = await screen.findByText(/Cannot reach the Command Center/);
    expect(banner.closest(".alert")).toHaveTextContent(/last update \d{1,2}:\d{2}:\d{2}/);
    expect(within(nav).getByText("Worker unknown")).toBeInTheDocument();
    expect(within(nav).getByText("n8n unknown")).toBeInTheDocument();
    expect(nav.querySelectorAll(".sidebar-foot .dot-unknown").length).toBe(3);
    expect(nav.querySelector(".sidebar-foot .dot-good")).toBeNull();
  });
});

describe("header", () => {
  it("has the reference layout: search field, Execute beside it, gear before bell, round avatar with name and role", async () => {
    await renderShell();
    const input = screen.getByLabelText("Command for the AI Supervisor");
    expect(input).toHaveAttribute("placeholder", "Ask or give a command…");
    expect(input).toHaveAttribute("aria-keyshortcuts", "Control+K Meta+K");
    expect(screen.queryByText("Ctrl K")).toBeNull();
    const execute = screen.getByRole("button", { name: "Execute" });
    expect(execute).toHaveAttribute("type", "submit");
    expect(input.closest(".command-field")!.contains(execute)).toBe(false);
    expect(input.closest("form")!.contains(execute)).toBe(true);

    const header = document.querySelector(".topbar") as HTMLElement;
    const settings = within(header).getByRole("link", { name: "Settings" });
    const bell = within(header).getByRole("button", { name: "Notifications" });
    expect(settings.compareDocumentPosition(bell) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const account = screen.getByRole("button", { name: "Administrator, account menu" });
    expect(account).toHaveTextContent("Administrator");
    expect(within(account).getByText("Administrator", { selector: ".account-role" })).toBeInTheDocument();
  });

  it("shows the role, not the e-mail address, under the name; no gear for operators", async () => {
    await renderShell({ "GET /api/auth/me": { user: operator } });
    const account = screen.getByRole("button", { name: "Olive, account menu" });
    expect(within(account).getByText("Operator")).toBeInTheDocument();
    expect(account).not.toHaveTextContent("admin@test.local");
    expect(within(document.querySelector(".topbar") as HTMLElement).queryByRole("link", { name: "Settings" })).toBeNull();
  });

  it("keeps Execute coloured and says what is missing when nothing was typed, without sending", async () => {
    const { calls } = await renderShell();
    const execute = screen.getByRole("button", { name: "Execute" });
    expect(execute).toBeEnabled();
    fireEvent.click(execute);
    expect(await screen.findByText("Type a command first.")).toBeInTheDocument();
    expect(screen.getByLabelText("Command for the AI Supervisor")).toHaveFocus();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });
});

describe("sidebar", () => {
  it("names Postonomous in the menu and gives its page a meaningful title", async () => {
    const { nav } = await renderShell();
    expect(within(nav).getByRole("link", { name: "Postonomous" })).toHaveAttribute("href", "/social");
    expect(pageTitle("/social")).toBe("Postonomous (social media) · Infinite AI Command Center");
  });

  it("keeps Approvals, Tasks, AI Supervisor and Activity log visible and the rest under 'More', remembered", async () => {
    const { nav } = await renderShell();
    for (const name of ["Approvals", "Tasks", "AI Supervisor", "Activity log"]) expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    const more = within(nav).getByRole("button", { name: "More" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(within(nav).queryByRole("link", { name: "Leads" })).toBeNull();
    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    for (const name of ["Leads", "Client support", "Drafts & reports", "Outbox", "Integrations"]) expect(within(nav).getByRole("link", { name })).toBeInTheDocument();
    expect(localStorage.getItem("cc.nav.more")).toBe("open");
  });

  it("opens 'More' by itself from the saved state and when one of its pages is open", async () => {
    localStorage.setItem("cc.nav.more", "open");
    const first = await renderShell();
    expect(within(first.nav).getByRole("button", { name: "More" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows the open page's entry when that page is under 'More'", async () => {
    window.history.pushState({}, "", "/outbox");
    mockFetch(routes({ "GET /api/outbox": { entries: [] } }));
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    await waitFor(() => expect(within(nav).getByRole("link", { name: "Outbox" })).toHaveAttribute("aria-current", "page"));
  });

  it("works when the browser blocks storage", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const { nav } = await renderShell();
    fireEvent.click(within(nav).getByRole("button", { name: "More" }));
    expect(within(nav).getByRole("link", { name: "Leads" })).toBeInTheDocument();
  });

  it("adds up the badges of the pages behind 'More'", () => {
    const items: NavItem[] = [
      { to: "/a", label: "A", icon: "file", badge: () => 2 },
      { to: "/b", label: "B", icon: "file" },
      { to: "/c", label: "C", icon: "file", badge: () => 1 },
    ];
    expect(badgeTotal(items, status as never)).toBe(3);
    expect(badgeTotal(items, null)).toBe(0);
  });

  it("shows the dashboard and API versions in the system footer", async () => {
    const { nav } = await renderShell();
    await waitFor(() => expect(nav.querySelector(".version-line")).toHaveTextContent(`Dashboard v${__APP_VERSION__} · API v0.5.0`));
  });

  it("builds the dashboard as the release in the VERSION file (0.5.1: stale copies of the version numbers)", () => {
    expect(__APP_VERSION__).toBe(releaseFile.trim());
  });

  it("says a page left open during an update is out of date, offers a reload, and gives no reinstall advice", async () => {
    await renderShell({ "GET /api/system/status": { ...status, version: "9.9.9" }, "GET /version.json": { version: "9.9.9" } });
    const banner = (await screen.findByText(/This page is out of date\./)).closest(".alert") as HTMLElement;
    expect(banner).toHaveTextContent(`The Command Center now runs dashboard version 9.9.9, but this page is still version ${__APP_VERSION__} from before the update.`);
    expect(within(banner).getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(banner).not.toHaveTextContent("install.ps1");
    expect(screen.queryByText(/Version mismatch\./)).toBeNull();
  });

  it("when this page is current and the API is not, says to reload first and then reinstall", async () => {
    const { calls } = await renderShell({ "GET /api/system/status": { ...status, version: "0.2.0" }, "GET /version.json": { version: __APP_VERSION__ } });
    const banner = (await screen.findByText(/Version mismatch\./)).closest(".alert") as HTMLElement;
    await waitFor(() => expect(calls.some((c) => c.path === "/version.json")).toBe(true));
    expect(banner).toHaveTextContent(`This dashboard reports version ${__APP_VERSION__}, but the API it talks to reports version 0.2.0.`);
    expect(banner).toHaveTextContent("If the Command Center was updated while this page was open, reload the page first.");
    expect(banner).toHaveTextContent("If the warning stays, at least one of them was not built from the files of the version you installed");
    expect(banner).toHaveTextContent("run scripts\\install.ps1 again from that version's folder");
    expect(screen.queryByText(/This page is out of date\./)).toBeNull();
  });

  it("falls back to the mismatch message when the served dashboard version cannot be read", async () => {
    await renderShell({ "GET /api/system/status": { ...status, version: "0.2.0" } }); // no /version.json: 404
    expect(await screen.findByText(/Version mismatch\./)).toBeInTheDocument();
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    expect(within(nav).getByText("Versions differ")).toBeInTheDocument();
  });

  it("shows no mismatch banner when the API reports this dashboard's version", async () => {
    await renderShell({ "GET /api/system/status": { ...status, version: __APP_VERSION__ } });
    await screen.findByRole("complementary", { name: "Main navigation" });
    expect(screen.queryByText(/Version mismatch\./)).toBeNull();
  });
});

describe("Intern 2", () => {
  it("is 'Not connected' from its one source of truth, with no device photo", async () => {
    expect(deviceStatus().state).toBe("not-connected");
    expect(deviceStatus().label).toBe("Not connected");
    const { nav } = await renderShell();
    const card = nav.querySelector(".device-card") as HTMLElement;
    expect(within(card).getByText("Intern 2")).toBeInTheDocument();
    expect(within(card).getByText("Not connected")).toBeInTheDocument();
    expect(card.querySelector("img")).toBeNull();
    expect(card).not.toHaveTextContent(/(^|[^t ])Connected/);
  });
});

describe("Quick Actions", () => {
  it("are for operators and admins only", async () => {
    await renderShell({ "GET /api/auth/me": { user: { ...admin, role: "viewer" } } });
    expect(screen.queryByRole("group", { name: "Quick Actions" })).toBeNull();
    const viewer = { ...admin, role: "viewer" } as never;
    const blockers = quickActionBlockers({ user: viewer, agents: { data: [], error: null }, sites: { data: [], error: null }, servers: { data: [], error: null }, active: { data: [], error: null } });
    expect(blockers.scan).toBe("Viewers cannot start actions");
  });

  it("scan all websites: shows the real task only after the API returned it", async () => {
    const calls = mockFetch(routes({ "POST /api/agents/monitoring/tasks": { json: { task: task(42, "monitoring", "check_all_sites") } } }));
    const fetchMock = globalThis.fetch;
    let release: () => void = () => undefined;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) =>
      init?.method === "POST" ? new Promise((resolve) => (release = () => resolve(fetchMock(input, init)))) : fetchMock(input, init),
    );
    window.history.pushState({}, "", "/schedules");
    render(<App />);
    const group = await screen.findByRole("group", { name: "Quick Actions" });
    const scan = within(group).getByRole("button", { name: "Scan all websites" });
    await waitFor(() => expect(scan).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(scan);
    expect(await within(group).findByText("Starting scan all websites…")).toBeInTheDocument();
    expect(within(group).queryByRole("link", { name: /Task #/ })).toBeNull();

    act(() => release());
    const link = await within(group).findByRole("link", { name: "Task #42" });
    expect(link).toHaveAttribute("href", "/tasks?id=42");
    expect(link.parentElement).toHaveTextContent("Task #42 queued");
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/api/agents/monitoring/tasks", body: { capability: "check_all_sites", params: {} } });
  });

  it("reports an API refusal as not started", async () => {
    await renderShell({ "POST /api/agents/monitoring/tasks": { status: 409, json: { detail: "the agent is switched off" } } });
    const group = screen.getByRole("group", { name: "Quick Actions" });
    const scan = within(group).getByRole("button", { name: "Scan all websites" });
    await waitFor(() => expect(scan).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(scan);
    expect(await within(group).findByText("Not started: the agent is switched off")).toBeInTheDocument();
    expect(within(group).queryByRole("link", { name: /Task #/ })).toBeNull();
  });

  it("are disabled with the reason: agent off, no sites, already running, no checkable server", async () => {
    const { calls } = await renderShell({
      "GET /api/agents": { agents: [agent("monitoring", ["check_all_sites", "uptime_report"], { enabled: false }), ...agents.slice(1)] },
      "GET /api/tasks": { tasks: [task(9, "seo_content", "audit_page", "running")], total: 1 },
      "GET /api/servers": { servers: [servers[1], servers[2]] },
    });
    const group = screen.getByRole("group", { name: "Quick Actions" });
    const scan = within(group).getByRole("button", { name: "Scan all websites" });
    await waitFor(() => expect(scan).toHaveAccessibleDescription("Monitoring agent is switched off"));
    expect(scan).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(scan);
    expect(within(group).getByRole("button", { name: "Run SEO audit" })).toHaveAccessibleDescription("Already running: task #9");
    expect(within(group).getByRole("button", { name: "Check servers" })).toHaveAccessibleDescription("No enabled server with a pinned host key");
    fireEvent.click(within(group).getByRole("button", { name: "Check servers" }));
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("names the missing websites and servers", () => {
    const user = admin as never;
    const loaded = <T,>(data: T) => ({ data, error: null });
    const b = quickActionBlockers({ user, agents: loaded(agents as never), sites: loaded([sites[1]] as never), servers: loaded([]), active: loaded([]) });
    expect(b.scan).toBe("No websites being monitored");
    expect(b.security_report).toBe("No websites being monitored");
    expect(b.servers).toBe("No servers added");
    expect(b.seo).toBeNull();
    const notEntitled = quickActionBlockers({ user, agents: loaded([agent("seo_content", ["audit_page"], { entitled: false })] as never), sites: loaded(sites as never), servers: loaded(servers as never), active: loaded([]) });
    expect(notEntitled.seo).toBe("SEO agent: not in this edition");
    expect(notEntitled.scan).toBe("Monitoring agent not available");
  });

  it("check servers: one service check per enabled server with a pinned host key", async () => {
    expect(checkableServers(servers as never).map((s) => s.id)).toEqual([1]);
    const { calls } = await renderShell({ "POST /api/agents/server_management/tasks": { json: { task: task(51, "server_management", "check_services") } } });
    const group = screen.getByRole("group", { name: "Quick Actions" });
    const check = within(group).getByRole("button", { name: "Check servers" });
    await waitFor(() => expect(check).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(check);
    expect(await within(group).findByRole("link", { name: "Task #51" })).toBeInTheDocument();
    expect(within(group).getByText(/cwp-1/)).toBeInTheDocument();
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0].body).toEqual({ capability: "check_services", params: { server_id: 1 } });
  });

  it("run SEO audit: asks for a monitored site or an address, then audits that page", async () => {
    const { calls } = await renderShell({ "POST /api/agents/seo_content/tasks": { json: { task: task(60, "seo_content", "audit_page") } } });
    const group = screen.getByRole("group", { name: "Quick Actions" });
    const open = within(group).getByRole("button", { name: "Run SEO audit" });
    await waitFor(() => expect(open).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog", { name: "Run SEO audit" });
    // Only monitored (enabled) sites are offered.
    expect(within(dialog).queryByRole("option", { name: /Old/ })).toBeNull();
    fireEvent.change(within(dialog).getByLabelText("Page to audit"), { target: { value: "" } });
    fireEvent.change(within(dialog).getByLabelText("Page address"), { target: { value: "javascript:alert(1)" } });
    fireEvent.submit(within(dialog).getByRole("button", { name: "Run SEO audit" }).closest("form")!);
    expect(await within(dialog).findByText(/starts with https:\/\/ or http:\/\//)).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);

    fireEvent.change(within(dialog).getByLabelText("Page to audit"), { target: { value: "https://shop.example" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run SEO audit" }));
    expect(await within(group).findByRole("link", { name: "Task #60" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Run SEO audit" })).toBeNull();
    expect(calls.find((c) => c.method === "POST")).toMatchObject({ path: "/api/agents/seo_content/tasks", body: { capability: "audit_page", params: { url: "https://shop.example" } } });
  });

  it("run SEO audit: an API refusal stays in the dialog and nothing is claimed", async () => {
    await renderShell({ "POST /api/agents/seo_content/tasks": { status: 400, json: { detail: "invalid url" } } });
    const group = screen.getByRole("group", { name: "Quick Actions" });
    const open = within(group).getByRole("button", { name: "Run SEO audit" });
    await waitFor(() => expect(open).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(open);
    const dialog = screen.getByRole("dialog", { name: "Run SEO audit" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Run SEO audit" }));
    expect(await within(dialog).findByText("invalid url")).toBeInTheDocument();
    expect(within(group).queryByRole("link", { name: /Task #/ })).toBeNull();
  });

  it("generate report: security report or uptime report, both saved to Drafts & reports", async () => {
    const { calls } = await renderShell({
      "POST /api/agents/wordpress_security/tasks": { json: { task: task(70, "wordpress_security", "security_report") } },
      "POST /api/agents/monitoring/tasks": { json: { task: task(71, "monitoring", "uptime_report") } },
    });
    const group = screen.getByRole("group", { name: "Quick Actions" });
    const open = within(group).getByRole("button", { name: "Generate report" });
    await waitFor(() => expect(open).not.toHaveAttribute("aria-disabled"));
    fireEvent.click(open);
    fireEvent.click(within(screen.getByRole("dialog", { name: "Generate a report" })).getByRole("button", { name: "Generate security report" }));
    expect(await within(group).findByRole("link", { name: "Task #70" })).toBeInTheDocument();
    expect(within(group).getByRole("link", { name: "Drafts & reports" })).toHaveAttribute("href", "/drafts");
    fireEvent.click(open);
    fireEvent.click(within(screen.getByRole("dialog", { name: "Generate a report" })).getByRole("button", { name: "Generate uptime report" }));
    expect(await within(group).findByRole("link", { name: "Task #71" })).toBeInTheDocument();
    const posts = calls.filter((c) => c.method === "POST");
    expect(posts.map((c) => [c.path, c.body])).toEqual([
      ["/api/agents/wordpress_security/tasks", { capability: "security_report", params: {} }],
      ["/api/agents/monitoring/tasks", { capability: "uptime_report", params: {} }],
    ]);
  });

  it("open n8n: admins only, in a new tab without opener, hidden when the API gives no editor address", async () => {
    await renderShell();
    const link = within(screen.getByRole("group", { name: "Quick Actions" })).getByRole("link", { name: /Open n8n/ });
    expect(link).toHaveAttribute("href", "http://localhost:5680");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("open n8n is hidden for operators and when the address is missing or not http(s)", async () => {
    await renderShell({ "GET /api/auth/me": { user: operator } });
    expect(within(screen.getByRole("group", { name: "Quick Actions" })).queryByRole("link", { name: /Open n8n/ })).toBeNull();
  });

  it("open n8n is hidden without an editor address", async () => {
    await renderShell({ "GET /api/system/status": { ...status, n8n_editor_url: "javascript:alert(1)" } });
    await screen.findByText("API v0.5.0");
    expect(within(screen.getByRole("group", { name: "Quick Actions" })).queryByRole("link", { name: /Open n8n/ })).toBeNull();
  });
});
