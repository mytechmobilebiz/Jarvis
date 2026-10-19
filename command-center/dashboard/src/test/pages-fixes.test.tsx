// Page fixes from the 0.4.0 final review: credit-repair task details, list windows, keyboard paths to row dialogs,
// the Approvals view switch and the Dashboard tiles.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { User } from "../api";
import { canSeeSensitive, hidesTaskDetail } from "../work";
import { mockFetch } from "./fetchMock";

const now = new Date().toISOString();
const status = {
  app: "Infinite AI Command Center",
  version: "0.4.0",
  environment: "test",
  time: now,
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: now, alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  workflow_engine: "n8n",
  n8n: { name: "n8n", last_ok_at: now, last_error_at: null, last_error: "", detail: {} },
  counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
};

const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin", can_access_sensitive: false };
const operator = { ...admin, id: 2, email: "op@test.local", display_name: "Op", role: "operator" };

const task = (over: Record<string, unknown>) => ({
  id: 1, created_at: now, updated_at: now, request_id: null, schedule_id: null, agent: "monitoring", capability: "check_site", params: {},
  risk: "read", status: "succeeded", attempts: 1, started_at: now, finished_at: now, source: "dashboard", created_by: "op@test.local",
  summary: "", result: null, error: "", ...over,
});

// A credit-repair task whose summary, error, parameters and log name a client.
const crTask = task({
  id: 5, agent: "credit_repair", capability: "draft_dispute_letter", status: "failed", params: { client_name: "Jane Roe" },
  summary: "Dispute for Jane Roe", error: "bureau address missing for Jane Roe",
});
const crDetail = { task: crTask, logs: [{ id: 1, at: now, level: "info", message: "drafting for Jane Roe" }], approval: null };

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("credit-repair task details (sec-1)", () => {
  it("uses the same rule as the Credit Repair menu entry", () => {
    const u = (role: User["role"], can_access_sensitive = false) => ({ ...admin, role, can_access_sensitive }) as User;
    expect(canSeeSensitive(u("admin"))).toBe(true);
    expect(canSeeSensitive(u("operator"))).toBe(false);
    expect(canSeeSensitive(u("viewer"))).toBe(false);
    expect(canSeeSensitive(u("viewer", true))).toBe(true);
    expect(hidesTaskDetail({ agent: "credit_repair" }, u("operator"))).toBe(true);
    expect(hidesTaskDetail({ agent: "credit_repair" }, u("operator", true))).toBe(false);
    expect(hidesTaskDetail({ agent: "monitoring" }, u("viewer"))).toBe(false);
  });

  it("hides the summary, error, parameters, log and actions from users without the permission", async () => {
    window.history.pushState({}, "", "/tasks?id=5");
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: operator },
      "GET /api/tasks": { tasks: [crTask, task({ id: 4, summary: "Acme is up" })], total: 2 },
      "GET /api/tasks/5": crDetail,
    });
    render(<App />);
    const dialog = await screen.findByRole("dialog", { name: "Task #5" });
    expect(await within(dialog).findByText("Details of credit-repair tasks need the sensitive-data permission.")).toBeInTheDocument();
    expect(within(dialog).getByText("credit_repair.draft_dispute_letter")).toBeInTheDocument();
    expect(within(dialog).queryByRole("heading", { name: "Parameters" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Retry" })).toBeNull();
    // Neither the dialog nor the task list shows anything about the client.
    expect(screen.queryByText(/Jane Roe/)).toBeNull();
    expect(screen.getByText("Acme is up")).toBeInTheDocument();
    expect(screen.getByTitle("—")).toHaveTextContent("—");
  });

  it("shows everything to a user with the sensitive-data permission", async () => {
    window.history.pushState({}, "", "/tasks?id=5");
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: { ...operator, can_access_sensitive: true } },
      "GET /api/tasks": { tasks: [crTask], total: 1 },
      "GET /api/tasks/5": crDetail,
    });
    render(<App />);
    const dialog = await screen.findByRole("dialog", { name: "Task #5" });
    expect(await within(dialog).findByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(within(dialog).getByText("drafting for Jane Roe", { exact: false })).toBeInTheDocument();
    expect(within(dialog).queryByText("Details of credit-repair tasks need the sensitive-data permission.")).toBeNull();
    expect(screen.getAllByText("bureau address missing for Jane Roe").length).toBe(2); // the list and the dialog
  });

  it("opens a credit-repair event's task from the Automation Logs, guarded by the task dialog", async () => {
    window.history.pushState({}, "", "/logs");
    const event = {
      id: 11, at: now, kind: "credit_repair.letter_drafted", severity: "info", source: "credit_repair", title: "Dispute letter drafted", detail: {},
      task_id: 5, forward_status: "skipped", forward_attempts: 0, forward_error: "", forwarded_at: null, n8n_ack_at: null, n8n_execution_id: "",
    };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: operator },
      "GET /api/events": { events: [event] },
      "GET /api/integrations": { integrations: [] },
      "GET /api/tasks/5": crDetail,
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task 5" }));
    const dialog = await screen.findByRole("dialog", { name: "Task #5" });
    expect(await within(dialog).findByText("Details of credit-repair tasks need the sensitive-data permission.")).toBeInTheDocument();
    expect(screen.queryByText(/Jane Roe/)).toBeNull();
  });
});

