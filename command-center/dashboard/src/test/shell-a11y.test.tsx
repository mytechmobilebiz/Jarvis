import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { Modal } from "../components";
import { pageTitle } from "../shell/nav";
import { serviceHealth } from "../shell/Sidebar";
import { speechSupported } from "../shell/useSpeech";
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

const admin = { id: 1, email: "admin@test.local", display_name: "Administrator", role: "admin" };

const common = {
  "GET /api/auth/me": { user: admin },
  "GET /api/system/status": status,
  "GET /api/events": { events: [] },
  "GET /api/sites": { sites: [] },
  "GET /api/agents": { agents: [] },
  "GET /api/approvals": { approvals: [] },
  "GET /api/tasks": { tasks: [], total: 0 },
  "GET /api/supervisor/requests": { requests: [] },
};

function supervisorRequest(id: number, state: string, summary = "") {
  return {
    request: { id, created_at: new Date().toISOString(), source: "dashboard", requested_by: "admin@test.local", message: "Check all websites", status: state, planner: "rule-based", plan: { notes: [] }, reply: "", summary, summarizer: "template", error: "", completed_at: null },
    tasks: [],
  };
}

/** A stand-in for the browser's speech recognition that keeps every instance, so a test can deliver results and errors. */
const recognitions: FakeRecognition[] = [];
class FakeRecognition {
  lang = "";
  interimResults = false;
  continuous = false;
  onresult: ((e: unknown) => void) | null = null;
  onerror: ((e: { error: string }) => void) | null = null;
  onend: (() => void) | null = null;
  start = vi.fn();
  stop = vi.fn();
  abort = vi.fn();
  constructor() {
    recognitions.push(this);
  }
}

function withSpeech() {
  vi.stubGlobal("webkitSpeechRecognition", FakeRecognition);
  vi.stubGlobal("isSecureContext", true);
}

afterEach(() => {
  vi.unstubAllGlobals();
  recognitions.length = 0;
  localStorage.clear();
  window.history.pushState({}, "", "/");
});

