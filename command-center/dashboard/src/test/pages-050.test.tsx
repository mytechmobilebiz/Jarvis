// 0.5.0 page contracts and audit fixes: approval cards that name who asked and the exact target (C3-02, C3-03, C3-08),
// typed confirmations for host-key replacement and production demotion (C3-05, C3-06), "sent" only when delivered (C2-01,
// C2-02, C2-11), uptime with its coverage and open issues from both checks (C2-07, C2-09, C2-10), connection states with
// their age and cadences from the real schedules (C2-04, C2-15), sample-data marks (C2-12) and stated windows (C2-13, C2-14).
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { ApiError, type IntegrationState, type Schedule, type Uptime } from "../api";
import { requestedBy } from "../pages/Approvals";
import { itemStatus, letterStatus } from "../pages/CreditRepair";
import { emailTileLabel } from "../pages/Email";
import {
  ageText,
  cadence,
  confirmReason,
  coverage,
  cronHint,
  deliveredConnection,
  deliveryCounts,
  deliverySummary,
  emailDelivery,
  leadStatus,
  mockLabel,
  probedConnection,
  ticketStatus,
} from "../pages/shared";
import { issueSources, openIssues, type SiteView } from "../pages/Websites";
import { mockFetch } from "./fetchMock";

const now = new Date().toISOString();
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const status = {
  app: "Infinite AI Command Center",
  version: "0.5.0",
  environment: "test",
  time: now,
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: now, alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  workflow_engine: "n8n",
  n8n: null,
  counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
};
const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin", can_access_sensitive: false };

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

const base = (over: Record<string, unknown> = {}) => ({ "GET /api/system/status": status, "GET /api/auth/me": { user: admin }, ...over });

// ---------------------------------------------------------------------------------------------------------------------

