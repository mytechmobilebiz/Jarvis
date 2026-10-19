// 0.5.0 front-end review fixes (FR-01 to FR-09): every server judged on its own, no stale row left green, a stat value and its
// note read apart, one Escape closes one dialog, the webhook engine never "connected", agent data that is not current says
// so, green only for agents that ran, and site keys listed with the address the server reports.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useCallback, useState } from "react";
import { flushSync } from "react-dom";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Agent, Integration, Server, SystemStatus } from "../api";
import App from "../App";
import { Modal } from "../components";
import { serviceRows } from "../dashboard/service";
import { StatCard } from "../dashboard/StatCards";
import { engineState, serviceHealth } from "../status";
import { mockFetch } from "./fetchMock";

const NOW = Date.now();
const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();

function makeStatus(over: Partial<SystemStatus> = {}): SystemStatus {
  return {
    app: "Infinite AI Command Center",
    version: "0.5.0",
    environment: "test",
    time: new Date(NOW).toISOString(),
    database: { ok: true },
    workers: [{ worker_id: "w1", last_seen_at: new Date().toISOString(), alive: true, info: {} }],
    llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200, reason: "no Anthropic API key set" },
    workflow_engine: "n8n",
    n8n: { name: "n8n", last_ok_at: new Date().toISOString(), last_error_at: null, last_error: "", detail: {} },
    counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
    ...over,
  };
}

const fresh = { error: null, lastSuccessAt: NOW - 2_000 };
const loaded = <T,>(data: T) => ({ data, error: null });
const integrations: Integration[] = [
  { name: "email", title: "Email", mode: "live", description: "", live_requirements: "", status: { name: "email", last_ok_at: iso(5 * MIN), last_error_at: null, last_error: "", detail: {} } },
  { name: "postonomous", title: "Postonomous", mode: "mock", description: "", live_requirements: "", status: null },
];
const server = (id: number, name: string, seenAgo: number | null, over: Partial<Server> = {}) =>
  ({ id, name, enabled: true, host_key_pinned: true, last_seen_at: seenAgo === null ? null : iso(seenAgo), last_hello: {}, ...over }) as Server;
const rowsBy = (rows: ReturnType<typeof serviceRows>) => Object.fromEntries(rows.map((r) => [r.key, r]));

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("CWP servers row judges every server (FR-01)", () => {
  it("is not Verified while one server was never or long ago contacted, and names the worst", () => {
    const servers = [server(1, "cwp-new", 10 * MIN), server(2, "cwp-old", 5 * DAY), server(3, "cwp-unpinned", null, { host_key_pinned: false })];
    const row = rowsBy(serviceRows(makeStatus(), fresh, loaded(servers), loaded(integrations), NOW)).servers;
    expect(row).toMatchObject({ tone: "unknown", state: "1 of 3 verified", detail: "cwp-unpinned: no SSH contact yet · 1 more not verified" });
    expect(row.title).toBe("cwp-old: last SSH contact 5 days ago\ncwp-unpinned: no SSH contact yet");

    const two = rowsBy(serviceRows(makeStatus(), fresh, loaded(servers.slice(0, 2)), loaded(integrations), NOW)).servers;
    expect(two).toMatchObject({ tone: "unknown", state: "1 of 2 verified", detail: "cwp-old: not checked in 5 days" });
  });

  it("is Verified only when every switched-on server had contact within a day, and gives the oldest contact", () => {
    const servers = [server(1, "a", 10 * MIN), server(2, "b", 5 * HOUR), server(3, "off", null, { enabled: false })];
    expect(rowsBy(serviceRows(makeStatus(), fresh, loaded(servers), loaded(integrations), NOW)).servers).toMatchObject({
      tone: "good",
      state: "Verified",
      detail: "2 servers · oldest SSH contact 5 h ago",
    });
    const never = rowsBy(serviceRows(makeStatus(), fresh, loaded([server(1, "a", null), server(2, "b", null)]), loaded(integrations), NOW)).servers;
    expect(never).toMatchObject({ tone: "unknown", state: "0 of 2 verified", detail: "a: no SSH contact yet · 1 more not verified" });
  });

  it("measures the age on the API's clock, so a PC clock that is behind cannot make an old contact look fresh", () => {
    const pcBehind = NOW - 2 * DAY;
    const row = rowsBy(serviceRows(makeStatus(), { error: null, lastSuccessAt: pcBehind }, loaded([server(1, "a", 2 * DAY + 10 * MIN)]), loaded(integrations), pcBehind)).servers;
    expect(row).toMatchObject({ tone: "unknown", state: "Not checked in 2 days" });
  });

  it("turns Unknown when the server list or the status is no longer current, and so do the channel rows", () => {
    const servers = [server(1, "a", 10 * MIN)];
    const failed = rowsBy(serviceRows(makeStatus(), fresh, { data: servers, error: "Failed to fetch" }, { data: integrations, error: "HTTP 502" }, NOW));
    expect(failed.servers).toMatchObject({ tone: "unknown", state: "Unknown", detail: "Not updated · Failed to fetch" });
    expect(failed.email).toMatchObject({ tone: "unknown", state: "Unknown", detail: "Not updated · HTTP 502" });

    // A full outage: the status poll fails before the slower server poll does. Nothing may stay green.
    const outage = serviceRows(makeStatus(), { error: "Failed to fetch", lastSuccessAt: NOW - 40_000 }, loaded(servers), loaded(integrations), NOW);
    expect(outage.filter((r) => r.tone === "good")).toEqual([]);
    expect(outage.filter((r) => r.key !== "database").every((r) => r.state === "Unknown" && r.tone === "unknown")).toBe(true);
    expect(rowsBy(outage).database).toMatchObject({ tone: "bad", state: "Not answering" });
  });
});

