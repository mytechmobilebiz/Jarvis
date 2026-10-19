// 0.5.0 final verification fixes: an offline PC (C2-22) and sites never checked (C2-09) are said in words, not left
// looking monitored and current; the bell says when its count is a minimum (C2-14); a tile is green only after a real
// check (C2-08).
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CCEvent, Integration, Site, SystemStatus } from "../api";
import App, { InternetLost } from "../App";
import { sitesCard } from "../dashboard/StatCards";
import { unseenAlerts } from "../shell/Menus";
import { mockFetch } from "./fetchMock";

const NOW = Date.now();
const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();

function makeStatus(over: Partial<SystemStatus> = {}): SystemStatus {
  return {
    app: "Infinite AI Command Center",
    version: "0.5.0",
    environment: "test",
    time: new Date(NOW).toISOString(),
    database: { ok: true },
    workers: [{ worker_id: "w1", last_seen_at: new Date(NOW).toISOString(), alive: true, info: {} }],
    llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
    workflow_engine: "n8n",
    n8n: null,
    counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
    ...over,
  };
}

const offline = (since: string | null) =>
  makeStatus({ connectivity: { state: "offline", since, last_ok_at: iso(3_600_000), last_error_at: iso(60_000), last_error: "no answer" } });

function site(id: number, over: Partial<Site> = {}): Site {
  return {
    id,
    name: `Site ${id}`,
    url: `https://s${id}.example`,
    client: "",
    notes: "",
    enabled: true,
    last_status: "up",
    last_checked_at: iso(60_000),
    last_check: null,
    environment: "production",
    server_id: null,
    server_site_key: "",
    created_at: iso(86_400_000),
    updated_at: iso(86_400_000),
    ...over,
  } as Site;
}

const loaded = (sites: Site[]) => ({ data: sites, error: null });
const fresh = { error: null, lastSuccessAt: NOW - 2_000 };

describe("Websites card (C2-09, C2-22)", () => {
  it("names sites that were never checked instead of counting them as monitored", () => {
    expect(sitesCard(loaded([site(1), site(2, { last_checked_at: null, last_status: "unknown" })]))).toEqual({
      value: "2",
      sub: "Monitored · 1 not checked yet",
      tone: "muted",
    });
    expect(sitesCard(loaded([site(1, { last_checked_at: null, last_status: "unknown" })])).sub).toBe("Not checked yet");
    expect(sitesCard(loaded([site(1, { last_status: "down" }), site(2, { last_checked_at: null })])).sub).toBe("1 down · 1 not checked yet");
    expect(sitesCard(loaded([site(1), site(2)])).sub).toBe("Monitored");
  });

  it("says the sites are not being checked while the PC has no internet", () => {
    expect(sitesCard(loaded([site(1), site(2, { last_status: "down" })]), offline(iso(600_000)))).toEqual({
      value: "2",
      sub: "Not checked · no internet",
      tone: "unknown",
    });
    expect(sitesCard(loaded([site(1)]), makeStatus({ connectivity: { state: "online", since: null, last_ok_at: iso(1_000), last_error_at: null, last_error: "" } })).sub).toBe(
      "Monitored",
    );
  });
});

describe("No-internet banner (C2-22)", () => {
  it("appears while the monitor finds the PC offline, and says the site statuses are not current", () => {
    render(<InternetLost status={offline(iso(600_000))} meta={fresh} />);
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(/^No internet on this PC since .+\. Websites are not being checked: their status is the last result/);
    expect(banner).toHaveTextContent("does not count toward uptime");
  });

  it("stays away when online, with an older API, and while the status itself is out of date", () => {
    const { container, rerender } = render(<InternetLost status={makeStatus()} meta={fresh} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<InternetLost status={makeStatus({ connectivity: { state: "online", since: null, last_ok_at: iso(1_000), last_error_at: null, last_error: "" } })} meta={fresh} />);
    expect(container).toBeEmptyDOMElement();
    // The red "Cannot reach the Command Center" banner covers a stale status; this one would repeat an old answer.
    rerender(<InternetLost status={offline(null)} meta={{ error: "HTTP 502", lastSuccessAt: NOW - 60_000 }} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("Bell count window (C2-14)", () => {
  const event = (id: number, severity: CCEvent["severity"] = "warning") => ({ id, severity }) as CCEvent;
  const latest = (from: number, n: number) => Array.from({ length: n }, (_, i) => event(from - i, i % 3 ? "warning" : "info"));

  it("counts unseen warnings, and marks the count as a minimum when every event read is unseen", () => {
    expect(unseenAlerts(latest(100, 30), 0, 30)).toEqual({ count: 20, more: true });
    expect(unseenAlerts(latest(100, 30), 75, 30)).toEqual({ count: 16, more: false });
    // Fewer events than the window: that is all there is.
    expect(unseenAlerts(latest(10, 10), 0, 30)).toEqual({ count: 6, more: false });
    expect(unseenAlerts([], 0, 30)).toEqual({ count: 0, more: false });
  });
});

describe("Integrations 'Failing now' tile (C2-08)", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    window.history.pushState({}, "", "/");
  });
  const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin", can_access_sensitive: false };
  const email = (status: Integration["status"]): Integration =>
    ({ name: "email", title: "Email", mode: "mock", description: "", live_requirements: "", status }) as Integration;

  it("is neutral until an integration was called, and green only once a call worked", async () => {
    window.history.pushState({}, "", "/integrations");
    mockFetch({ "GET /api/auth/me": { user: admin }, "GET /api/system/status": makeStatus(), "GET /api/integrations": { integrations: [email(null)] } });
    const { unmount } = render(<App />);
    const tile = await screen.findByRole("region", { name: "Failing now" });
    await waitFor(() => expect(tile).toHaveTextContent("No integration has been called yet"));
    expect(tile).toHaveClass("tile-muted");
    unmount();

    mockFetch({
      "GET /api/auth/me": { user: admin },
      "GET /api/system/status": makeStatus(),
      "GET /api/integrations": { integrations: [email({ name: "email", last_ok_at: iso(60_000), last_error_at: null, last_error: "", detail: {} })] },
    });
    render(<App />);
    const ok = await screen.findByRole("region", { name: "Failing now" });
    await waitFor(() => expect(ok).toHaveTextContent("No integration's latest call failed"));
    expect(ok).toHaveClass("tile-green");
  });
});