describe("delivery labels (C2-01, C2-02, C2-11)", () => {
  it("says sent or replied only for a live delivery", () => {
    expect(emailDelivery("live", "Proposal sent")).toEqual(["sent", "Proposal sent"]);
    expect(emailDelivery("mock", "Proposal sent")).toEqual(["mock", "Recorded (mock), not sent"]);
    expect(emailDelivery(null, "Replied")).toEqual(["unknown", "Approved · delivery not recorded"]);
    expect(emailDelivery(undefined, "Replied")[1]).toBe("Approved · delivery not recorded");
    const lead = { status: "proposal_sent", delivery_mode: "mock" } as Parameters<typeof leadStatus>[0];
    expect(leadStatus(lead)[1]).toBe("Recorded (mock), not sent");
    expect(leadStatus({ ...lead, status: "qualified" })).toEqual(["up", "Qualified"]);
    const ticket = { status: "replied", delivery_mode: "live" } as Parameters<typeof ticketStatus>[0];
    expect(ticketStatus(ticket)).toEqual(["sent", "Replied"]);
    expect(ticketStatus({ ...ticket, status: "drafted" })).toEqual(["draft", "Reply drafted"]);
  });

  it("names what a mock did not do, per channel", () => {
    expect(mockLabel("email")).toBe("Recorded (mock), not sent");
    expect(mockLabel("postal_mail")).toBe("Recorded (mock), not mailed");
    expect(mockLabel("postonomous")).toBe("Recorded (mock), not posted");
    expect(mockLabel("ads")).toBe("Recorded (mock), not applied");
  });

  it("counts and summarises deliveries without zeros", () => {
    const rows = [
      { status: "proposal_sent", delivery_mode: "live" as const },
      { status: "proposal_sent", delivery_mode: "mock" as const },
      { status: "proposal_sent", delivery_mode: "mock" as const },
      { status: "proposal_sent" },
      { status: "new" },
    ];
    const c = deliveryCounts(rows, "proposal_sent");
    expect(c).toEqual({ live: 1, mock: 2, unknown: 1 });
    expect(deliverySummary(c, "sent", "none")).toBe("1 sent · 2 recorded (mock), not sent · 1 approved, delivery not recorded");
    expect(deliverySummary({ live: 0, mock: 0, unknown: 0 }, "sent", "none")).toBe("none");
  });

  it("labels the email tile by what was really delivered", () => {
    expect(emailTileLabel(0, 3, "mock")).toBe("Emails recorded (mock)");
    expect(emailTileLabel(2, 0, "live")).toBe("Emails sent");
    expect(emailTileLabel(2, 1, "live")).toBe("Emails sent or recorded");
    expect(emailTileLabel(0, 0, "mock")).toBe("Emails recorded (mock)");
    expect(emailTileLabel(0, 0, undefined)).toBe("Emails");
  });

  it("calls a mock-mailed letter recorded, not mailed, and its items not disputed with the bureau", () => {
    expect(letterStatus({ status: "mailed_mock", mail_mode: "mock" })).toEqual(["mock", "Recorded (mock), not mailed"]);
    expect(letterStatus({ status: "mailed_mock" })[1]).toBe("Recorded (mock), not mailed");
    expect(letterStatus({ status: "mailed", mail_mode: "live" })).toEqual(["sent", "Mailed"]);
    expect(letterStatus({ status: "draft", mail_mode: null })).toEqual(["info", "draft"]);
    expect(itemStatus({ status: "disputed", mail_mode: "mock" })[1]).toBe("Dispute recorded (mock), not mailed");
    expect(itemStatus({ status: "in_letter" })).toEqual(["info", "in letter"]);
  });

  it("Leads, Client support and Email show a mock send as recorded, not sent, and a live one as sent", async () => {
    const lead = {
      id: 7, created_at: now, source: "n8n", name: "Ann Lee", company: "Lee Dental", email: "ann@lee.example", phone: "", website: "", service_interest: "",
      message: "", score: 82, score_reasons: [], status: "proposal_sent", proposal_artifact_id: 3, delivery_mode: "mock", delivery_outbox_id: 9,
    };
    const live = { ...lead, id: 8, company: "Live Co", delivery_mode: "live" };
    window.history.pushState({}, "", "/leads");
    mockFetch(base({ "GET /api/leads": { leads: [lead, live] } }));
    const first = render(<App />);
    const row = (await screen.findByText("Lee Dental")).closest("tr")!;
    expect(within(row).getByText("Recorded (mock), not sent")).toBeInTheDocument();
    expect(within(row).getByRole("link", { name: "proposal" })).toHaveAttribute("href", "/drafts?id=3");
    expect(within(screen.getByText("Live Co").closest("tr")!).getByText("Proposal sent")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "New leads" })).toHaveTextContent("1 sent · 1 recorded (mock), not sent");
    first.unmount();

    window.history.pushState({}, "", "/support");
    const ticket = {
      id: 4, created_at: now, source: "dashboard", customer_name: "Bo", customer_email: "bo@example.test", subject: "Site down", body: "", category: "outage",
      priority: "urgent", sentiment: "neutral", status: "replied", reply_artifact_id: 11, delivery_mode: "mock", delivery_outbox_id: 2,
    };
    mockFetch(base({ "GET /api/support/tickets": { tickets: [ticket] } }));
    const second = render(<App />);
    const trow = (await screen.findByText("Site down")).closest("tr")!;
    expect(within(trow).getByText("Recorded (mock), not sent")).toBeInTheDocument();
    expect(within(trow).queryByText("replied")).toBeNull();
    expect(screen.getByRole("region", { name: "Open messages" })).toHaveTextContent("1 reply recorded (mock), not sent");
    second.unmount();

    window.history.pushState({}, "", "/email");
    const entry = {
      id: 2, created_at: now, task_id: 5, agent: "communications", integration: "email", mode: "mock", action: "send", recipient: "bo@example.test",
      subject: "Re: Site down", body: "Hi", payload: {}, approved_by: admin.email, result: "recorded, not sent",
    };
    mockFetch(
      base({
        "GET /api/support/tickets": { tickets: [ticket] },
        "GET /api/leads": { leads: [lead] },
        "GET /api/outbox": { entries: [entry] },
        "GET /api/integrations": { integrations: [{ name: "email", title: "Email", mode: "mock", description: "", live_requirements: "", status: null }] },
        "GET /api/tasks": { tasks: [], total: 0 },
        "GET /api/artifacts": { artifacts: [] },
        "GET /api/agents": { agents: [] },
      }),
    );
    render(<App />);
    expect(await screen.findByRole("region", { name: "Emails recorded (mock)" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Emails sent" })).toBeNull();
    const clients = screen.getByRole("region", { name: "Client messages" });
    await waitFor(() => expect(clients).toHaveTextContent("No open messages · 1 recorded (mock), not sent"));
    expect(screen.getByRole("region", { name: "New leads" })).toHaveTextContent("1 recorded (mock), not sent");
    expect(screen.getAllByText("Recorded (mock), not sent").length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------------------------------------------------

describe("approval cards (C3-02, C3-03, C3-08)", () => {
  const me = { email: admin.email };
  const req = (over: Record<string, unknown>) => ({
    source: "dashboard", by: admin.email, request_id: null, request_source: null, requested_by: null, message: null, schedule: null, retry_of: null, ...over,
  });

  function text(node: ReturnType<typeof requestedBy>) {
    const { container, unmount } = render(<>{node}</>, { wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter> });
    const t = container.textContent;
    unmount();
    return t;
  }

  it("says who or what asked: you, another person, the AI Supervisor and where its request came from, a schedule, a retry", () => {
    expect(text(requestedBy(req({}), me))).toBe("you (admin@test.local)");
    expect(text(requestedBy(req({ by: "op@test.local" }), me))).toBe("op@test.local");
    expect(
      text(requestedBy(req({ source: "supervisor", by: "supervisor (request 12)", request_id: 12, request_source: "n8n", requested_by: "service:n8n", message: "Fix the site" }), me)),
    ).toBe("AI Supervisor from request #12 (n8n, with the service token), asked by service:n8n“Fix the site”");
    expect(text(requestedBy(req({ source: "schedule", by: "schedule:wordpress-uptime-sweep", schedule: "wordpress-uptime-sweep" }), me))).toBe(
      "Schedule wordpress-uptime-sweep",
    );
    expect(text(requestedBy(req({ source: "retry", by: admin.email, retry_of: 41 }), me))).toBe("Retry of task #41, by you (admin@test.local)");
    expect(requestedBy(undefined, me)).toBeNull();
  });

  const draft = { artifact_id: 5, kind: "proposal", title: "Proposal for Lee Dental", status: "draft", sha256: "abc", excerpt: "Dear Ann, here is our proposal." };
  const email = {
    id: 21, created_at: now, task_id: 30, status: "pending", agent: "communications", capability: "send_proposal", risk: "external",
    summary: 'communications.send_proposal(lead_id=7) on email to ann@lee.example, subject "Website Design Proposal for Lee Dental", draft #5',
    details: {
      params: { lead_id: 7 },
      capability_description: "Email the drafted proposal to the lead (external: approval required).",
      target: 'email to ann@lee.example, subject "Website Design Proposal for Lee Dental", draft #5',
      environment: "mock: recorded in the Outbox, not sent",
      production: false,
      delivery: "mock",
      draft,
      binding: { lead_id: 7, recipient: "ann@lee.example", subject: "Website Design Proposal for Lee Dental", artifact_id: 5, draft_sha256: "abc" },
      requested: req({ source: "supervisor", by: "supervisor (request 12)", request_id: 12, request_source: "n8n", requested_by: "service:n8n", message: "Send Lee Dental the proposal" }),
    },
    decided_by: "", decided_at: null, decision_note: "",
  };
  const publish = {
    ...email,
    id: 22,
    agent: "seo_content",
    capability: "publish_draft",
    risk: "external",
    summary: "seo_content.publish_draft(...) on site Acme (https://acme.example) (PRODUCTION)",
    details: {
      params: { artifact_id: 6, destination: "wordpress", site_id: 3 },
      target: 'site Acme (https://acme.example): a draft post "Spring tips" from blog draft #6',
      environment: "production",
      production: true,
      confirm: "Acme",
      delivery: "mock",
      draft: { ...draft, artifact_id: 6, kind: "blog_draft", title: "Spring tips", status: "approved", excerpt: "Spring is here." },
      binding: { site_id: 3, name: "Acme", url: "https://acme.example", artifact_id: 6, draft_sha256: "def" },
      requested: req({ source: "schedule", by: "schedule:weekly-post", schedule: "weekly-post" }),
    },
  };
  const old = {
    ...email,
    id: 23,
    agent: "social_marketing",
    capability: "change_budget",
    risk: "financial",
    summary: "social_marketing.change_budget(campaign_id='c1', daily_budget=40)",
    details: { params: { campaign_id: "c1", daily_budget: 40 } },
  };

  it("shows the recipient, subject and draft of an email, the site of a publish, and who asked", async () => {
    window.history.pushState({}, "", "/approvals");
    mockFetch(base({ "GET /api/approvals": { approvals: [email, publish, old] } }));
    render(<App />);
    const card = (await screen.findByText("#21")).closest("section")!;
    const facts = within(card);
    expect(facts.getByText("Requested by").nextSibling).toHaveTextContent("AI Supervisor from request #12 (n8n, with the service token)");
    expect(facts.getByRole("link", { name: "request #12" })).toHaveAttribute("href", "/supervisor?id=12");
    expect(facts.getByText("To").nextSibling).toHaveTextContent("ann@lee.example");
    expect(facts.getByText("Subject").nextSibling).toHaveTextContent("Website Design Proposal for Lee Dental");
    expect(facts.getByRole("link", { name: "#5 Proposal for Lee Dental" })).toHaveAttribute("href", "/drafts?id=5");
    expect(facts.getByText("Dear Ann, here is our proposal.")).toBeInTheDocument();
    expect(facts.getByText("Recorded (mock), not sent")).toBeInTheDocument();

    const pub = within(screen.getByText("#22").closest("section")!);
    expect(pub.getByText("Site").nextSibling).toHaveTextContent("Acme https://acme.example");
    expect(pub.getByText("Environment").nextSibling).toHaveTextContent("production");
    expect(pub.getByText("PRODUCTION")).toBeInTheDocument();
    expect(pub.getByText("Requested by").nextSibling).toHaveTextContent("Schedule weekly-post");
    expect(pub.getByText("Recorded (mock), not published")).toBeInTheDocument();
    expect(pub.getByLabelText("Type Acme to confirm this production change")).toBeInTheDocument();

    const legacy = within(screen.getByText("#23").closest("section")!);
    expect(legacy.getByText("Requested by").nextSibling).toHaveTextContent("Not recorded (requested before version 0.5.0)");
    expect(legacy.getByText(/No target recorded for this action/)).toBeInTheDocument();
  });

  it("shows the platform and account of a social post, and the mock delivery once", async () => {
    window.history.pushState({}, "", "/approvals");
    const social = {
      ...email,
      id: 25,
      agent: "social_marketing",
      capability: "publish_draft",
      details: {
        params: { artifact_id: 8, destination: "facebook" },
        target: 'facebook through Postonomous (no Postonomous account connected): social draft #8 "Spring offer"',
        environment: "mock: recorded in the Outbox, not published",
        production: false,
        delivery: "mock",
        draft: { ...draft, artifact_id: 8, kind: "social_drafts", title: "Spring offer", status: "approved", excerpt: "Spring offer: 10% off." },
        binding: { platform: "facebook", account: "no Postonomous account connected", artifact_id: 8, draft_sha256: "x" },
        requested: req({ by: "op@test.local" }),
      },
    };
    mockFetch(base({ "GET /api/approvals": { approvals: [social] } }));
    render(<App />);
    const card = within((await screen.findByText("#25")).closest("section")!);
    expect(card.getByText("Platform").nextSibling).toHaveTextContent("facebook");
    expect(card.getByText("Account").nextSibling).toHaveTextContent("no Postonomous account connected");
    expect(card.queryByText("Environment")).toBeNull();
    expect(card.getByText("Recorded (mock), not posted")).toBeInTheDocument();
    expect(card.getByRole("link", { name: "#8 Spring offer" })).toHaveAttribute("href", "/drafts?id=8");
  });

  it("shows the exact version a repair will apply", async () => {
    window.history.pushState({}, "", "/approvals");
    const repair = {
      ...email,
      id: 24,
      agent: "wordpress_security",
      capability: "repair_update_plugin",
      risk: "change",
      details: {
        params: { site_id: 3, slug: "akismet" },
        target: "site Acme (https://acme.example) on server cwp-1 (ccagent@203.0.113.5:22), cc-gate site key acme (https://acme.example)",
        environment: "test",
        production: false,
        confirm: "Acme",
        expected: { plugin: "akismet", version: "5.0", update_version: "5.1", checked_at: now, summary: "update akismet 5.0 → 5.1" },
        requested: req({ by: "op@test.local" }),
      },
    };
    mockFetch(base({ "GET /api/approvals": { approvals: [repair] } }));
    render(<App />);
    const card = within((await screen.findByText("#24")).closest("section")!);
    expect(card.getByText("Target").nextSibling).toHaveTextContent("cc-gate site key acme");
    expect(card.getByText("Will apply").nextSibling).toHaveTextContent("update akismet 5.0 → 5.1");
    expect(card.getByText("Requested by").nextSibling).toHaveTextContent("op@test.local");
  });

  it("opens the linked draft on Drafts & reports", async () => {
    window.history.pushState({}, "", "/drafts?id=5");
    const artifact = { id: 5, created_at: now, task_id: 1, agent: "communications", kind: "proposal", title: "Proposal for Lee Dental", content: "Dear Ann", content_type: "text/markdown", data: {}, status: "draft" };
    mockFetch(base({ "GET /api/artifacts": { artifacts: [] }, "GET /api/artifacts/5": { artifact } }));
    render(<App />);
    const dialog = await screen.findByRole("dialog", { name: "Proposal for Lee Dental" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.location.search).toBe("");
  });
});

// ---------------------------------------------------------------------------------------------------------------------

const server = (over: Record<string, unknown> = {}) => ({
  id: 1, name: "cwp-1", host: "203.0.113.5", port: 22, username: "ccagent", environment: "production", host_key_pinned: true,
  host_key_fingerprint: "SHA256:old", host_key_trusted_by: admin.email, host_key_trusted_at: now, enabled: true, notes: "", last_hello: {},
  last_seen_at: now, created_at: now, sites: [], ...over,
});

async function openServer(name = "cwp-1") {
  fireEvent.click(await screen.findByRole("button", { name }));
  return within(await screen.findByRole("dialog", { name: new RegExp(name) }));
}

describe("host key replacement (C3-05)", () => {
  it("shows 'Host key CHANGED' and replaces the pin only after the server's name is typed", async () => {
    window.history.pushState({}, "", "/servers");
    const calls = mockFetch(
      base({
        "GET /api/servers": { servers: [server()] },
        "GET /api/servers/1/commands": { commands: [], total: 0 },
        "GET /api/ssh/key": { key: null },
        "POST /api/servers/1/host-key/scan": { fingerprint: "SHA256:new", host_key: "ssh-ed25519 AAAAnew", matches_pinned: false, pinned_fingerprint: "SHA256:old" },
        "POST /api/servers/1/host-key/trust": { server: server({ host_key_fingerprint: "SHA256:new" }) },
      }),
    );
    render(<App />);
    const dialog = await openServer();
    fireEvent.click(dialog.getByRole("button", { name: "Scan host key" }));
    const alert = await screen.findByText("Host key CHANGED");
    expect(alert.closest("[role=alert]")).toHaveTextContent("now presents SHA256:new, not the pinned SHA256:old");
    // No one-click trust for a changed key.
    expect(screen.queryByRole("button", { name: "The fingerprints match: trust this key" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Replace the pinned key…" }));
    const confirm = within(await screen.findByRole("dialog", { name: "Replace the host key of cwp-1" }));
    const go = confirm.getByRole("button", { name: "Replace the pinned key" });
    expect(go).toBeDisabled();
    fireEvent.change(confirm.getByLabelText(/Type cwp-1 to confirm/), { target: { value: "cwp" } });
    expect(go).toBeDisabled();
    fireEvent.change(confirm.getByLabelText(/Type cwp-1 to confirm/), { target: { value: "cwp-1" } });
    fireEvent.click(go);
    await waitFor(() => expect(calls.some((c) => c.path === "/api/servers/1/host-key/trust")).toBe(true));
    expect(calls.find((c) => c.path === "/api/servers/1/host-key/trust")!.body).toEqual({ fingerprint: "SHA256:new", confirm: "cwp-1" });
  });

  it("says when the scanned key is the pinned one", async () => {
    window.history.pushState({}, "", "/servers");
    mockFetch(
      base({
        "GET /api/servers": { servers: [server()] },
        "GET /api/servers/1/commands": { commands: [], total: 0 },
        "GET /api/ssh/key": { key: null },
        "POST /api/servers/1/host-key/scan": { fingerprint: "SHA256:old", host_key: "ssh-ed25519 AAAAold", matches_pinned: true, pinned_fingerprint: "SHA256:old" },
      }),
    );
    render(<App />);
    const dialog = await openServer();
    fireEvent.click(dialog.getByRole("button", { name: "Scan host key" }));
    expect(await screen.findByText(/Nothing to change/)).toBeInTheDocument();
    expect(screen.queryByText("Host key CHANGED")).toBeNull();
  });

  it("asks for the name when the API refuses a pasted key with 'confirm:', then sends it", async () => {
    window.history.pushState({}, "", "/servers");
    const reason = "confirm: the host key of cwp-1 CHANGED (trusted: SHA256:old, now: SHA256:pasted). Only replace it if the server was reinstalled; type the server's name, cwp-1, to replace it";
    const calls = mockFetch(
      base({
        "GET /api/servers": { servers: [server({ host_key_pinned: false, host_key_fingerprint: "" })] },
        "GET /api/servers/1/commands": { commands: [], total: 0 },
        "GET /api/ssh/key": { key: null },
        "POST /api/servers/1/host-key/trust": (body: unknown) =>
          (body as { confirm?: string }).confirm === "cwp-1" ? { json: { server: server() } } : { status: 400, json: { detail: reason } },
      }),
    );
    render(<App />);
    const dialog = await openServer();
    fireEvent.change(dialog.getByLabelText("Public host key"), { target: { value: "ssh-ed25519 AAAApasted" } });
    fireEvent.click(dialog.getByRole("button", { name: "Pin pasted key" }));
    const confirm = within(await screen.findByRole("dialog", { name: "Replace the host key of cwp-1" }));
    expect(confirm.getByRole("alert")).toHaveTextContent("CHANGED (trusted: SHA256:old, now: SHA256:pasted)");
    fireEvent.change(confirm.getByLabelText(/Type cwp-1 to confirm/), { target: { value: "cwp-1" } });
    fireEvent.click(confirm.getByRole("button", { name: "Replace the pinned key" }));
    await waitFor(() => expect(calls.filter((c) => c.path === "/api/servers/1/host-key/trust")).toHaveLength(2));
    expect(calls.filter((c) => c.path === "/api/servers/1/host-key/trust")[1].body).toEqual({ host_key: "ssh-ed25519 AAAApasted", confirm: "cwp-1" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Replace the host key of cwp-1" })).toBeNull());
  });

  it("recognises the API's typed-confirmation refusals only", () => {
    expect(confirmReason(new ApiError(400, "confirm: type the server's name"))).toBe("type the server's name");
    expect(confirmReason(new ApiError(400, "no such server"))).toBeNull();
    expect(confirmReason(new ApiError(403, "confirm: x"))).toBeNull();
    expect(confirmReason(new Error("confirm: x"))).toBeNull();
  });
});

describe("taking a server or site out of production (C3-06)", () => {
  it("Servers: production to staging needs the typed name; test to staging does not", async () => {
    window.history.pushState({}, "", "/servers");
    const calls = mockFetch(
      base({
        "GET /api/servers": { servers: [server(), server({ id: 2, name: "lab", environment: "test" })] },
        "GET /api/servers/1/commands": { commands: [], total: 0 },
        "GET /api/servers/2/commands": { commands: [], total: 0 },
        "GET /api/ssh/key": { key: null },
        "PATCH /api/servers/1": { server: server({ environment: "staging" }) },
        "PATCH /api/servers/2": { server: server({ id: 2, name: "lab", environment: "staging" }) },
      }),
    );
    const { unmount } = render(<App />);
    let dialog = await openServer();
    fireEvent.change(dialog.getByLabelText("Server environment"), { target: { value: "staging" } });
    const confirm = within(await screen.findByRole("dialog", { name: "Take cwp-1 out of production" }));
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    expect(confirm.getByRole("alert")).toHaveTextContent("no longer need an admin's typed confirmation");
    fireEvent.change(confirm.getByLabelText(/Type cwp-1 to confirm/), { target: { value: "cwp-1" } });
    fireEvent.click(confirm.getByRole("button", { name: "Change to staging" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.path === "/api/servers/1")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH" && c.path === "/api/servers/1")!.body).toEqual({ environment: "staging", confirm: "cwp-1" });
    unmount();

    render(<App />);
    dialog = await openServer("lab");
    fireEvent.change(dialog.getByLabelText("Server environment"), { target: { value: "staging" } });
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.path === "/api/servers/2")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH" && c.path === "/api/servers/2")!.body).toEqual({ environment: "staging" });
    expect(screen.queryByRole("dialog", { name: /out of production/ })).toBeNull();
  });

  const site = (over: Record<string, unknown> = {}) => ({
    id: 3, name: "Acme", url: "https://acme.example", client: "", notes: "", enabled: true, last_status: "up", last_checked_at: now, last_check: null,
    environment: "production", server_id: null, server_site_key: "", uptime: null, last_diagnosis: {}, last_diagnosis_at: null, ...over,
  });

  it("Websites: a production site to test, and unlinking a test site from a production server, need the typed name", async () => {
    window.history.pushState({}, "", "/websites");
    const calls = mockFetch(
      base({
        "GET /api/sites": { sites: [site(), site({ id: 4, name: "Beta", url: "https://beta.example", environment: "test", server_id: 1, server_site_key: "beta" })] },
        "GET /api/sites/3": { site: site() },
        "GET /api/sites/3/checks": { checks: [] },
        "GET /api/sites/4": { site: site({ id: 4, name: "Beta", url: "https://beta.example", environment: "test", server_id: 1, server_site_key: "beta" }) },
        "GET /api/sites/4/checks": { checks: [] },
        "GET /api/servers": { servers: [server()] },
        "GET /api/schedules": { schedules: [] },
        "PATCH /api/sites/3": { site: site({ environment: "test" }) },
        "PATCH /api/sites/4": { site: site({ id: 4 }) },
      }),
    );
    const { unmount } = render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Acme" }));
    const detail = within(await screen.findByRole("dialog", { name: "Acme" }));
    fireEvent.change(detail.getByLabelText("Site environment setting"), { target: { value: "test" } });
    const confirm = within(await screen.findByRole("dialog", { name: "Take Acme out of production" }));
    fireEvent.change(confirm.getByLabelText(/Type Acme to confirm/), { target: { value: "Acme" } });
    fireEvent.click(confirm.getByRole("button", { name: "Confirm the change" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.path === "/api/sites/3")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH" && c.path === "/api/sites/3")!.body).toEqual({ environment: "test", confirm: "Acme" });
    unmount();

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Beta" }));
    const beta = within(await screen.findByRole("dialog", { name: "Beta" }));
    // Its server is production, so its repairs count as production even though the site itself is test.
    expect(await beta.findByText(/the site's server is production/)).toBeInTheDocument();
    await waitFor(() => expect(beta.getByLabelText("Server")).toHaveValue("1"));
    fireEvent.change(beta.getByLabelText("Server"), { target: { value: "" } });
    fireEvent.click(beta.getByRole("button", { name: "Save link" }));
    const unlink = within(await screen.findByRole("dialog", { name: "Take Beta out of production" }));
    expect(unlink.getByRole("alert")).toHaveTextContent("not linked to a server");
    fireEvent.change(unlink.getByLabelText(/Type Beta to confirm/), { target: { value: "Beta" } });
    fireEvent.click(unlink.getByRole("button", { name: "Confirm the change" }));
    await waitFor(() => expect(calls.some((c) => c.method === "PATCH" && c.path === "/api/sites/4")).toBe(true));
    expect(calls.find((c) => c.method === "PATCH" && c.path === "/api/sites/4")!.body).toEqual({ unlink_server: true, confirm: "Beta" });
  });
});

// ---------------------------------------------------------------------------------------------------------------------

describe("uptime coverage and open issues (C2-07, C2-09, C2-10)", () => {
  const nowMs = Date.parse("2026-10-19T12:00:00Z");
  const u = (over: Partial<Uptime>): Uptime => ({ "24h": 100, "7d": 100, "30d": 100, checks_30d: 96, avg_response_ms_24h: 100, ...over });

  it("shows a percentage only with enough checks, with how many and since when", () => {
    expect(coverage(u({ "7d": 50, n_7d: 2, first_check_at: "2026-10-19T11:30:00Z" }), "7d", nowMs).pct).toBeNull();
    expect(coverage(u({ "7d": 50, n_7d: 2, first_check_at: "2026-10-19T11:30:00Z" }), "7d", nowMs).text).toMatch(/^Not enough data yet \(2 checks since .+\)$/);
    const ok = coverage(u({ "7d": 99.5, n_7d: 96, first_check_at: "2026-10-01T00:00:00Z" }), "7d", nowMs);
    expect(ok).toMatchObject({ pct: 99.5, checks: 96, since: null, text: "96 checks" });
    expect(coverage(u({ "24h": null, n_24h: 0 }), "24h", nowMs).text).toBe("No checks in this window");
    expect(coverage(null, "24h", nowMs).text).toBe("No checks in this window");
    // 30 days: n_30d, or checks_30d from older APIs.
    expect(coverage(u({ checks_30d: 3 }), "30d", nowMs).pct).toBeNull();
    // Older APIs give no count for 24 h and 7 days: the figure stays, and says the count is unknown.
    expect(coverage(u({ "24h": 98 }), "24h", nowMs)).toMatchObject({ pct: 98, checks: null, text: "Number of checks not reported" });
  });

  it("takes open issues from both checks when the API gives them, and names the checks", () => {
    const s = {
      last_check: { findings: [{ code: "x", severity: "warning", title: "Old" }] },
      open_findings: [
        { code: "cloaked_spam", severity: "critical", title: "Cloaked spam", source: "deep", checked_at: "2026-10-19T03:17:00Z" },
        { code: "slow", severity: "info", title: "Slow", source: "quick", checked_at: "2026-10-19T11:45:00Z" },
      ],
      open_findings_sources: { quick: "2026-10-19T11:45:00Z", deep: "2026-10-19T03:17:00Z" },
    } as unknown as SiteView;
    expect(openIssues(s).map((f) => f.title)).toEqual(["Cloaked spam"]);
    expect(issueSources(s, nowMs)).toMatch(/^the security scan of .+ and the uptime check of .+$/);
    const older = { last_check: { findings: [{ code: "x", severity: "warning", title: "Old" }, { code: "i", severity: "info", title: "Note" }] } } as unknown as SiteView;
    expect(openIssues(older).map((f) => f.title)).toEqual(["Old"]);
    expect(issueSources(older)).toBeNull();
  });

  it("Websites: leaves paused sites out, says when data is thin, keeps nightly findings and states the real cadence", async () => {
    window.history.pushState({}, "", "/websites");
    const site = (id: number, over: Record<string, unknown>) => ({
      id, name: `Site ${id}`, url: `https://s${id}.example`, client: "", notes: "", enabled: true, last_status: "up", last_checked_at: now, last_check: null,
      environment: "production", server_id: null, server_site_key: "", last_diagnosis: {}, last_diagnosis_at: null,
      uptime: { "24h": 100, "7d": 99, "30d": 99, n_24h: 96, n_7d: 600, n_30d: 900, checks_30d: 900, avg_response_ms_24h: 120, first_check_at: ago(40 * 86_400_000) },
      ...over,
    });
    const deepAt = ago(9 * 3_600_000);
    const sites = [
      site(1, {
        open_findings: [{ code: "cloaked_spam", severity: "critical", title: "Cloaked spam for search crawlers", source: "deep", checked_at: deepAt }],
        open_findings_sources: { quick: ago(600_000), deep: deepAt },
      }),
      site(2, { uptime: { "24h": 0, "7d": 0, "30d": 0, n_24h: 2, n_7d: 2, n_30d: 2, checks_30d: 2, avg_response_ms_24h: null, first_check_at: ago(1_800_000) }, open_findings: [], open_findings_sources: { quick: ago(60_000), deep: null } }),
      site(3, { enabled: false, last_status: "down", uptime: { "24h": 10, "7d": 10, "30d": 10, n_24h: 96, n_7d: 600, n_30d: 900, checks_30d: 900, avg_response_ms_24h: null, first_check_at: null } }),
    ];
    const schedules: Partial<Schedule>[] = [
      { id: 1, name: "wordpress-uptime-sweep", agent: "monitoring", capability: "check_all_sites", cron: "*/30 * * * *", enabled: true },
      { id: 2, name: "wordpress-security-scan", agent: "wordpress_security", capability: "security_scan_all", cron: "17 3 * * *", enabled: false },
    ];
    mockFetch(base({ "GET /api/sites": { sites }, "GET /api/schedules": { schedules } }));
    render(<App />);
    expect(await screen.findByText(/Uptime and redirect checks run every 30 minutes and the security scan is paused, all read-only\./)).toBeInTheDocument();
    const avg = screen.getByRole("region", { name: "Average uptime, 7 days" });
    await waitFor(() => expect(avg).toHaveTextContent("99.00%"));
    expect(avg).toHaveTextContent("Average of 1 monitored site · 1 with too few checks left out · 1 paused, not included");
    const issues = screen.getByRole("region", { name: "Open issues" });
    expect(issues).toHaveTextContent("1");
    expect(issues).toHaveTextContent("1 critical · on 1 site · latest uptime checks and security scans");
    const status = screen.getByRole("region", { name: "Up · down · degraded" });
    expect(status).toHaveTextContent("1 paused, not counted");
    expect(within(screen.getByText("Site 2").closest("tr")!).getByText("Not enough data yet")).toBeInTheDocument();
    expect(within(screen.getByText("Site 3").closest("tr")!).getByText("Paused")).toBeInTheDocument();
  });

  it("the site dialog lists each open issue with the check that found it, and each window's coverage", async () => {
    window.history.pushState({}, "", "/websites");
    const s = {
      id: 1, name: "Acme", url: "https://acme.example", client: "", notes: "", enabled: true, last_status: "degraded", last_checked_at: now, last_check: null,
      environment: "production", server_id: null, server_site_key: "", last_diagnosis: {}, last_diagnosis_at: null,
      uptime: { "24h": 100, "7d": 100, "30d": 100, n_24h: 3, n_7d: 3, n_30d: 3, checks_30d: 3, avg_response_ms_24h: 90, first_check_at: ago(2_700_000) },
      open_findings: [{ code: "cloaked_spam", severity: "critical", title: "Cloaked spam for search crawlers", source: "deep", checked_at: ago(3_600_000) }],
      open_findings_sources: { quick: ago(60_000), deep: ago(3_600_000) },
    };
    mockFetch(base({ "GET /api/sites": { sites: [s] }, "GET /api/sites/1": { site: s }, "GET /api/sites/1/checks": { checks: [] }, "GET /api/schedules": { schedules: [] } }));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Acme" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Acme" }));
    expect(dialog.getByText("Cloaked spam for search crawlers")).toBeInTheDocument();
    expect(dialog.getByText(/Found by the security scan/)).toBeInTheDocument();
    expect(dialog.getByRole("heading", { name: /Open issues \(from the security scan of .+ and the uptime check of .+\)/ })).toBeInTheDocument();
    expect(dialog.getAllByText(/Not enough data yet \(3 checks since/)).toHaveLength(3);
  });
});

// ---------------------------------------------------------------------------------------------------------------------

describe("connection states with their age, and cadences from the schedules (C2-04, C2-15)", () => {
  const st = (over: Partial<IntegrationState>): IntegrationState => ({ name: "n8n", last_ok_at: null, last_error_at: null, last_error: "", detail: {}, ...over });
  const t = Date.parse("2026-10-19T12:00:00Z");
  const before = (ms: number) => new Date(t - ms).toISOString();

  it("calls n8n connected only when it answered in the last 3 minutes, always with the age", () => {
    expect(probedConnection("live", st({ last_ok_at: before(40_000) }), t)).toEqual({
      label: "Connected · checked 40 s ago",
      state: "Connected",
      age: "checked 40 s ago",
      tone: "good",
    });
    expect(probedConnection("live", st({ last_ok_at: before(10 * 60_000) }), t).label).toMatch(/^Not checked since /);
    expect(probedConnection("live", st({ last_ok_at: before(10 * 60_000) }), t).tone).toBe("warn");
    expect(probedConnection("live", st({ last_ok_at: before(60_000), last_error_at: before(30_000), detail: { reachable: false } }), t)).toMatchObject({
      label: "Unreachable · 30 s ago",
      tone: "bad",
    });
    expect(probedConnection("live", null, t).label).toBe("Not checked yet");
    expect(probedConnection("disabled", st({ last_ok_at: before(1000) }), t).label).toBe("Not connected");
  });

  it("never calls the webhook engine connected: only its deliveries", () => {
    expect(deliveredConnection("live", st({ last_ok_at: before(5 * 60_000) }), t)).toMatchObject({ label: "Last successful delivery 5 min ago", tone: "muted" });
    expect(deliveredConnection("live", st({ last_ok_at: before(9e6), last_error_at: before(60_000) }), t).label).toBe("Last delivery failed · 1 min ago");
    expect(deliveredConnection("live", null, t).label).toBe("No delivery yet");
    expect(ageText(before(3 * 3_600_000), t)).toBe("3 h ago");
    expect(ageText(before(2 * 86_400_000), t)).toBe("2 d ago");
  });

  it("builds cadences from the real schedules, in UTC", () => {
    const s = (over: Partial<Schedule>) => ({ id: 1, name: "x", description: "", agent: "monitoring", capability: "check_all_sites", params: {}, cron: "*/15 * * * *", enabled: true, last_run_at: null, next_run_at: null, ...over }) as Schedule;
    expect(cadence([s({})], "monitoring", "check_all_sites")).toBe("every 15 minutes");
    expect(cadence([s({ enabled: false })], "monitoring", "check_all_sites")).toBe("paused");
    expect(cadence([], "monitoring", "check_all_sites")).toBe("not scheduled");
    expect(cadence([s({ agent: "wordpress_security", capability: "security_scan_all", cron: "17 3 * * *" })], "wordpress_security", "security_scan_all")).toBe("daily at 03:17 UTC");
    expect(cadence([s({ cron: "5 4 1-7 * 1" })], "monitoring", "check_all_sites")).toBe("on cron 5 4 1-7 * 1 (UTC)");
    expect(cronHint("43 6 * * 1")).toBe("Mondays at 06:43 UTC");
  });

  it("Workflows and Integrations show n8n's state with its age", async () => {
    const n8n = (okAgo: number) => ({
      name: "n8n", title: "n8n workflow engine", mode: "live", description: "", live_requirements: "", webhook_url: "http://n8n:5678/webhook/cc-events",
      status: { name: "n8n", last_ok_at: ago(okAgo), last_error_at: null, last_error: "", detail: { reachable: true } }, workflows: null,
    });
    window.history.pushState({}, "", "/workflows");
    mockFetch(
      base({
        "GET /api/integrations": { integrations: [n8n(40_000)] },
        "GET /api/schedules": { schedules: [] },
        "GET /api/agents": { agents: [] },
        "GET /api/supervisor/requests": { requests: [], total: 0 },
      }),
    );
    const { unmount } = render(<App />);
    const tile = await screen.findByRole("region", { name: "Workflow engine" });
    await waitFor(() => expect(tile).toHaveTextContent(/Connected.*Checked \d+ s ago$/));
    expect(await screen.findByText(/^Connected · checked \d+ s ago$/)).toBeInTheDocument();
    unmount();

    window.history.pushState({}, "", "/integrations");
    mockFetch(base({ "GET /api/integrations": { integrations: [n8n(20 * 60_000)] } }));
    render(<App />);
    expect(await screen.findByText(/Not checked since/)).toBeInTheDocument();
    expect(screen.queryByText(/^Connected/)).toBeNull();
    expect(screen.getByText(/n8n imports the Command Center's event router, morning briefing and lead intake workflows when it starts/)).toBeInTheDocument();
  });

  it("Schedules says cron times are in UTC and reads each one", async () => {
    window.history.pushState({}, "", "/schedules");
    const schedule = { id: 1, name: "wordpress-uptime-sweep", description: "", agent: "monitoring", capability: "check_all_sites", params: {}, cron: "*/15 * * * *", enabled: true, last_run_at: null, next_run_at: null };
    mockFetch(base({ "GET /api/schedules": { schedules: [schedule] } }));
    render(<App />);
    expect(await screen.findByText(/Cron times are in UTC/)).toBeInTheDocument();
    expect(screen.queryByText(/server's time zone/)).toBeNull();
    expect(screen.getByText("Every 15 minutes")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------------------------------------------

describe("sample data, windows and missing values (C2-12, C2-13, C2-14)", () => {
  it("marks ad reports and their tasks as sample data on SEO & Marketing and in the task dialog", async () => {
    window.history.pushState({}, "", "/seo");
    const report = { id: 9, created_at: now, task_id: 12, agent: "social_marketing", kind: "ads_report", title: "Ad performance (7 days, sample data)", content_type: "text/markdown", data: { sample_data: true }, status: "draft" };
    const adTask = {
      id: 12, created_at: now, updated_at: now, request_id: null, schedule_id: null, agent: "social_marketing", capability: "ads_report", params: { days: 7 }, risk: "read",
      status: "succeeded", attempts: 1, started_at: now, finished_at: now, source: "dashboard", created_by: admin.email, summary: "Ads (sample data, 7 days): $900 spend, ROAS 1.20, 1 flag(s)",
      result: { summary: "", data: { sample_data: true }, findings: [{ code: "low_roas", severity: "info", title: "[Sample data] Retargeting: ROAS 0.62" }] }, error: "",
    };
    mockFetch(
      base({
        "GET /api/agents": { agents: [] },
        "GET /api/integrations": { integrations: [] },
        "GET /api/artifacts?agent=social_marketing&kind=ads_report&limit=20": { artifacts: [report] },
        "GET /api/artifacts": { artifacts: [] },
        "GET /api/tasks?agent=social_marketing&limit=30": { tasks: [adTask], total: 1 },
        "GET /api/tasks": { tasks: [], total: 0 },
        "GET /api/tasks/12": { task: adTask, logs: [], approval: null },
      }),
    );
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("Sample data").length).toBeGreaterThanOrEqual(2));
    fireEvent.click(screen.getAllByRole("button", { name: /Ads Report/ })[0]);
    const dialog = within(await screen.findByRole("dialog", { name: "Task #12" }));
    expect(await dialog.findByText("Sample data")).toBeInTheDocument();
    expect(dialog.getByText(/these figures and findings are synthetic/)).toBeInTheDocument();
  });

  it("Agents: 'Awaiting approval' counts what is held now, however old, with the 24 h count as a hint", async () => {
    window.history.pushState({}, "", "/agents");
    const agent = (name: string, over: Record<string, unknown> = {}) => ({
      name, role: name === "supervisor" ? "supervisor" : "agent", title: name, description: "", enabled: true, entitled: true, sensitive: false, integrations: [],
      capabilities: [], stats_24h: {}, last_run_at: null, active: { queued: 0, running: 0, awaiting_approval: 0 }, ...over,
    });
    mockFetch(
      base({
        "GET /api/agents": {
          agents: [agent("supervisor"), agent("wordpress_security", { stats_24h: { awaiting_approval: 1 }, active: { queued: 0, running: 0, awaiting_approval: 3 } })],
        },
      }),
    );
    render(<App />);
    const tile = await screen.findByRole("region", { name: "Awaiting approval" });
    await waitFor(() => expect(tile).toHaveTextContent("3"));
    expect(tile).toHaveTextContent("Held for you now, however old · 1 requested in the last 24 h");
    expect(screen.getAllByText("Awaiting approval now")).toHaveLength(2);
  });

  it("Agents: switching one off asks first and reports the changes it cancelled", async () => {
    window.history.pushState({}, "", "/agents");
    const a = { name: "wordpress_security", role: "agent", title: "WordPress Security & Repair", description: "", enabled: true, entitled: true, sensitive: false, integrations: [], capabilities: [], stats_24h: {}, last_run_at: null };
    const calls = mockFetch(base({ "GET /api/agents": { agents: [a] }, "PATCH /api/agents/wordpress_security": { name: a.name, enabled: false, cancelled_tasks: [31, 32] } }));
    const ask = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    expect(ask).toHaveBeenCalledTimes(1);
    expect(calls.some((c) => c.method === "PATCH")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    expect(await screen.findByText("WordPress Security & Repair is off. Cancelled tasks #31, #32.")).toBeInTheDocument();
  });

  it("shows '…', not 0, while counts are loading", async () => {
    window.history.pushState({}, "", "/servers");
    mockFetch(base({ "GET /api/servers": { status: 500, json: { detail: "the API is restarting" } }, "GET /api/ssh/key": { key: null } }));
    render(<App />);
    expect(await screen.findByText(/Servers \(…\)/)).toBeInTheDocument();
  });
});

describe("Knowledge Base (0.5.0)", () => {
  it("describes the dashboard, the version check and the stricter approvals", async () => {
    window.history.pushState({}, "", "/kb");
    mockFetch(base({ "GET /api/agents": { agents: [] }, "GET /api/integrations": { integrations: [] } }));
    render(<App />);
    expect(await screen.findByRole("heading", { name: "The dashboard" })).toBeInTheDocument();
    expect(screen.getAllByText(/cannot measure this PC's processor, memory or disk/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Not connected/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/smoke\.ps1/, { selector: "code" }).length).toBeGreaterThan(0);
    expect(screen.getByText(/the running API container's version and the folder the container was created from/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Typed confirmations" })).toBeInTheDocument();
    expect(screen.getAllByText(/Host key CHANGED/).length).toBeGreaterThan(0);
    expect(screen.getByRole("heading", { name: "What an approval card shows" })).toBeInTheDocument();
  });
});