describe("keyboard paths to row dialogs (a11y-3)", () => {
  /** The row's primary cell holds a real button that opens the same dialog as a click on the row. */
  async function opensFromButton(name: string | RegExp, dialogName: string | RegExp) {
    const button = await screen.findByRole("button", { name });
    expect(button.tagName).toBe("BUTTON");
    expect(button).toHaveAttribute("type", "button");
    button.focus();
    expect(button).toHaveFocus();
    fireEvent.click(button);
    expect(await screen.findByRole("dialog", { name: dialogName })).toBeInTheDocument();
  }

  it("Tasks: the capability", async () => {
    window.history.pushState({}, "", "/tasks");
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/tasks": { tasks: [task({ id: 7 })], total: 1 },
      "GET /api/tasks/7": { task: task({ id: 7 }), logs: [], approval: null },
    });
    render(<App />);
    await opensFromButton("check_site", "Task #7");
    expect(window.location.search).toBe("?id=7");
  });

  it("Websites: the site's name", async () => {
    window.history.pushState({}, "", "/websites");
    const site = {
      id: 3, name: "Acme", url: "https://acme.example", client: "", notes: "", enabled: true, last_status: "up", last_checked_at: now, last_check: null,
      environment: "test", server_id: null, server_site_key: "", uptime: null, last_diagnosis: null, last_diagnosis_at: null,
    };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/sites": { sites: [site] },
      "GET /api/sites/3": { site },
      "GET /api/sites/3/checks": { checks: [] },
      "GET /api/servers": { servers: [] },
    });
    render(<App />);
    await opensFromButton("Acme", /Acme/);
  });

  it("CWP / Servers: the server's name", async () => {
    window.history.pushState({}, "", "/servers");
    const server = {
      id: 1, name: "cwp-1", host: "203.0.113.5", port: 22, username: "ccagent", environment: "production", host_key_pinned: true,
      host_key_fingerprint: "SHA256:abc", host_key_trusted_by: "admin@test.local", host_key_trusted_at: now, enabled: true, notes: "", last_hello: {},
      last_seen_at: now, created_at: now, sites: [],
    };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: { ...admin, role: "viewer" } },
      "GET /api/servers": { servers: [server] },
      "GET /api/servers/1/commands": { commands: [], total: 0 },
    });
    render(<App />);
    await opensFromButton("cwp-1", /cwp-1/);
  });

  it("Drafts & reports: the title", async () => {
    window.history.pushState({}, "", "/drafts");
    const artifact = { id: 3, created_at: now, task_id: 1, agent: "supervisor", kind: "briefing", title: "Daily briefing", content: "# Hi", content_type: "text/markdown", data: {}, status: "draft" };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/artifacts": { artifacts: [artifact] },
      "GET /api/artifacts/3": { artifact },
    });
    render(<App />);
    await opensFromButton("Daily briefing", "Daily briefing");
  });

  it("Outbox: the action", async () => {
    window.history.pushState({}, "", "/outbox");
    const entry = {
      id: 1, created_at: now, task_id: 4, agent: "communications", integration: "email", mode: "mock", action: "send", recipient: "ann@lee.example",
      subject: "Your proposal", body: "Hi", payload: {}, approved_by: "admin@test.local", result: "recorded, not sent",
    };
    mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/outbox": { entries: [entry] } });
    render(<App />);
    await opensFromButton("email.send", "Outbox #1: email.send");
  });

  it("Leads: the company", async () => {
    window.history.pushState({}, "", "/leads");
    const lead = { id: 7, created_at: now, source: "n8n", name: "Ann Lee", company: "Lee Dental", email: "ann@lee.example", phone: "", website: "", service_interest: "SEO", message: "", score: 82, score_reasons: [], status: "qualified", proposal_artifact_id: null };
    mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/leads": { leads: [lead] } });
    render(<App />);
    await opensFromButton("Lee Dental", "Lee Dental");
  });

  it("Credit Repair: the client's name", async () => {
    window.history.pushState({}, "", "/credit-repair");
    const client = { id: 2, reference: "CR-0002", full_name: "Test Client", status: "active", contract_signed_on: null, services_completed_on: null, fees_collected_cents: 0, created_at: now };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: { ...admin, can_access_sensitive: true } },
      "GET /api/credit-repair/status": { configured: true, entitled: true, enabled: { acknowledged_by: admin.email, acknowledged_at: now }, can_access: true, notice: "" },
      "GET /api/credit-repair/clients": { clients: [client] },
    });
    render(<App />);
    // The client detail is not mocked, so the dialog keeps its loading title.
    await opensFromButton("Test Client", "Client");
  });
});

