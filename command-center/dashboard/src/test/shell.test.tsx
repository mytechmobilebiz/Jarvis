import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { mockFetch } from "./fetchMock";

const status = {
  app: "Infinite AI Command Center",
  version: "0.4.0",
  environment: "test",
  time: "2026-10-18T12:00:00Z",
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: new Date().toISOString(), alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  n8n: { name: "n8n", last_ok_at: new Date().toISOString(), last_error_at: null, last_error: "", detail: {} },
  counts: { sites: { up: 1, down: 1 }, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 2, events_24h: {}, drafts: 0 },
};

const admin = { id: 1, email: "admin@test.local", display_name: "Administrator", role: "admin" };

const common = {
  "GET /api/system/status": status,
  "GET /api/events": { events: [] },
  "GET /api/sites": { sites: [] },
  "GET /api/agents": { agents: [] },
  "GET /api/approvals": { approvals: [] },
  "GET /api/tasks": { tasks: [], total: 0 },
  "GET /api/supervisor/requests": { requests: [] },
};

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("Shell", () => {
  it("shows every section in the sidebar and keeps the operations pages", async () => {
    mockFetch({ ...common, "GET /api/auth/me": { user: admin } });
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    for (const label of [
      "Dashboard",
      "AI Agents",
      "Workflows",
      "Websites",
      "SEO & Marketing",
      "CWP / Servers",
      "Email",
      "Credit Repair",
      "Social Media / Postonomous",
      "Automation Logs",
      "Knowledge Base",
      "Settings",
      "Approvals",
      "Tasks",
      "AI Supervisor",
      "Activity log",
    ]) {
      expect(within(nav).getByRole("link", { name: new RegExp(`^${label.replace(/[/&]/g, "\\$&")}`) })).toBeInTheDocument();
    }
    // Pending approvals are counted on the sidebar entry and the bell.
    await waitFor(() => expect(within(nav).getByLabelText("2 need attention")).toBeInTheDocument());
  });

  it("hides admin and sensitive sections from operators", async () => {
    mockFetch({ ...common, "GET /api/auth/me": { user: { ...admin, display_name: "Olive", role: "operator" } } });
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    expect(within(nav).getByRole("link", { name: /^Approvals/ })).toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /^Settings/ })).toBeNull();
    expect(within(nav).queryByRole("link", { name: /^Activity log/ })).toBeNull();
    expect(within(nav).queryByRole("link", { name: /^Credit Repair/ })).toBeNull();
  });

  it("sends a command to the AI Supervisor and shows the answer", async () => {
    const calls = mockFetch({
      ...common,
      "GET /api/auth/me": { user: admin },
      "POST /api/supervisor/requests": { json: { request: { id: 7, status: "planning" } } },
      "GET /api/supervisor/requests/7": {
        request: { id: 7, created_at: new Date().toISOString(), source: "dashboard", requested_by: "admin@test.local", message: "Check all websites", status: "completed", planner: "rule-based", plan: { notes: [] }, reply: "Routing this to: monitoring.check_all_sites.", summary: "Checked 2 site(s).", summarizer: "template", error: "", completed_at: null },
        tasks: [],
      },
    });
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    fireEvent.change(input, { target: { value: "Check all websites" } });
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("Checked 2 site(s).")).toBeInTheDocument();
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/supervisor/requests")!.body).toEqual({ message: "Check all websites" });
    expect(screen.getByRole("link", { name: "Open in AI Supervisor" })).toHaveAttribute("href", "/supervisor?id=7");
  });

  it("does not let viewers send commands and shows no microphone without browser support", async () => {
    const calls = mockFetch({ ...common, "GET /api/auth/me": { user: { ...admin, role: "viewer" } } });
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    expect(input).toBeDisabled();
    expect(screen.getByRole("button", { name: "Execute" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Voice input" })).toBeNull();
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("shows the microphone only where speech recognition is available and asks before using it", async () => {
    class FakeRecognition {
      start() {}
      stop() {}
      abort() {}
      addEventListener() {}
    }
    vi.stubGlobal("webkitSpeechRecognition", FakeRecognition);
    vi.stubGlobal("isSecureContext", true);
    mockFetch({ ...common, "GET /api/auth/me": { user: admin } });
    render(<App />);
    const mic = await screen.findByRole("button", { name: "Voice input" });
    fireEvent.click(mic);
    const consent = await screen.findByRole("dialog", { name: "Voice input" });
    expect(within(consent).getByText(/Google|Microsoft/)).toBeInTheDocument();
  });
});