describe("command bar", () => {
  it("polls a second request sent while the first answer is still open (cr-1)", async () => {
    let next = 7;
    let polls8 = 0;
    const calls = mockFetch({
      ...common,
      "POST /api/supervisor/requests": () => ({ json: { request: { id: next++, status: "planning" } } }),
      "GET /api/supervisor/requests/7": supervisorRequest(7, "completed", "First answer."),
      "GET /api/supervisor/requests/8": () => ({ json: polls8++ === 0 ? supervisorRequest(8, "planning") : supervisorRequest(8, "completed", "Second answer.") }),
    });
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    fireEvent.change(input, { target: { value: "Check all websites" } });
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("First answer.")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "Check all websites again" } });
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("Request #8")).toBeInTheDocument();
    // The first poll says "planning"; the answer must keep polling until the request is done.
    expect(await screen.findByText("Second answer.", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(calls.filter((c) => c.path === "/api/supervisor/requests/8").length).toBeGreaterThanOrEqual(2);
  });

  it("jumps to the input with Ctrl+K, not with '/', and not out of an open dialog (a11y-12)", async () => {
    mockFetch(common);
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    fireEvent.keyDown(document.body, { key: "/" });
    expect(input).not.toHaveFocus();

    const dialog = document.createElement("div");
    dialog.setAttribute("aria-modal", "true");
    document.body.appendChild(dialog);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(input).not.toHaveFocus();
    dialog.remove();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(input).toHaveFocus();
  });

  it("shows suggestions as a group and hides them on Escape without leaving the input (a11y-5)", async () => {
    mockFetch(common);
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    act(() => input.focus());
    expect(screen.getByRole("group", { name: "Suggestions" })).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).toBeNull();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Suggestions" })).toBeNull();
    expect(input).toHaveFocus();
  });

  it("closes the suggestions when the focus moves out of the command bar (a11y-5)", async () => {
    mockFetch(common);
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    act(() => input.focus());
    expect(screen.getByRole("group", { name: "Suggestions" })).toBeInTheDocument();
    act(() => screen.getByRole("link", { name: /^Dashboard/, current: "page" }).focus());
    expect(screen.queryByRole("group", { name: "Suggestions" })).toBeNull();
  });

  it("moves the focus into the voice consent question, and Escape gives it back to the microphone (a11y-6)", async () => {
    withSpeech();
    mockFetch(common);
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    act(() => input.focus());
    const mic = screen.getByRole("button", { name: "Voice input" });
    fireEvent.click(mic);
    const consent = screen.getByRole("dialog", { name: "Voice input" });
    expect(within(consent).getByRole("button", { name: "Use voice input" })).toHaveFocus();
    // The suggestions are not drawn over the question.
    expect(screen.queryByRole("group", { name: "Suggestions" })).toBeNull();
    fireEvent.keyDown(consent, { key: "Escape" });
    expect(screen.queryByRole("dialog", { name: "Voice input" })).toBeNull();
    expect(mic).toHaveFocus();
  });

  it("aborts dictation when a command is sent, so a late result cannot refill the box (sec-3)", async () => {
    withSpeech();
    localStorage.setItem("cc.voice.consent", "yes");
    mockFetch({ ...common, "POST /api/supervisor/requests": { json: { request: { id: 7, status: "planning" } } }, "GET /api/supervisor/requests/7": supervisorRequest(7, "planning") });
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    fireEvent.click(screen.getByRole("button", { name: "Voice input" }));
    const rec = recognitions[0];
    expect(rec.start).toHaveBeenCalled();
    act(() => rec.onresult!({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: "Check all websites" } }] }));
    expect(input).toHaveValue("Check all websites");
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("Request #7")).toBeInTheDocument();
    expect(rec.abort).toHaveBeenCalled();
    expect(rec.stop).not.toHaveBeenCalled();
    expect(rec.onresult).toBeNull();
    // Chrome reports abort() as an "aborted" error; it must not show up as a voice-input error.
    expect(rec.onerror).toBeNull();
    expect(input).toHaveValue("");
  });

  it("announces a voice error and lets it be dismissed (a11y-14, cr-5)", async () => {
    withSpeech();
    localStorage.setItem("cc.voice.consent", "yes");
    mockFetch(common);
    render(<App />);
    await screen.findByLabelText("Command for the AI Supervisor");
    const mic = screen.getByRole("button", { name: "Voice input" });
    fireEvent.click(mic);
    expect(mic).toHaveAttribute("aria-pressed", "true");
    expect(mic).toHaveAccessibleName("Voice input");
    act(() => recognitions[0].onerror!({ error: "no-speech" }));
    expect(screen.getByRole("alert")).toHaveTextContent("No speech was heard. Try again.");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("No speech was heard. Try again.")).toBeNull();
  });

  it("clears a send error when the command is edited, and offers Dismiss (cr-5)", async () => {
    mockFetch({ ...common, "POST /api/supervisor/requests": { status: 503, json: { detail: "planner unavailable" } } });
    render(<App />);
    const input = await screen.findByLabelText("Command for the AI Supervisor");
    fireEvent.change(input, { target: { value: "Check all websites" } });
    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("planner unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
    fireEvent.change(input, { target: { value: "Check all websites now" } });
    expect(screen.queryByText("planner unavailable")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Execute" }));
    expect(await screen.findByText("planner unavailable")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("planner unavailable")).toBeNull();
  });
});

describe("speechSupported", () => {
  it("honours a Permissions-Policy that blocks the microphone, also under Chrome's featurePolicy name (sec-4)", () => {
    withSpeech();
    expect(speechSupported()).toBe(true);
    Object.defineProperty(document, "featurePolicy", { value: { allowsFeature: (f: string) => f !== "microphone" }, configurable: true });
    try {
      expect(speechSupported()).toBe(false);
    } finally {
      delete (document as unknown as { featurePolicy?: unknown }).featurePolicy;
    }
  });
});

describe("account and notifications", () => {
  it("is a disclosure: its name includes the visible name, Escape returns the focus, Sign out is a plain button (a11y-4, a11y-15)", async () => {
    mockFetch(common);
    render(<App />);
    const button = await screen.findByRole("button", { name: "Administrator, account menu" });
    expect(button).not.toHaveAttribute("aria-haspopup");
    fireEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    const panel = screen.getByRole("group", { name: "Account menu" });
    expect(button).toHaveAttribute("aria-controls", panel.id);
    expect(within(panel).getByRole("button", { name: "Sign out" })).toBeInTheDocument();
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("group", { name: "Account menu" })).toBeNull();
    expect(button).toHaveFocus();
  });

  it("closes when the focus moves elsewhere (a11y-4)", async () => {
    mockFetch(common);
    render(<App />);
    const button = await screen.findByRole("button", { name: "Notifications" });
    act(() => button.focus());
    fireEvent.click(button);
    expect(screen.getByRole("group", { name: "Notifications" })).toBeInTheDocument();
    act(() => screen.getByLabelText("Command for the AI Supervisor").focus());
    expect(screen.queryByRole("group", { name: "Notifications" })).toBeNull();
  });
});

