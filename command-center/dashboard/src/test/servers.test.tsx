import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { mockFetch } from "./fetchMock";

const status = {
  app: "Infinite AI Command Center",
  version: "0.3.0",
  environment: "test",
  time: "2026-10-18T12:00:00Z",
  database: { ok: true },
  workers: [],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  n8n: null,
  counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 1, events_24h: {}, drafts: 0 },
};
const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin", can_access_sensitive: false };
const now = new Date().toISOString();

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.pushState({}, "", "/");
});

describe("production approvals", () => {
  const approval = {
    id: 4, created_at: now, task_id: 9, status: "pending", agent: "wordpress_security", capability: "repair_restore_site_url", risk: "change",
    summary: "wordpress_security.repair_restore_site_url(site_id=3) on site Acme (https://acme.example) (PRODUCTION)",
    details: { params: { site_id: 3 }, target: "site Acme (https://acme.example)", environment: "production", production: true, confirm: "Acme" },
    decided_by: "", decided_at: null, decision_note: "",
  };

  it("needs the admin to type the target's name", async () => {
    window.history.pushState({}, "", "/approvals");
    const calls = mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/approvals?status=pending&limit=500": { approvals: [approval] },
      "POST /api/approvals/4/decision": { json: { approval: { ...approval, status: "approved" } } },
    });
    render(<App />);
    expect(await screen.findByText("PRODUCTION")).toBeInTheDocument();
    const approve = screen.getByRole("button", { name: "Approve" });
    expect(approve).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Type the name to confirm"), { target: { value: "acme" } });
    expect(approve).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Type the name to confirm"), { target: { value: "Acme" } });
    fireEvent.click(approve);
    await waitFor(() => expect(calls.some((c) => c.path === "/api/approvals/4/decision")).toBe(true));
    expect(calls.find((c) => c.path === "/api/approvals/4/decision")!.body).toMatchObject({ approve: true, confirm: "Acme" });
  });

  it("tells operators that an admin must approve production changes", async () => {
    window.history.pushState({}, "", "/approvals");
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: { ...admin, role: "operator" } },
      "GET /api/approvals?status=pending&limit=500": { approvals: [approval] },
    });
    render(<App />);
    expect(await screen.findByText("Production change: an admin must approve it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });
});

describe("servers page", () => {
  it("lets an admin create the SSH key and shows unpinned servers", async () => {
    window.history.pushState({}, "", "/servers");
    let key: unknown = null;
    const calls = mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/ssh/key": () => ({ json: { key } }),
      "POST /api/ssh/key": () => {
        key = { openssh: "ssh-ed25519 AAAAtest infinite-command-center", fingerprint: "SHA256:abc", created_at: now, created_by: admin.email,
          authorized_keys_line: 'command="/usr/local/bin/cc-gate",restrict ssh-ed25519 AAAAtest infinite-command-center' };
        return { status: 201, json: { key } };
      },
      "GET /api/servers": {
        servers: [{ id: 1, name: "cwp-1", host: "203.0.113.5", port: 22, username: "ccagent", environment: "production", host_key_pinned: false,
          host_key_fingerprint: "", host_key_trusted_by: "", host_key_trusted_at: null, enabled: true, notes: "", last_hello: {}, last_seen_at: null,
          created_at: now, sites: [] }],
      },
    });
    render(<App />);
    expect(await screen.findByText("cwp-1")).toBeInTheDocument();
    expect(screen.getByText("not pinned")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create SSH key" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/ssh/key")).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.path === "/api/ssh/key")!.body).toEqual({ replace: false });
    expect(await screen.findByLabelText("authorized_keys line")).toHaveTextContent('command="/usr/local/bin/cc-gate",restrict');
  });
});

describe("server-side check on the website page", () => {
  it("shows what could be read and what the server could not answer", async () => {
    window.history.pushState({}, "", "/websites");
    const site = {
      id: 3, name: "Acme", url: "https://acme.example", client: "", notes: "", enabled: true, last_status: "up", last_checked_at: now,
      last_check: null, environment: "test", server_id: 1, server_site_key: "acme", uptime: null,
      last_diagnosis: {
        at: now, server: "cwp-1", site_urls: { home: "https://acme.example", siteurl: "https://acme.example", expected: "https://acme.example", matches: true },
        plugins: [{ name: "akismet", status: "active", version: "5.3", update: "none", update_version: "" }], admins: [{ ID: 1, user_login: "owner", user_registered: "" }],
        core_verify: { verified: false, files: [], extra_files: ["Warning: File should not exist: wp-includes/x.php"] },
        errors: { core_version: "wp-cli failed" }, findings: [],
      },
      last_diagnosis_at: now,
    };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/sites": { sites: [site] },
      "GET /api/sites/3": { site },
      "GET /api/sites/3/checks?limit=30": { checks: [] },
      "GET /api/servers": { servers: [] },
    });
    render(<App />);
    fireEvent.click(await screen.findByText("Acme"));
    expect(await screen.findByText("modified (1 unexpected file(s))")).toBeInTheDocument();
    expect(screen.getByText(/version not read/)).toBeInTheDocument();
    expect(screen.getByText("core_version")).toBeInTheDocument();
  });

  it("says when parts were never read and when the latest check failed", async () => {
    window.history.pushState({}, "", "/websites");
    const site = {
      id: 3, name: "Acme", url: "https://acme.example", client: "", notes: "", enabled: true, last_status: "up", last_checked_at: now,
      last_check: null, environment: "test", server_id: 1, server_site_key: "acme", uptime: null,
      last_diagnosis: {
        at: now, server: "cwp-1",
        site_urls: { home: "https://acme.example", siteurl: "https://acme.example/wp", expected: "https://acme.example", matches: false,
                     subdirectory_install_suspected: true, suspected_siteurl: "https://acme.example/wp" },
        errors: { plugins: "database down", admins: "database down" }, findings: [],
        last_failed_attempt: { at: now, error: "could not connect to 203.0.113.5:22" },
      },
      last_diagnosis_at: now,
    };
    mockFetch({
      "GET /api/system/status": status,
      "GET /api/auth/me": { user: admin },
      "GET /api/sites": { sites: [site] },
      "GET /api/sites/3": { site },
      "GET /api/sites/3/checks?limit=30": { checks: [] },
      "GET /api/servers": { servers: [] },
    });
    render(<App />);
    fireEvent.click(await screen.findByText("Acme"));
    expect(await screen.findByText(/WordPress in its own folder\?/)).toBeInTheDocument();
    expect(screen.getAllByText("not read")).toHaveLength(2);  // plugins and administrators, not "none"
    expect(screen.getByText(/could not connect to 203\.0\.113\.5:22/)).toBeInTheDocument();
    expect(screen.getByText("(the results above are older)")).toBeInTheDocument();
  });
});
