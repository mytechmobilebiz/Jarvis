import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { Modal } from "../components";
import { mockFetch } from "./fetchMock";

const status = {
  app: "Infinite AI Command Center",
  version: "0.4.0",
  environment: "test",
  time: "2026-10-18T12:00:00Z",
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: new Date().toISOString(), alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  workflow_engine: "n8n",
  n8n: { name: "n8n", last_ok_at: new Date().toISOString(), last_error_at: null, last_error: "", detail: {} },
  counts: { sites: { up: 1 }, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
};

const common = {
  "GET /api/auth/me": { user: { id: 1, email: "admin@test.local", display_name: "Administrator", role: "admin" } },
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

describe("command bar error (cr-5)", () => {
  it("gives the focus back to the input when the error is dismissed, instead of losing it", async () => {
    mockFetch({ ...common, "POST /api/supervisor/requests": { status: 503, json: { detail: "planner unavailable" } } });
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    fireEvent.change(input, { target: { value: "Check all websites" } });
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("planner unavailable")).toBeInTheDocument();
    const dismiss = screen.getByRole("button", { name: "Dismiss" });
    act(() => dismiss.focus());
    fireEvent.click(dismiss);
    expect(screen.queryByText("planner unavailable")).toBeNull();
    expect(input).toHaveFocus();
  });
});

describe("Modal focus trap with <details> (a11y-11)", () => {
  function Dialog() {
    return (
      <Modal title="Task #1" onClose={() => undefined}>
        <p>Status</p>
        <details>
          <summary>Result data</summary>
          <button>Inside the closed details</button>
        </details>
      </Modal>
    );
  }

  it("lets Tab reach a summary and skips what a closed <details> hides", () => {
    render(<Dialog />);
    const dialog = screen.getByRole("dialog", { name: "Task #1" });
    const close = within(dialog).getByRole("button", { name: "Close" });
    const summary = dialog.querySelector("summary") as HTMLElement;
    expect(close).toHaveFocus();

    // Shift+Tab from the first item wraps to the summary, not to the button the closed details hides.
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(summary).toHaveFocus();
    // Tab from the summary, the last item, wraps back to Close.
    fireEvent.keyDown(summary, { key: "Tab" });
    expect(close).toHaveFocus();

    // Once the details is open, its button is the last item.
    act(() => (dialog.querySelector("details") as HTMLDetailsElement).setAttribute("open", ""));
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(within(dialog).getByRole("button", { name: "Inside the closed details" })).toHaveFocus();
  });
});