describe("phone drawer", () => {
  it("takes the focus, makes the page behind inert, and closes on Escape back to the menu button (a11y-1)", async () => {
    mockFetch(common);
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    const menu = screen.getByRole("button", { name: "Open the menu" });
    fireEvent.click(menu);
    await waitFor(() => expect(within(nav).getAllByRole("link")[0]).toHaveFocus());
    expect((document.querySelector(".topbar") as HTMLElement).inert).toBe(true);
    expect((document.querySelector("main") as HTMLElement).inert).toBe(true);
    expect(within(nav).getByRole("button", { name: "Close the menu" })).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Open the menu" })).toHaveFocus());
    expect((document.querySelector(".topbar") as HTMLElement).inert).toBe(false);
  });

  it("closes from its own close button", async () => {
    mockFetch(common);
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    fireEvent.click(screen.getByRole("button", { name: "Open the menu" }));
    expect(nav).toHaveClass("open");
    fireEvent.click(within(nav).getByRole("button", { name: "Close the menu" }));
    expect(nav).not.toHaveClass("open");
    await waitFor(() => expect(screen.getByRole("button", { name: "Open the menu" })).toHaveFocus());
  });
});

describe("sidebar health", () => {
  it("does not report a workflow-engine problem when none is set up or it was never contacted (cr-3)", async () => {
    expect(serviceHealth({ ...status, workflow_engine: "none", n8n: null }).n8n).toBe("muted");
    expect(serviceHealth({ ...status, n8n: null }).n8n).toBe("muted");
    expect(serviceHealth(status).n8n).toBe("good");
    expect(serviceHealth({ ...status, n8n: { ...status.n8n, last_ok_at: null } } as never).n8n).toBe("warn");

    mockFetch({ ...common, "GET /api/system/status": { ...status, workflow_engine: "none", n8n: null } });
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    expect(await within(nav).findByText("n8n not configured")).toBeInTheDocument();
  });

  it("names a webhook engine and says when nothing was delivered yet (cr-3, C2-04)", async () => {
    mockFetch({ ...common, "GET /api/system/status": { ...status, workflow_engine: "webhook", n8n: null } });
    render(<App />);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    const line = await within(nav).findByText("Webhook: no delivery yet");
    expect(line.closest(".health-line")).toHaveAttribute("title", "Webhook (workflow engine): no delivery yet");
  });
});

describe("page titles (a11y-19)", () => {
  it("maps routes to the sidebar names", () => {
    expect(pageTitle("/")).toBe("Dashboard · Infinite AI Command Center");
    expect(pageTitle("/websites")).toBe("Websites · Infinite AI Command Center");
    expect(pageTitle("/tasks/")).toBe("Tasks · Infinite AI Command Center");
    expect(pageTitle("/schedules")).toBe("Schedules · Infinite AI Command Center");
    expect(pageTitle("/nowhere")).toBe("Infinite AI Command Center");
  });

  it("sets the title for the sign-in page and for the page that is open", async () => {
    mockFetch({ ...common, "GET /api/auth/me": { status: 401, json: { detail: "not signed in" } } });
    const { unmount } = render(<App />);
    await screen.findByLabelText("Email");
    await waitFor(() => expect(document.title).toBe("Sign in · Infinite AI Command Center"));
    unmount();

    window.history.pushState({}, "", "/approvals");
    mockFetch(common);
    render(<App />);
    await waitFor(() => expect(document.title).toBe("Approvals · Infinite AI Command Center"));
  });
});

describe("Modal (a11y-11)", () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return (
      <>
        <button onClick={() => setOpen(true)}>Open dialog</button>
        {open ? (
          <Modal title="Test dialog" onClose={() => setOpen(false)}>
            <input aria-label="Name" />
            <button>Save</button>
          </Modal>
        ) : null}
      </>
    );
  }

  it("moves the focus in, keeps Tab inside and gives the focus back on close", () => {
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open dialog" });
    act(() => opener.focus());
    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Test dialog" });
    const close = within(dialog).getByRole("button", { name: "Close" });
    const save = within(dialog).getByRole("button", { name: "Save" });
    expect(close).toHaveFocus();

    act(() => save.focus());
    fireEvent.keyDown(save, { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(save).toHaveFocus();

    fireEvent.keyDown(save, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });
});