describe("stat card value and note (FR-02)", () => {
  it("separates the value from its note for screen readers: '39, 2 failed', never '392 failed'", () => {
    render(
      <MemoryRouter>
        <StatCard to="/tasks" badge={null} tone="green" value="39" note={<span className="stat-note">2 failed</span>} label="Tasks Today" sub="No comparison yet" subTone="muted" />
      </MemoryRouter>,
    );
    const link = screen.getByRole("link");
    // jsdom puts a space around each element ("39 , 2 failed"); either way the value and the note are read apart.
    expect(link).toHaveAccessibleName(/^39 ?, 2 failed\s*Tasks Today/);
    expect(link.querySelector(".stat-value")!.textContent).toBe("39, 2 failed");
  });

  it("has no stray comma without a note", () => {
    render(
      <MemoryRouter>
        <StatCard to="/agents" badge={null} tone="blue" value="7" label="AI Agents" sub="Active" subTone="good" />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link").querySelector(".stat-value")!.textContent).toBe("7");
  });
});

describe("nested dialogs (FR-03)", () => {
  // `sync` removes a closed dialog at once, as a browser can between two key listeners, so the dialog underneath then looks
  // like the top one.
  function Nested({ sync = false }: { sync?: boolean }) {
    const [outer, setOuter] = useState(true);
    const [inner, setInner] = useState(true);
    const closeInner = () => (sync ? flushSync(() => setInner(false)) : setInner(false));
    // Stable, as a page's own close handler is: the dialog underneath keeps its key listener while the top one closes.
    const closeOuter = useCallback(() => setOuter(false), []);
    return outer ? (
      <Modal title="Server cwp-1" onClose={closeOuter}>
        <p>Host key CHANGED</p>
        <input aria-label="Reason" onKeyDown={(e) => e.key === "Escape" && e.preventDefault()} />
        {inner ? (
          <Modal title="Replace the host key of cwp-1" onClose={closeInner}>
            <input aria-label="Type cwp-1 to confirm" />
          </Modal>
        ) : null}
      </Modal>
    ) : null;
  }

  it("closes only the top dialog on one Escape, then the one underneath on the next", () => {
    render(<Nested />);
    expect(screen.getAllByRole("dialog")).toHaveLength(2);
    fireEvent.keyDown(screen.getByLabelText("Type cwp-1 to confirm"), { key: "Escape" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(screen.getByRole("dialog", { name: "Server cwp-1" })).toHaveTextContent("Host key CHANGED");
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the dialog underneath when the closed one is removed before its listener runs", async () => {
    render(<Nested sync />);
    // Outside act, as a browser dispatches a key press: the top dialog's state change is committed at once, before the
    // listener of the dialog underneath runs.
    const env = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean };
    const was = env.IS_REACT_ACT_ENVIRONMENT;
    env.IS_REACT_ACT_ENVIRONMENT = false;
    try {
      screen.getByLabelText("Type cwp-1 to confirm").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    } finally {
      env.IS_REACT_ACT_ENVIRONMENT = was;
    }
    await act(async () => {});
    expect(screen.getByRole("dialog", { name: "Server cwp-1" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Replace the host key of cwp-1" })).toBeNull();
  });

  it("closes nothing on an Escape a field inside has already used", () => {
    function One() {
      const [open, setOpen] = useState(true);
      return open ? (
        <Modal title="Edit" onClose={() => setOpen(false)}>
          <input aria-label="Search" onKeyDown={(e) => e.key === "Escape" && e.preventDefault()} />
        </Modal>
      ) : null;
    }
    render(<One />);
    fireEvent.keyDown(screen.getByLabelText("Search"), { key: "Escape" });
    expect(screen.getByRole("dialog", { name: "Edit" })).toBeInTheDocument();
  });
});

describe("webhook engine: deliveries only, never 'connected' (FR-04, C2-04)", () => {
  const hook = (ok: number | null, err: number | null) =>
    makeStatus({
      workflow_engine: "webhook",
      n8n: { name: "webhook", last_ok_at: ok === null ? null : iso(ok), last_error_at: err === null ? null : iso(err), last_error: err === null ? "" : "HTTP 404", detail: {} },
    });

  it("has no connected state and no warning just because time has passed", () => {
    expect(engineState(hook(40_000, null), NOW)).toBe("delivered");
    expect(engineState(hook(10 * MIN, null), NOW)).toBe("delivered");
    expect(engineState(hook(3 * DAY, null), NOW)).toBe("delivered");
    expect(engineState(hook(10 * MIN, MIN), NOW)).toBe("failing");
    expect(engineState(hook(null, null), NOW)).toBe("not-contacted");
    expect(serviceHealth(hook(40_000, null), fresh, NOW).n8n).toBe("muted");
    expect(serviceHealth(hook(10 * MIN, null), fresh, NOW).n8n).toBe("muted");
    expect(serviceHealth(hook(10 * MIN, MIN), fresh, NOW).n8n).toBe("warn");
    // n8n itself is unchanged: probed, so an old answer is a warning.
    expect(serviceHealth(makeStatus({ n8n: { name: "n8n", last_ok_at: iso(10 * MIN), last_error_at: null, last_error: "", detail: {} } }), fresh, NOW).n8n).toBe("warn");
  });

  it("says 'Last delivery' in the Service Status row, muted, and warns only after a failed delivery", () => {
    const row = (s: SystemStatus) => rowsBy(serviceRows(s, fresh, loaded([]), loaded(integrations), NOW)).engine;
    expect(row(hook(40_000, null))).toMatchObject({ name: "Webhook", tone: "muted", state: "Last delivery OK", detail: "40 s ago · not probed" });
    expect(row(hook(10 * MIN, null))).toMatchObject({ tone: "muted", state: "Last delivery OK", detail: "10 min ago · not probed" });
    expect(row(hook(10 * MIN, MIN))).toMatchObject({ tone: "warn", state: "Last delivery failed", detail: "Failed 1 min ago", title: "HTTP 404" });
    expect(row(hook(null, null))).toMatchObject({ tone: "muted", state: "No delivery yet" });
  });

  it("shows the last delivery in the sidebar with a neutral dot", async () => {
    mockFetch({
      "GET /api/auth/me": { user: { id: 1, email: "a@test.local", display_name: "Admin", role: "viewer" } },
      "GET /api/system/status": { ...hook(10 * MIN, null), time: new Date().toISOString(), n8n: { name: "webhook", last_ok_at: new Date(Date.now() - 10 * MIN).toISOString(), last_error_at: null, last_error: "", detail: {} } },
      "GET /api/agents": { agents: [] },
      "GET /api/sites": { sites: [] },
      "GET /api/events": { events: [] },
      "GET /api/approvals": { approvals: [] },
      "GET /api/tasks": { tasks: [], total: 0 },
    });
    window.history.pushState({}, "", "/approvals");
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    const line = (await within(nav).findByText("Webhook: last delivery 10 min ago")).closest(".health-line")!;
    expect(line.querySelector(".dot")).toHaveClass("dot-muted");
    expect(nav).not.toHaveTextContent(/Webhook:? connected/);
  });
});

// ---------------------------------------------------------------------------------------------------------------------

const cap = (name: string) => ({ name, description: name, risk: "read" as const, requires_approval: false, params_schema: {}, examples: [] });
function agent(name: string, over: Partial<Agent> = {}): Agent {
  return {
    name,
    role: "agent",
    title: name,
    description: "",
    enabled: true,
    entitled: true,
    sensitive: false,
    integrations: [],
    capabilities: [cap("check_site")],
    stats_24h: {},
    last_run_at: iso(5 * MIN),
    active: { queued: 0, running: 0, awaiting_approval: 0 },
    ...over,
  };
}

function dashboardRoutes(over: Record<string, unknown> = {}) {
  return {
    "GET /api/auth/me": { user: { id: 1, email: "owner@test.local", display_name: "Owner", role: "admin" } },
    "GET /api/system/status": makeStatus({ time: new Date().toISOString() }),
    "GET /api/agents": { agents: [agent("monitoring"), agent("seo_content")] },
    "GET /api/sites": { sites: [] },
    "GET /api/stats/task-activity": { status: 500, json: { detail: "later" } },
    "GET /api/tasks": { tasks: [], total: 0 },
    "GET /api/schedules": { schedules: [] },
    "GET /api/servers": { servers: [] },
    "GET /api/integrations": { integrations: [] },
    "GET /api/events": { events: [] },
    "GET /api/approvals": { approvals: [] },
    "GET /api/supervisor/requests": { requests: [] },
    ...over,
  };
}
const statLink = (name: RegExp) => within(document.querySelector(".stat-row") as HTMLElement).getByRole("link", { name });

describe("agent data that is not current (FR-07)", () => {
  it("says the cards and the AI Agents card are not updated once a later poll fails", async () => {
    let fail = false;
    mockFetch(dashboardRoutes({ "GET /api/agents": () => (fail ? { status: 500, json: { detail: "agents down" } } : { json: { agents: [agent("monitoring"), agent("seo_content")] } }) }));
    render(<App />);
    const section = await screen.findByRole("region", { name: "AI Agents" });
    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("2AI AgentsActive"));
    expect(within(section).queryByText(/Not updated/)).toBeNull();
    fail = true;
    // A tab coming back to the front polls at once.
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(await within(section).findByText("Not updated: agents down")).toBeInTheDocument();
    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("2AI AgentsNot updated"));
    expect(statLink(/AI Agents/).querySelector(".stat-sub")).toHaveClass("tone-text-unknown");
    // The last cards stay readable, and nothing interrupts as an alert.
    expect(within(section).getAllByRole("listitem")).toHaveLength(2);
    expect(within(section).queryByRole("alert")).toBeNull();
  });
});

describe("other stat cards after a failed poll (FR-07, same rule)", () => {
  it("keeps the last figures of Websites, Tasks Today and Website Uptime but says they are not updated", async () => {
    let fail = false;
    const okSites = { sites: [{ id: 1, name: "Shop", url: "https://shop.example", enabled: true, last_status: "up", uptime: { "24h": 100, "7d": 100, "30d": 99.9, checks_30d: 40, n_30d: 40, avg_response_ms_24h: 1 } }] };
    const okActivity = {
      range: "24h", bucket: "hour", tz: "UTC", since: iso(DAY), categories: [], buckets: [], totals: {}, failed: 0,
      today: { succeeded: 12, failed: 0 }, yesterday_same_time: { succeeded: 10, failed: 0 },
    };
    const down = { status: 502, json: { detail: "bad gateway" } };
    mockFetch(
      dashboardRoutes({
        "GET /api/sites": () => (fail ? down : { json: okSites }),
        "GET /api/stats/task-activity": () => (fail ? down : { json: okActivity }),
      }),
    );
    render(<App />);
    await waitFor(() => expect(statLink(/Tasks Today/)).toHaveTextContent("12Tasks Today20% vs yesterday"));
    expect(statLink(/Website Uptime/)).toHaveTextContent("99.9%Website UptimeLast 30 days");
    fail = true;
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    await waitFor(() => expect(statLink(/Tasks Today/)).toHaveTextContent("12Tasks TodayNot updated"));
    await waitFor(() => expect(statLink(/^1\s*Websites/)).toHaveTextContent("1WebsitesNot updated"));
    expect(statLink(/Website Uptime/)).toHaveTextContent("99.9%Website UptimeNot updated");
    for (const name of [/Tasks Today/, /^1\s*Websites/, /Website Uptime/]) expect(statLink(name).querySelector(".stat-sub")).toHaveClass("tone-text-unknown");
  });
});

describe("green only for agents that have run (FR-08, C2-06)", () => {
  it("keeps the AI Agents card neutral while a switched-on agent has never run, and names agents needing attention", async () => {
    mockFetch(dashboardRoutes({ "GET /api/agents": { agents: [agent("monitoring"), agent("seo_content", { last_run_at: null })] } }));
    const { unmount } = render(<App />);
    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("2AI AgentsActive"));
    expect(statLink(/AI Agents/).querySelector(".stat-sub")).toHaveClass("tone-text-muted");
    const cards = within(await screen.findByRole("region", { name: "AI Agents" })).getAllByRole("listitem");
    expect(cards[1].querySelector(".agent-status")).toHaveClass("tone-text-muted");
    expect(cards[1]).toHaveTextContent("Idle · not run yet");
    expect(cards[0].querySelector(".agent-status")).toHaveClass("tone-text-good");
    unmount();

    mockFetch(dashboardRoutes({ "GET /api/agents": { agents: [agent("monitoring"), agent("seo_content", { stats_24h: { succeeded: 1, failed: 1 } })] } }));
    const second = render(<App />);
    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("2AI Agents1 needs attention"));
    expect(statLink(/AI Agents/).querySelector(".stat-sub")).toHaveClass("tone-text-warn");
    second.unmount();

    mockFetch(dashboardRoutes({ "GET /api/agents": { agents: [agent("monitoring")] } }));
    render(<App />);
    await waitFor(() => expect(statLink(/AI Agents/)).toHaveTextContent("1AI AgentsActive"));
    expect(statLink(/AI Agents/).querySelector(".stat-sub")).toHaveClass("tone-text-good");
  });
});

describe("site keys with the address the server reports (FR-09, C3-01)", () => {
  const site = { id: 3, name: "Acme", url: "https://acme.example", client: "", notes: "", enabled: true, last_status: "up", last_checked_at: iso(MIN), last_check: null,
    environment: "staging", server_id: null, server_site_key: "", uptime: null, last_diagnosis: {}, last_diagnosis_at: null };
  const cwp = (sites: Record<string, { url: string }> | undefined) => ({
    id: 1, name: "cwp-1", host: "203.0.113.5", port: 22, username: "ccagent", environment: "staging", host_key_pinned: true, host_key_fingerprint: "SHA256:x",
    host_key_trusted_by: "", host_key_trusted_at: null, enabled: true, notes: "", last_hello: sites ? { sites } : {}, last_seen_at: iso(MIN), created_at: iso(DAY), sites: [],
  });

  async function openLinkForm(server: ReturnType<typeof cwp>) {
    window.history.pushState({}, "", "/websites");
    mockFetch({
      "GET /api/auth/me": { user: { id: 1, email: "owner@test.local", display_name: "Owner", role: "admin" } },
      "GET /api/system/status": makeStatus({ time: new Date().toISOString() }),
      "GET /api/sites": { sites: [site] },
      "GET /api/sites/3": { site },
      "GET /api/sites/3/checks": { checks: [] },
      "GET /api/servers": { servers: [server] },
      "GET /api/schedules": { schedules: [] },
      "GET /api/events": { events: [] },
      "GET /api/approvals": { approvals: [] },
      "GET /api/agents": { agents: [] },
      "GET /api/tasks": { tasks: [], total: 0 },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Acme" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Acme" }));
    await waitFor(() => expect(dialog.getByRole("option", { name: "cwp-1" })).toBeInTheDocument());
    fireEvent.change(dialog.getByLabelText("Server"), { target: { value: "1" } });
    return dialog;
  }

  it("lists each key with its reported address", async () => {
    const dialog = await openLinkForm(cwp({ acme: { url: "https://acme.example" }, shop: { url: "https://shop.example" }, bare: { url: "" } }));
    const options = Array.from(document.querySelectorAll<HTMLOptionElement>("#site-keys option"));
    expect(options.map((o) => [o.value, o.label])).toEqual([
      ["acme", "acme (https://acme.example)"],
      ["shop", "shop (https://shop.example)"],
      ["bare", "bare (no URL reported)"],
    ]);
    expect(dialog.getByLabelText("Site key on the server")).toHaveAccessibleDescription("Pick the key whose address is this site's (https://acme.example); cwp-1 reports 3 keys.");
  });

  it("asks for Test connection first when the server has reported no keys", async () => {
    const dialog = await openLinkForm(cwp(undefined));
    expect(document.querySelectorAll("#site-keys option")).toHaveLength(0);
    expect(dialog.getByLabelText("Site key on the server")).toHaveAccessibleDescription(
      "cwp-1 has reported no site keys yet: run Test connection on the CWP / Servers page first.",
    );
  });
});