describe("list windows (cr-2, cr-7)", () => {
  it("Leads asks for 500 and says when the tiles count only the latest 500", async () => {
    window.history.pushState({}, "", "/leads");
    const leads = Array.from({ length: 500 }, (_, i) => ({
      id: i + 1, created_at: now, source: "dashboard", name: `Lead ${i + 1}`, company: "", email: "", phone: "", website: "", service_interest: "", message: "",
      score: null, score_reasons: [], status: "new", proposal_artifact_id: null,
    }));
    const calls = mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/leads": { leads } });
    render(<App />);
    const tile = await screen.findByRole("region", { name: "Leads" });
    await waitFor(() => expect(tile).toHaveTextContent("in the latest 500"));
    expect(screen.queryByRole("region", { name: "Total leads" })).toBeNull();
    expect(calls.some((c) => c.path === "/api/leads?limit=500")).toBe(true);
  });

  it("Client support and Outbox ask for 500 and keep their plain labels below that", async () => {
    window.history.pushState({}, "", "/support");
    let calls = mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/support/tickets": { tickets: [] } });
    const { unmount } = render(<App />);
    const open = await screen.findByRole("region", { name: "Open messages" });
    await waitFor(() => expect(open).toHaveTextContent("of 0"));
    expect(calls.some((c) => c.path === "/api/support/tickets?limit=500")).toBe(true);
    unmount();

    window.history.pushState({}, "", "/outbox");
    calls = mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/outbox": { entries: [] } });
    render(<App />);
    expect(await screen.findByText("No approved outward action yet")).toBeInTheDocument();
    expect(screen.queryByText(/in the latest 500/)).toBeNull();
    expect(calls.some((c) => c.path === "/api/outbox?limit=500")).toBe(true);
  });

  it("Approvals asks for 500 and the view switch states which view is shown (a11y-16)", async () => {
    window.history.pushState({}, "", "/approvals");
    const calls = mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/approvals": { approvals: [] } });
    render(<App />);
    const group = await screen.findByRole("group", { name: "Approvals view" });
    const pending = within(group).getByRole("button", { name: "Pending" });
    const history = within(group).getByRole("button", { name: "History" });
    expect(pending).toHaveAttribute("aria-pressed", "true");
    expect(history).toHaveAttribute("aria-pressed", "false");
    await waitFor(() => expect(calls.some((c) => c.path === "/api/approvals?status=pending&limit=500")).toBe(true));
    fireEvent.click(history);
    expect(history).toHaveAttribute("aria-pressed", "true");
    expect(pending).toHaveAttribute("aria-pressed", "false");
    await waitFor(() => expect(calls.some((c) => c.path === "/api/approvals?status=all&limit=500")).toBe(true));
  });
});

