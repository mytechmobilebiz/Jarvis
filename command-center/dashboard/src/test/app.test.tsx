import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { mockFetch } from "./fetchMock";

const status = {
  app: "Infinite AI Command Center",
  version: "0.1.0",
  environment: "test",
  time: "2026-10-18T12:00:00Z",
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: new Date().toISOString(), alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  n8n: { name: "n8n", last_ok_at: new Date().toISOString(), last_error_at: null, last_error: "", detail: {} },
  counts: { sites: { up: 2, down: 1 }, tasks_24h: { succeeded: 4 }, tasks_queued: 0, tasks_running: 0, approvals_pending: 1, events_24h: { critical: 1 }, drafts: 0 },
};

const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin" };

const common = {
  "GET /api/system/status": status,
  "GET /api/events": { events: [{ id: 1, at: new Date().toISOString(), kind: "site.down", severity: "critical", source: "monitoring", title: "Broken is DOWN", detail: {}, task_id: 1, forward_status: "sent", forward_attempts: 0, forward_error: "", forwarded_at: null, n8n_ack_at: new Date().toISOString(), n8n_execution_id: "7" }] },
  "GET /api/sites": { sites: [{ id: 3, name: "Broken", url: "https://broken.test", client: "", notes: "", enabled: true, last_status: "down", last_checked_at: null, last_check: null }] },
  "GET /api/supervisor/requests": { requests: [] },
};

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("App", () => {
  it("shows the sign-in form when not signed in, then the dashboard", async () => {
    const calls = mockFetch({
      ...common,
      "GET /api/auth/me": { status: 401, json: { detail: "not signed in" } },
      "POST /api/auth/login": (body: unknown) =>
        (body as { password: string }).password === "right" ? { json: { user: admin } } : { status: 401, json: { detail: "wrong email or password" } },
    });
    render(<App />);
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "admin@test.local" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("wrong email or password");

    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "right" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { level: 1, name: /^Good (morning|afternoon|evening), Admin$/ })).toBeInTheDocument();
    expect(await screen.findByText("Broken is DOWN")).toBeInTheDocument();
    const sites = screen.getByRole("region", { name: "Websites monitored" });
    await waitFor(() => expect(within(sites).getByText("1 down")).toBeInTheDocument());
    expect(within(sites).getByText("1", { selector: ".tile-value" })).toBeInTheDocument();
    const login = calls.find((c) => c.method === "POST" && c.path === "/api/auth/login");
    expect((login!.init.headers as Record<string, string>)["X-Requested-With"]).toBe("command-center");
  });

  it("approves a pending action with the exact parameters shown", async () => {
    window.history.pushState({}, "", "/approvals");
    const approval = {
      id: 9, created_at: new Date().toISOString(), task_id: 42, status: "pending", agent: "seo_content", capability: "publish_draft",
      risk: "external", summary: "Publish post 'Spring tips'", details: { params: { draft_id: 5 } }, decided_by: "", decided_at: null, decision_note: "",
    };
    const calls = mockFetch({
      ...common,
      "GET /api/auth/me": { user: admin },
      "GET /api/approvals?status=pending&limit=500": { approvals: [approval] },
      "POST /api/approvals/9/decision": { json: { approval: { ...approval, status: "approved" } } },
    });
    render(<App />);
    expect(await screen.findByText(/Publish post 'Spring tips'/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Decision note"), { target: { value: "go ahead" } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/approvals/9/decision")).toBe(true));
    const decision = calls.find((c) => c.path === "/api/approvals/9/decision")!;
    expect(decision.body).toEqual({ approve: true, note: "go ahead", confirm: "" });
  });

  it("submits a supervisor request", async () => {
    window.history.pushState({}, "", "/supervisor");
    const calls = mockFetch({
      ...common,
      "GET /api/auth/me": { user: admin },
      "POST /api/supervisor/requests": { json: { request: { id: 5, status: "planning" } } },
      "GET /api/supervisor/requests/5": {
        request: { id: 5, created_at: new Date().toISOString(), source: "dashboard", requested_by: "admin@test.local", message: "Check all websites", status: "completed", planner: "rule-based", plan: { notes: [] }, reply: "Routing this to: monitoring.check_all_sites.", summary: "Results:\n- all good", summarizer: "template", error: "", completed_at: null },
        tasks: [],
      },
    });
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Check all websites" }));
    fireEvent.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/Routing this to/)).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/supervisor/requests")!.body).toEqual({ message: "Check all websites" });
  });

  it("hides operator controls from viewers", async () => {
    window.history.pushState({}, "", "/websites");
    mockFetch({ ...common, "GET /api/auth/me": { user: { ...admin, role: "viewer" } } });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Websites" })).toBeInTheDocument();
    expect(await screen.findByText("Broken")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add site" })).toBeNull();
  });
});