describe("sidebar fits the reference window, with Intern 2 and the versions always in view (FR-05)", () => {
  const shellRoutes = (over: Record<string, unknown> = {}) => ({
    "GET /api/auth/me": { user: { id: 1, email: "owner@test.local", display_name: "Owner", role: "admin" } },
    "GET /api/system/status": makeStatus({ time: new Date().toISOString() }),
    "GET /api/events": { events: [] },
    "GET /api/schedules": { schedules: [] },
    "GET /api/agents": { agents: [agent("monitoring", { capabilities: [cap("check_all_sites")] })] },
    "GET /api/sites": { sites: [{ id: 1, name: "Shop", url: "https://shop.example", enabled: true, last_status: "up" }] },
    "GET /api/servers": { servers: [] },
    "GET /api/tasks": { tasks: [], total: 0 },
    ...over,
  });

  it("scrolls the menu and Quick Actions, and pins Intern 2, the system lines and the versions below them", async () => {
    window.history.pushState({}, "", "/schedules");
    mockFetch(shellRoutes());
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    const scroll = nav.querySelector(".sidebar-scroll") as HTMLElement;
    const pinned = nav.querySelector(".sidebar-pinned") as HTMLElement;
    expect(scroll.querySelector("nav")).not.toBeNull();
    expect(within(scroll).getByRole("group", { name: "Quick Actions" })).toBeInTheDocument();
    expect(within(pinned).getByText("Intern 2")).toBeInTheDocument();
    expect(within(pinned).getByText(/^Dashboard v/)).toBeInTheDocument();
    expect(within(pinned).getByText("Worker running")).toBeInTheDocument();
  });

  it("brings a Quick Action's result into view in the scrolling part", async () => {
    window.history.pushState({}, "", "/schedules");
    const created = { id: 41, agent: "monitoring", capability: "check_all_sites", status: "queued", created_at: new Date().toISOString(), params: {}, risk: "read" };
    mockFetch(shellRoutes({ "POST /api/agents/monitoring/tasks": { task: created } }));
    const scrolled: Element[] = [];
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = vi.fn(function (this: Element) {
      scrolled.push(this);
    });
    try {
      render(<App />);
      const nav = await screen.findByRole("complementary", { name: "Main navigation" });
      const scan = await within(nav).findByRole("button", { name: "Scan all websites" });
      await waitFor(() => expect(scan).not.toHaveAttribute("aria-disabled"));
      fireEvent.click(scan);
      const live = nav.querySelector(".qa-live") as HTMLElement;
      await waitFor(() => expect(live).toHaveTextContent("#41"));
      expect(scrolled).toContain(live);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