describe("dashboard tiles (cr-3, cr-4, cr-9, sparkline)", () => {
  const site = (id: number, enabled: boolean, last_status: string, up: number | null) => ({
    id, name: `Site ${id}`, url: `https://s${id}.example`, client: "", notes: "", enabled, last_status, last_checked_at: now, last_check: null,
    environment: "production", server_id: null, server_site_key: "", uptime: up == null ? null : { "24h": up, "7d": up, "30d": up }, last_diagnosis: null,
    last_diagnosis_at: null,
  });
  const agent = (name: string, role = "agent") => ({
    name, role, title: name, description: "", enabled: true, entitled: true, sensitive: false, integrations: [], capabilities: [], stats_24h: {}, last_run_at: null,
  });

  it("counts monitored sites only, labels the uptime average and reports no workflow engine as not configured", async () => {
    mockFetch({
      // The status counts still include the paused site that is down.
      "GET /api/system/status": { ...status, workflow_engine: "none", n8n: null, counts: { ...status.counts, sites: { up: 1, down: 1 } } },
      "GET /api/auth/me": { user: admin },
      "GET /api/agents": { agents: [agent("supervisor", "supervisor"), agent("a1"), agent("a2")] },
      "GET /api/sites": { sites: [site(1, true, "up", 100), site(2, false, "down", null)] },
      "GET /api/tasks": { tasks: [], total: 0 },
      "GET /api/approvals": { approvals: [] },
      "GET /api/events": { events: [] },
      "GET /api/supervisor/requests": { requests: [] },
    });
    render(<App />);
    const sites = await screen.findByRole("region", { name: "Websites monitored" });
    await waitFor(() => expect(sites).toHaveTextContent("1 up · 0 down · 0 degraded"));
    expect(sites).toHaveTextContent("1 paused");
    expect(screen.getByRole("region", { name: "Website uptime (24 h)" })).toHaveTextContent("average of 1 site(s)");
    const agents = screen.getByRole("region", { name: "Active AI agents" });
    await waitFor(() => expect(agents).toHaveTextContent("of 2"));
    const health = screen.getByRole("region", { name: "Worker and service health" });
    expect(health).toHaveTextContent("All systems operational");
    expect(within(health).getByText("not configured")).toBeInTheDocument();
  });

  it("says when every site is paused, names a webhook engine and says when it was not contacted yet", async () => {
    mockFetch({
      "GET /api/system/status": { ...status, workflow_engine: "webhook", n8n: null },
      "GET /api/auth/me": { user: admin },
      "GET /api/sites": { sites: [site(2, false, "down", null)] },
    });
    render(<App />);
    const sites = await screen.findByRole("region", { name: "Websites monitored" });
    await waitFor(() => expect(sites).toHaveTextContent("All sites paused"));
    const health = screen.getByRole("region", { name: "Worker and service health" });
    expect(within(health).getByText("Webhook")).toBeInTheDocument();
    expect(within(health).getByText("not contacted yet")).toBeInTheDocument();
    expect(health).not.toHaveTextContent("needs attention");
    // Before the agents list loads there is no made-up total.
    expect(screen.getByRole("region", { name: "Active AI agents" })).not.toHaveTextContent("of 7");
  });

  it("draws the 'completed today' sparkline as a line during the first hour of the day", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 19, 0, 30));
    mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, "GET /api/tasks": { tasks: [], total: 0 } });
    const { container } = render(<App />);
    await waitFor(() => expect(container.querySelector(".sparkline-line")).not.toBeNull());
    const points = container.querySelector(".sparkline-line")!.getAttribute("points")!.trim().split(/\s+/);
    expect(points).toEqual(["0.0,29.0", "120.0,29.0"]);
  });
});
