import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { mockFetch } from "./fetchMock";

const status = {
  app: "Infinite AI Command Center",
  version: "0.2.0",
  environment: "test",
  time: "2026-10-18T12:00:00Z",
  database: { ok: true },
  workers: [],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  n8n: null,
  counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
};

const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin", can_access_sensitive: false };
const now = new Date().toISOString();
const notEnabled = { configured: true, entitled: true, enabled: null, can_access: false, notice: "Credit repair is regulated (CROA)." };

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("record pages", () => {
  it("shows Credit Repair in the menu only to admins and permitted users", async () => {
    mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: { ...admin, role: "operator" } }, "GET /api/events": { events: [] }, "GET /api/sites": { sites: [] } });
    const { unmount } = render(<App />);
    expect(await screen.findByRole("link", { name: "Websites" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Approvals" })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Credit Repair" })).toBeNull();
    unmount();

    mockFetch({ "GET /api/system/status": status, "GET /api/auth/me": { user: { ...admin, role: "operator", can_access_sensitive: true } }, "GET /api/events": { events: [] }, "GET /api/sites": { sites: [] } });
    render(<App />);
    expect(await screen.findByRole("link", { name: "Credit Repair" })).toBeInTheDocument();
  });

  it("enables Credit Repair only after the compliance notice is acknowledged", async () => {
    window.history.pushState({}, "", "/credit-repair");
    const calls = mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/credit-repair/status": notEnabled,
      "POST /api/credit-repair/enable": { json: { enabled: { acknowledged_by: admin.email, acknowledged_at: now } } },
    });
    render(<App />);
    expect(await screen.findByText("Credit repair is regulated (CROA).")).toBeInTheDocument();
    const enable = screen.getByRole("button", { name: "Enable the module" });
    expect(enable).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(enable);
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/credit-repair/enable")).toBe(true));
    expect(calls.find((c) => c.path === "/api/credit-repair/enable")!.body).toEqual({ acknowledge: true });
    // Without the sensitive-data permission no client data is requested.
    expect(calls.some((c) => c.path.startsWith("/api/credit-repair/clients"))).toBe(false);
  });

  it("adds a lead and offers sending only once a proposal is drafted", async () => {
    window.history.pushState({}, "", "/leads");
    const lead = { id: 7, created_at: now, source: "n8n", name: "Ann Lee", company: "Lee Dental", email: "ann@lee.example", phone: "", website: "", service_interest: "SEO", message: "", score: 82, score_reasons: ["budget mentioned"], status: "qualified", proposal_artifact_id: null };
    const calls = mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/leads": { leads: [lead, { ...lead, id: 8, company: "Drafted Co", proposal_artifact_id: 3 }] },
      "POST /api/leads": { json: { lead: { ...lead, id: 9 }, task_ids: [1] } },
    });
    render(<App />);
    expect(await screen.findByText("Lee Dental")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Send (needs approval)" })).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("name"), { target: { value: "Bo Diaz" } });
    fireEvent.change(screen.getByLabelText("email"), { target: { value: "bo@diaz.example" } });
    fireEvent.click(screen.getByRole("button", { name: "Add lead" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/leads")).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/leads")!.body).toMatchObject({ name: "Bo Diaz", email: "bo@diaz.example" });
  });

  it("labels mock outbox entries as recorded, not sent (C2-01)", async () => {
    window.history.pushState({}, "", "/outbox");
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/outbox": {
        entries: [{ id: 1, created_at: now, task_id: 4, agent: "communications", integration: "email", mode: "mock", action: "send", recipient: "ann@lee.example", subject: "Your proposal", body: "Hi", payload: {}, approved_by: "admin@test.local", result: "recorded, not sent" }],
      },
    });
    render(<App />);
    expect(await screen.findByText("recorded, not sent")).toBeInTheDocument();
    expect(screen.getByText("Recorded (mock), not sent")).toBeInTheDocument();
    expect(screen.queryByText("MOCK")).toBeNull();
  });
});

describe("agents page", () => {
  it("shows the AI Supervisor apart from the seven numbered agents", async () => {
    window.history.pushState({}, "", "/agents");
    const agent = (name: string, title: string, role = "agent") => ({
      name, role, title, description: "d", enabled: true, entitled: true, sensitive: name === "credit_repair", integrations: [],
      capabilities: [{ name: "x", description: "", risk: "change", requires_approval: true, params_schema: { properties: {} }, examples: [] }],
      stats_24h: {}, last_run_at: null,
    });
    const titles = ["WordPress Security & Repair", "SEO & Content", "Social Media & Marketing (Postonomous)", "VPS & Server Management (CWP/SSH)",
      "Email & Customer Communications", "Website & Infrastructure Monitoring", "Credit Repair & Dispute Management"];
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/agents": { agents: [agent("supervisor", "AI Supervisor", "supervisor"), ...titles.map((t, i) => agent(`a${i}`, t))] },
    });
    render(<App />);
    expect(await screen.findByRole("heading", { name: "Coordinator" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "The seven agents" })).toBeInTheDocument();
    for (const t of titles) expect(screen.getByText(t)).toBeInTheDocument();
    expect(screen.getAllByText("change · approval").length).toBe(8);
    // The supervisor cannot be switched off; each of the seven can.
    expect(screen.getAllByRole("button", { name: "Disable" })).toHaveLength(7);
  });
});
