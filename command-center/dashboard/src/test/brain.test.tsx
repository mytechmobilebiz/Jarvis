// JARVIS Brain page (0.6.0; docs/JARVIS_BRAIN_SPEC.md section 5): every tab from mocked API data, what each role may do,
// the typed rollback confirmation, the convert confirmation, withheld and hidden conversations, the empty state, the
// test-copy banner, the nav entry and the reference notes on a task.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { pageTitle } from "../shell/nav";
import {
  CUSTOMER_PILL,
  emptiedCopy,
  FlagChips,
  hiddenText,
  HOLDS_INTRO,
  holdRuleText,
  identicalLabel,
  previewText,
  PURGE_PROMOTED_TEXT,
  QUARANTINED_TEXT,
  RELEASE_CONFIRM,
  SOURCE_WITHHELD_PENDING_TEXT,
  SOURCE_WITHHELD_TEXT,
  WITHHELD_COPY_TITLE,
  WITHHELD_SESSION,
  WITHHOLD_NOT_REACHED,
  WITHHOLD_REMOVES,
  withheldByAdmin,
  withholdText,
} from "../pages/Brain";
import { mockFetch } from "./fetchMock";

const now = new Date().toISOString();
const status = {
  app: "Infinite AI Command Center",
  version: __APP_VERSION__,
  environment: "test",
  time: now,
  database: { ok: true },
  workers: [{ worker_id: "w1", last_seen_at: now, alive: true, info: {} }],
  llm: { configured: false, provider: "rule-based", model: null, requests_today: 0, daily_cap: 200 },
  workflow_engine: "n8n",
  n8n: null,
  counts: { sites: {}, tasks_24h: {}, tasks_queued: 0, tasks_running: 0, approvals_pending: 0, events_24h: {}, drafts: 0 },
};

const admin = { id: 1, email: "admin@test.local", display_name: "Admin", role: "admin", can_access_sensitive: true };
const operator = { ...admin, id: 2, email: "op@test.local", display_name: "Olive", role: "operator", can_access_sensitive: false };
const viewer = { ...admin, id: 3, email: "view@test.local", display_name: "Vic", role: "viewer", can_access_sensitive: false };
type Person = typeof admin;

const run7 = {
  id: 7, source_system: "jarvis", label: "sample 5", status: "completed", scope: { sample: 5 }, source: { db: { name: "state.db", bytes: 52_428_800 } },
  importer_version: "1.0.0", created_by: "importer (cli)", started_at: "2026-10-18T09:00:00Z", heartbeat_at: "2026-10-18T09:05:00Z",
  finished_at: "2026-10-18T09:06:00Z", checkpoint: {}, counts: { by_action: { imported: 120, withheld: 4, excluded: 9 } }, error: "",
  rolled_back_at: null, rolled_back_by: "",
};

const overview = {
  memory: { total: 42, by_status: { approved: 30, pending_review: 10, quarantined: 2 }, by_kind: { knowledge: 20, business: 22 }, by_agent: { "": 30, seo_content: 12 } },
  archive: { sessions: 5, messages: 133, withheld_messages: 4, first_at: "2025-01-02T10:00:00Z", last_at: "2026-09-30T10:00:00Z", by_staff: { jarvis: 3, sarah: 2 } },
  review: { pending_items: 10, quarantined_items: 2, open_conflicts: 3 },
  workflows: { drafts: 5, by_status: { draft: 4, converted: 1 } },
  imports: { runs: 1, latest: run7 },
  ai_context: "off",
  instance_label: "",
};
const emptyOverview = {
  memory: { total: 0, by_status: {}, by_kind: {}, by_agent: {} },
  archive: { sessions: 0, messages: 0, withheld_messages: 0, first_at: null, last_at: null, by_staff: {} },
  review: { pending_items: 0, quarantined_items: 0, open_conflicts: 0 },
  workflows: { drafts: 0, by_status: {} },
  imports: { runs: 0, latest: null },
  ai_context: "off",
};

function item(over: Record<string, unknown> = {}) {
  return {
    id: 5, kind: "knowledge", kind_label: "Shared knowledge", agent: "", title: "Refund policy", body: "Refunds within 30 days.", tags: ["policy"],
    sensitivity: "general", status: "pending_review", origin: "import", flags: {}, source_system: "jarvis", source_ref: "knowledge/refund-policy.md",
    source_at: "2026-01-02T10:00:00Z", import_run_id: 7, version: 1, created_at: now, created_by: "importer (cli)", updated_at: now, updated_by: "",
    reviewed_by: "", reviewed_at: null, review_note: "", deleted_at: null, needs_admin: false, ...over,
  };
}
const versions = [
  { version: 2, change: "edited", kind: "knowledge", agent: "", title: "Refund policy", body: "Refunds within 30 days.", sensitivity: "general", status: "pending_review", changed_by: "op@test.local", changed_at: now, note: "typo" },
  { version: 1, change: "imported", kind: "knowledge", agent: "", title: "Refund policy", body: "Refunds within 3O days.", sensitivity: "general", status: "pending_review", changed_by: "importer (cli)", changed_at: now, note: "" },
];

function session(over: Record<string, unknown> = {}) {
  return {
    id: 11, source_system: "jarvis", source_key: "jarvis:session:abc", source_session_id: "abc", title: "Weekly SEO sweep", staff: "scout", agent: "seo_content",
    origin: "scheduled", channel: "Slack", lineage: "", parent_source_session_id: "", job_id: "j1", job_name: "weekly-sweep", started_at: "2026-03-01T08:00:00Z",
    ended_at: "2026-03-01T08:10:00Z", end_reason: "", hermes_archived: false, source_message_count: 4, imported_messages: 3, withheld_messages: 0, tool_calls: 1,
    sensitivity: "general", labels: { accomplishment: 1 }, summary: "Run the sweep → Found 3 keywords", flags: { attribution: "job id" }, import_run_id: 7,
    created_at: now, updated_at: now, ...over,
  };
}
function message(over: Record<string, unknown> = {}) {
  return {
    id: 101, session_id: 11, position: 0, role: "user", content: "Run the weekly sweep", truncated: false, tool_name: "", tool_calls: [], at: "2026-03-01T08:00:00Z",
    state: "live", sensitivity: "general", withheld: false, labels: [], redactions: {}, flags: {}, ...over,
  };
}
const messages = [
  message(),
  message({ id: 102, position: 1, role: "assistant", content: "Published the keyword brief.", labels: ["accomplishment"], tool_calls: [{ name: "keyword_brief", arguments: '{"topic":"wp"}' }] }),
  message({ id: 103, position: 3, role: "tool", tool_name: "keyword_brief", content: "3 keywords" }),
];

function draft(over: Record<string, unknown> = {}) {
  return {
    id: 3, source_system: "jarvis", source_key: "jarvis:job:j2", name: "roas-report", staff: "bobby", agent: "social_marketing", capability: "ads_report", params: {},
    compatibility: "mapped", trigger: "schedule", cron: "0 8 * * 1-5", schedule_display: "weekdays at 8am", timezone: "America/New_York", enabled_in_source: true,
    deliver: "slack", approval: { jarvis: "unknown", jarvis_rules: [], command_center: "runs without approval (read or draft only)" },
    error_handling: { jarvis: ["Stop when the budget is exceeded."], command_center: "one attempt; a failure is shown in Automation Logs, and the next scheduled run tries again" },
    duplicate_guard: { jarvis_job_enabled: true, overlaps: "", rule: "The draft never runs. Converting it creates a disabled schedule. Switch the JARVIS job off before enabling that schedule, so the work is not done twice." },
    spec: "Report ROAS.", history: { runs: 40, first: "2025-06-01T08:00:00Z", last: "2026-09-30T08:00:00Z", with_problems: 2, last_runs: ["2026-09-30T08:00:00Z"], from: "the JARVIS backup" },
    notes: ["Sample data until a Meta Ads integration is connected."], status: "reviewed", schedule_id: null, import_run_id: 7, created_at: now, updated_at: now,
    reviewed_by: "op@test.local", reviewed_at: now, ...over,
  };
}

const agents = [
  { agent: "supervisor", title: "AI Supervisor", reads_shared: true, reads_own: true, reads_other_agents: true, reads_customer_items: true, archive: "all", statuses: ["approved"],
    used_by: ["task results (reference notes)", "answers about JARVIS's history"], items: { approved: 30, by_kind: { knowledge: 20, business: 10 } }, sessions: 5 },
  { agent: "credit_repair", title: "Credit Repair & Dispute Management", reads_shared: true, reads_own: true, reads_other_agents: false, reads_customer_items: false, archive: "none",
    statuses: ["approved"], used_by: [] },
];

const skill = item({
  id: 21, kind: "skill", kind_label: "Skill reference", agent: "social_marketing", title: "roas-report", status: "approved", needs_admin: true,
  flags: { skill: "roas-report", staff: "bobby", capabilities: ["ads_report"], compatibility: "mapped", note: "Sample data until a Meta Ads integration is connected.", frontmatter: { name: "roas-report", description: "Weekly ROAS" } },
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

function routes(user: Person, over: Record<string, unknown> = {}) {
  return { "GET /api/auth/me": { user }, "GET /api/system/status": status, "GET /api/brain/overview": overview, ...over };
}

function renderAt(path: string, user: Person, over: Record<string, unknown> = {}) {
  window.history.pushState({}, "", path);
  const calls = mockFetch(routes(user, over));
  render(<App />);
  return calls;
}

const tabNames = () => screen.getAllByRole("tab").map((t) => t.textContent);
const page = () => document.querySelector(".br-page") as HTMLElement;

// ---------------------------------------------------------------------------------------------------------------------

describe("JARVIS Brain: shell", () => {
  it.each([
    ["admin", admin],
    ["viewer", viewer],
  ])("has a sidebar entry after AI Supervisor for a signed-in %s, and the page title", async (_, user) => {
    renderAt("/brain", user);
    const nav = await screen.findByRole("complementary", { name: "Main navigation" });
    const links = within(nav).getAllByRole("link").map((a) => a.textContent);
    const at = links.indexOf("JARVIS Brain");
    expect(at).toBeGreaterThan(0);
    expect(links[at - 1]).toBe("AI Supervisor");
    expect(within(nav).getByRole("link", { name: "JARVIS Brain" })).toHaveAttribute("href", "/brain");
    expect(await screen.findByRole("heading", { level: 1, name: "JARVIS Brain" })).toBeInTheDocument();
    await waitFor(() => expect(document.title).toBe("JARVIS Brain · Infinite AI Command Center"));
    expect(pageTitle("/brain")).toBe("JARVIS Brain · Infinite AI Command Center");
  });

  it("shows the test-copy banner only while the status reports an instance label", async () => {
    renderAt("/brain", admin, { "GET /api/system/status": { ...status, instance_label: "cc-brain-test" } });
    const banner = await screen.findByText((_, el) => el?.classList.contains("test-banner") ?? false);
    expect(banner).toHaveAttribute("role", "status");
    expect(banner.textContent).toBe("TEST COPY · cc-brain-test · not your live Command Center");
    expect(document.querySelector(".layout")).toHaveClass("has-test-banner");
  });

  it("shows no banner on a live install or an older API", async () => {
    renderAt("/brain", admin);
    await screen.findByRole("heading", { level: 1, name: "JARVIS Brain" });
    await waitFor(() => expect(screen.getByRole("region", { name: "Memory items" })).toHaveTextContent("42"));
    expect(document.querySelector(".test-banner")).toBeNull();
    expect(screen.queryByText(/TEST COPY/)).toBeNull();
  });
});

describe("JARVIS Brain: tabs per role", () => {
  it("gives viewers Overview, Search, Agent knowledge and Skills only, even when another tab is asked for", async () => {
    renderAt("/brain?tab=review", viewer);
    await screen.findByRole("heading", { level: 1, name: "JARVIS Brain" });
    expect(tabNames()).toEqual(["Overview", "Search", "Agent knowledge", "Skills"]);
    expect(screen.getByRole("tab", { name: "Overview" })).toHaveAttribute("aria-selected", "true");
  });

  it("gives operators and admins all eight tabs, kept in ?tab=", async () => {
    renderAt("/brain", operator, { "GET /api/brain/agents": { agents } });
    await screen.findByRole("heading", { level: 1, name: "JARVIS Brain" });
    expect(tabNames()).toEqual(["Overview", "Search", "Conversations", "Agent knowledge", "Skills", "Workflows", "Review", "Import history"]);
    fireEvent.click(screen.getByRole("tab", { name: "Agent knowledge" }));
    expect(window.location.search).toBe("?tab=agents");
    expect(screen.getByRole("tab", { name: "Agent knowledge" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("JARVIS Brain: overview", () => {
  it("shows '…', not 0, while the counts load", async () => {
    window.history.pushState({}, "", "/brain");
    mockFetch(routes(admin));
    const answered = globalThis.fetch;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => (String(input).startsWith("/api/brain/overview") ? new Promise(() => {}) : answered(input, init)));
    render(<App />);
    const tile = await screen.findByRole("region", { name: "Memory items" });
    expect(within(tile).getByText("…")).toBeInTheDocument();
    expect(within(tile).queryByText("0")).toBeNull();
    expect(within(screen.getByRole("region", { name: "Conversation archive" })).getByText("…")).toBeInTheDocument();
  });

  it("says 'No JARVIS import yet' before any import, with no error, and that memory never goes into AI prompts", async () => {
    renderAt("/brain", admin, { "GET /api/brain/overview": emptyOverview });
    expect(await screen.findByRole("heading", { name: "No JARVIS import yet" })).toBeInTheDocument();
    expect(page().querySelector(".alert-bad")).toBeNull();
    expect(screen.getByText("Off: memory never goes into AI prompts.")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Last import" })).getByText("None")).toBeInTheDocument();
  });

  it("shows the counts, the last import and a link to Settings for admins", async () => {
    renderAt("/brain", admin);
    const memory = await screen.findByRole("region", { name: "Memory items" });
    await waitFor(() => expect(memory).toHaveTextContent("42"));
    expect(memory).toHaveTextContent("30 approved · 10 pending review · 2 quarantined");
    expect(screen.getByRole("region", { name: "Waiting for review" })).toHaveTextContent("10");
    expect(screen.getByRole("region", { name: "Waiting for review" })).toHaveTextContent("2 quarantined items: an admin must review them");
    expect(screen.getByRole("region", { name: "Open conflicts" })).toHaveTextContent("3");
    expect(screen.getByRole("region", { name: "Conversation archive" })).toHaveTextContent("133 messages · 4 withheld");
    expect(screen.getByRole("region", { name: "Last import" })).toHaveTextContent("Completed");
    expect(screen.getByRole("link", { name: /Change in Settings/ })).toHaveAttribute("href", "/settings");
    expect(screen.queryByText("No JARVIS import yet", { selector: "h2" })).toBeNull();
  });

  it("gives viewers totals only: no review tiles, no Settings link", async () => {
    renderAt("/brain", viewer, { "GET /api/brain/overview": { ...overview, review: undefined, workflows: undefined, memory: { total: 30, by_status: { approved: 30 } }, ai_context: "approved_general" } });
    const memory = await screen.findByRole("region", { name: "Memory items" });
    await waitFor(() => expect(memory).toHaveTextContent("30"));
    expect(screen.queryByRole("region", { name: "Waiting for review" })).toBeNull();
    expect(screen.queryByRole("region", { name: "Open conflicts" })).toBeNull();
    expect(screen.queryByRole("link", { name: /Change in Settings/ })).toBeNull();
    expect(screen.getByText(/An admin changes it on the Settings page/)).toBeInTheDocument();
    expect(screen.getByText(/^On: approved, general memory/)).toBeInTheDocument();
  });
});

describe("JARVIS Brain: search", () => {
  const results = {
    memory: { hits: [item({ excerpt: "Refunds within 30 days", rank: 0.5 })], total: 34 },
    archive: { hits: [{ message: messages[1], session: { id: 11, title: "Weekly SEO sweep", staff: "scout", agent: "seo_content", started_at: "2026-03-01T08:00:00Z" }, excerpt: "Published the keyword brief." }], total: 1 },
    filtered: true,
  };

  it("searches memory and conversations for operators, and opens a message in its conversation", async () => {
    const calls = renderAt("/brain?tab=search", operator, {
      "GET /api/brain/search": results,
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
    });
    fireEvent.change(await screen.findByPlaceholderText("Search memory and conversations"), { target: { value: "refund" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Showing 1 of 34 items")).toBeInTheDocument();
    expect(calls.find((c) => c.path.startsWith("/api/brain/search"))!.path).toBe("/api/brain/search?q=refund&scope=all&limit=20");
    expect(screen.getByRole("button", { name: "Refund policy" })).toBeInTheDocument();
    expect(screen.getAllByText("matched by keywords").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Weekly SEO sweep" }));
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    expect(await within(dialog).findByText("Published the keyword brief.")).toBeInTheDocument();
    expect(dialog.querySelector('[aria-current="true"]')).toHaveAttribute("id", "br-msg-102");
  });

  it("searches memory only for viewers and says when the words filter nothing", async () => {
    const calls = renderAt("/brain?tab=search", viewer, { "GET /api/brain/search": { ...results, archive: { hits: [], total: 0 }, filtered: false } });
    fireEvent.change(await screen.findByPlaceholderText("Search memory"), { target: { value: "the" } });
    expect(screen.queryByLabelText("Where to search")).toBeNull();
    const kinds = Array.from((screen.getByLabelText("Kind") as HTMLSelectElement).options).map((o) => o.value);
    expect(kinds).not.toContain("instruction");
    expect(kinds).not.toContain("skill");
    expect(kinds).toContain("business");
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/None of these words can be searched on/)).toBeInTheDocument();
    expect(calls.find((c) => c.path.startsWith("/api/brain/search"))!.path).toContain("scope=memory");
    expect(screen.queryByRole("heading", { name: /Conversations/ })).toBeNull();
  });
});

describe("JARVIS Brain: conversations", () => {
  it("lists sessions with honest paging and shows a withheld one as withheld", async () => {
    const calls = renderAt("/brain?tab=conversations", operator, {
      "GET /api/brain/sessions": { sessions: [session(), session({ id: 12, title: "", sensitivity: "restricted", summary: "", labels: {}, imported_messages: 0, withheld_messages: 6 })], total: 120 },
    });
    expect(await screen.findByText("Showing 1–2 of 120 conversations")).toBeInTheDocument();
    expect(screen.getByText(WITHHELD_SESSION)).toBeInTheDocument();
    expect(screen.getAllByText("inferred from the scheduled job").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/brain/sessions?limit=50&offset=50")).toBe(true));
  });

  it("sends the filters", async () => {
    const calls = renderAt("/brain?tab=conversations", operator, { "GET /api/brain/sessions": { sessions: [], total: 0 } });
    expect(await screen.findByText("No JARVIS conversations have been imported, or none are visible to you.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("JARVIS staff"), { target: { value: "sarah" } });
    fireEvent.change(screen.getByLabelText("Origin"), { target: { value: "scheduled" } });
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "problem" } });
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-01-01" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/brain/sessions?staff=sarah&origin=scheduled&label=problem&since=2026-01-01&limit=50&offset=0")).toBe(true));
    expect(await screen.findByText("No conversations match these filters.")).toBeInTheDocument();
  });

  it("shows messages with roles, collapsed tool calls, keyword labels and the hidden customer messages", async () => {
    renderAt("/brain?tab=conversations&session=11", operator, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 2 },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    expect(await within(dialog).findByText("Run the weekly sweep")).toBeInTheDocument();
    expect(within(dialog).getByText("User")).toBeInTheDocument();
    expect(within(dialog).getByText("Assistant")).toBeInTheDocument();
    expect(within(dialog).getByText("Tool result")).toBeInTheDocument();
    const calls = within(dialog).getByText("1 tool call");
    expect(calls.closest("details")).not.toHaveAttribute("open");
    expect(within(dialog).getAllByText("matched by keywords").length).toBeGreaterThan(0);
    expect(within(dialog).getByText(hiddenText(2))).toBeInTheDocument();
    expect(hiddenText(2)).toBe("2 messages hidden: customer data (admins only)");
    expect(hiddenText(1)).toBe("1 message hidden: customer data (admins only)");
  });

  it("says a restricted conversation is withheld, with no message text", async () => {
    renderAt("/brain?tab=conversations&session=12", admin, {
      "GET /api/brain/sessions": { sessions: [], total: 0 },
      "GET /api/brain/sessions/12": { session: session({ id: 12, title: "", summary: "", sensitivity: "restricted", withheld_messages: 6 }), messages: [message({ id: 120, content: "", withheld: true })], total: 1, hidden: 0 },
    });
    const dialog = await screen.findByRole("dialog", { name: "Withheld conversation" });
    expect(within(dialog).getByText(WITHHELD_SESSION)).toBeInTheDocument();
    expect(WITHHELD_SESSION).toBe("Withheld: this conversation contained restricted data; it is kept only in the JARVIS backup");
    expect(within(dialog).queryByRole("button", { name: "Promote to memory" })).toBeNull();
  });

  it("lets operators promote a message but not withhold it", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", operator, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "POST /api/brain/messages/102/promote": { json: { item: item({ id: 77, origin: "promoted" }) } },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    expect(within(dialog).getAllByRole("button", { name: "Promote to memory" })).toHaveLength(3);
    expect(within(dialog).queryByRole("button", { name: "Withhold" })).toBeNull();
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Promote to memory" })[1]);
    const form = await screen.findByRole("dialog", { name: "Promote to memory" });
    expect(within(form).getByLabelText("Title")).toHaveValue("Published the keyword brief.");
    fireEvent.change(within(form).getByLabelText("Kind"), { target: { value: "accomplishment" } });
    fireEvent.click(within(form).getByRole("button", { name: "Promote" }));
    expect(await within(form).findByText(/Saved as memory item #77, waiting for review/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/brain/messages/102/promote")!.body).toEqual({ kind: "accomplishment", title: "Published the keyword brief.", agent: "seo_content" });
    // The count of promoted copies is an admin's, asked for only when a withhold is about to be confirmed.
    expect(calls.some((c) => c.path.endsWith("/promoted"))).toBe(false);
  });

  it("offers operators no admin-only kind, and leaves a message a secret was removed from to an admin", async () => {
    const withSecret = messages.map((m, i) => (i === 1 ? { ...m, redactions: { credential_assignment: 1 } } : m));
    renderAt("/brain?tab=conversations&session=11", operator, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages: withSecret, total: 3, hidden: 0 },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Promote to memory" })[1]);
    const form = await screen.findByRole("dialog", { name: "Promote to memory" });
    const kinds = within(within(form).getByLabelText("Kind")).getAllByRole("option").map((o) => (o as HTMLOptionElement).value);
    expect(kinds).not.toContain("instruction");
    expect(kinds).not.toContain("skill");
    expect(within(form).getByText(/an admin promotes it/)).toBeInTheDocument();
    expect(within(form).getByRole("button", { name: "Promote" })).toBeDisabled();
  });

  it("keeps the conversation and the promote result open when the new item is opened", async () => {
    renderAt("/brain?tab=conversations&session=11", operator, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "POST /api/brain/messages/102/promote": { json: { item: item({ id: 77, origin: "promoted" }) } },
      "GET /api/brain/items/77": { item: item({ id: 77, title: "Keyword brief published", origin: "promoted", import_run_id: null }), versions: [] },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Promote to memory" })[1]);
    const form = await screen.findByRole("dialog", { name: "Promote to memory" });
    fireEvent.click(within(form).getByRole("button", { name: "Promote" }));
    fireEvent.click(await within(form).findByRole("button", { name: "Open item #77" }));
    expect(await screen.findByRole("dialog", { name: "Keyword brief published" })).toBeInTheDocument();
    expect(window.location.search).toContain("item=77");
    // The conversation was not reloaded or reset underneath.
    expect(screen.getByRole("dialog", { name: "Weekly SEO sweep" })).toBeInTheDocument();
    expect(screen.getByText(/Saved as memory item #77, waiting for review/)).toBeInTheDocument();
  });

  it("never opens a conversation for a viewer, even from the address", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11&message=102", viewer, {
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
    });
    await screen.findByRole("heading", { level: 1, name: "JARVIS Brain" });
    await waitFor(() => expect(calls.some((c) => c.path.startsWith("/api/brain/overview"))).toBe(true));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(calls.some((c) => c.path.startsWith("/api/brain/sessions"))).toBe(false);
    expect(screen.queryByRole("button", { name: "Promote to memory" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Withhold" })).toBeNull();
  });

  it("lets admins withhold a message after a confirmation", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", admin, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "GET /api/brain/messages/102/promoted": { message: 102, count: 0, items: [] },
      "POST /api/brain/messages/102/withhold": { json: { message: { ...messages[1], content: "", withheld: true } } },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    expect(within(dialog).getAllByRole("button", { name: "Withhold" })).toHaveLength(3);
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[1]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #1" });
    expect(calls.some((c) => c.path.endsWith("/withhold"))).toBe(false);
    await waitFor(() => expect(within(confirm).getByRole("button", { name: "Withhold this message" })).toBeEnabled());
    fireEvent.click(within(confirm).getByRole("button", { name: "Withhold this message" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/brain/messages/102/withhold")).toBe(true));
  });

  it("warns how many promoted memory items a withhold empties, before the admin confirms", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", admin, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "GET /api/brain/messages/102/promoted": { message: 102, count: 2, items: [{ id: 77, status: "approved", deleted: false }] },
      "POST /api/brain/messages/102/withhold": {
        json: { message: { ...messages[1], content: "", withheld: true }, session: session(), copies: { items: [77, 78], versions_blanked: 3 }, answers_scrubbed: 1, rollback_copies_blanked: 0 },
      },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[1]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #1" });
    expect(await within(confirm).findByText(/^2 memory item\(s\) were promoted from this message\./)).toBeInTheDocument();
    expect(within(confirm).getByText(/including edits made since/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/brain/messages/102/promoted")?.method).toBe("GET");
    expect(calls.some((c) => c.path.endsWith("/withhold"))).toBe(false);
    fireEvent.click(within(confirm).getByRole("button", { name: "Withhold this message" }));
    expect(await within(dialog).findByText("Withheld. 2 promoted item(s) emptied.")).toBeInTheDocument();
    expect(withholdText(1)[1]).toMatch(/^1 memory item\(s\) were promoted from this message\. Their title, text, tags and review notes, and every earlier version, are removed too/);
    expect(withholdText(0)).toEqual([WITHHOLD_REMOVES, WITHHOLD_NOT_REACHED]);
  });

  it("no longer says the text stays only in the JARVIS backup, and lists what a withhold cannot reach", async () => {
    renderAt("/brain?tab=conversations&session=11", admin, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "GET /api/brain/messages/102/promoted": { message: 102, count: 0, items: [] },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[1]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #1" });
    expect(await within(confirm).findByText(WITHHOLD_NOT_REACHED)).toBeInTheDocument();
    expect(within(confirm).getByText(WITHHOLD_REMOVES)).toBeInTheDocument();
    expect(confirm.textContent).not.toMatch(/stays only in the JARVIS backup/);
    expect(confirm.textContent).not.toMatch(/memory item\(s\) were promoted/);
    for (const kept of ["the JARVIS backup", "Command Center backups", "decisions files", "the test copy", "Docker container logs", "an AI model", "title", "paired tool result"]) {
      expect(WITHHOLD_NOT_REACHED).toContain(kept);
    }
  });

  it("lets an admin withhold when the copies cannot be counted, and says copies are emptied too", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", admin, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "GET /api/brain/messages/102/promoted": { status: 500, json: { detail: "the database refused this change; nothing was saved" } },
      "POST /api/brain/messages/102/withhold": { json: { message: { ...messages[1], content: "", withheld: true }, copies: { items: [], versions_blanked: 0 } } },
    });
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[1]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #1" });
    expect(await within(confirm).findByText(/^Could not count the promoted copies \(.+\); any copies are emptied too\.$/)).toBeInTheDocument();
    fireEvent.click(within(confirm).getByRole("button", { name: "Withhold this message" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/brain/messages/102/withhold")).toBe(true));
    expect(await within(dialog).findByText("Withheld. 0 promoted item(s) emptied.")).toBeInTheDocument();
  });
});

describe("JARVIS Brain: withholding the same JARVIS message (0.6.2)", () => {
  const row = (id: number, position: number) => ({ id, session: 11, position, role: "user", at: "2026-03-01T08:00:00Z", state: "live" as const, withheld: false });
  function preview(over: Record<string, unknown> = {}) {
    return {
      message: 101, same_message_rows: [row(140, 4)], held_rows: [row(141, 5), row(142, 6)], identical_text_rows: [row(150, 9), row(151, 12)],
      rowless_copies: 1, items_with_this_text: [{ id: 66, title: "Weekly sweep notes" }], first_user_message: true, ...over,
    };
  }
  function withholdRoutes(over: Record<string, unknown> = {}) {
    return {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session(), messages, total: 3, hidden: 0 },
      "GET /api/brain/messages/101/promoted": { message: 101, count: 0, items: [] },
      "GET /api/brain/messages/101/withhold-preview": preview(),
      "POST /api/brain/messages/101/withhold": {
        json: { message: { ...messages[0], content: "", withheld: true }, copies: { items: [] }, also_withheld: [140], held_for_review: [141, 142], identical_withheld: 2, title_hidden: true },
      },
      ...over,
    };
  }

  it("lists the same message's other rows and the held ones, offers identical text unticked and the title ticked for the first user message", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", admin, withholdRoutes());
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Run the weekly sweep");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[0]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #0" });
    expect(await within(confirm).findByText("1 other stored row is the same JARVIS message and is withheld too; 2 are held for your review.")).toBeInTheDocument();
    expect(within(confirm).getByText(/1 copy JARVIS keeps without a stored row/)).toBeInTheDocument();
    const identical = within(confirm).getByRole("checkbox", { name: identicalLabel(2) });
    // Review 1 R15: identical rows can be in a continuation or a branch, and the label says so.
    expect(identicalLabel(2)).toBe("Also withhold 2 other messages with exactly this text in this conversation and its continuations and branches");
    expect(identical).not.toBeChecked();
    const title = within(confirm).getByRole("checkbox", { name: "Hide the conversation title" });
    expect(title).toBeChecked();
    expect(within(confirm).getByText(/1 imported memory item contains this text\. They are not removed/)).toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Weekly sweep notes" })).toBeInTheDocument();
    // The 0.6.1 wording stays.
    expect(within(confirm).getByText(WITHHOLD_REMOVES)).toBeInTheDocument();
    expect(within(confirm).getByText(WITHHOLD_NOT_REACHED)).toBeInTheDocument();
    // Nothing is withheld before the admin confirms, and identical text only when ticked.
    expect(calls.some((c) => c.method === "POST")).toBe(false);
    fireEvent.click(identical);
    fireEvent.click(title);
    fireEvent.click(within(confirm).getByRole("button", { name: "Withhold this message" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST" && c.path === "/api/brain/messages/101/withhold")?.body).toEqual({ include_identical: true, hide_title: false }));
    expect(
      await within(dialog).findByText(
        "Withheld. 0 promoted item(s) emptied. 1 other stored row of the same JARVIS message withheld too. 2 held for your review (JARVIS Brain → Review). 2 messages with exactly this text withheld. The conversation's title is hidden.",
      ),
    ).toBeInTheDocument();
  });

  it("sends the defaults when the admin changes nothing: identical text left alone, the title as the preview says", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", admin, withholdRoutes({
      "GET /api/brain/messages/102/promoted": { message: 102, count: 0, items: [] },
      "GET /api/brain/messages/102/withhold-preview": preview({ message: 102, same_message_rows: [], held_rows: [], identical_text_rows: [], rowless_copies: 0, items_with_this_text: [], first_user_message: false }),
      "POST /api/brain/messages/102/withhold": { json: { message: { ...messages[1], content: "", withheld: true }, copies: { items: [] } } },
    }));
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Published the keyword brief.");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[1]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #1" });
    const title = await within(confirm).findByRole("checkbox", { name: "Hide the conversation title" });
    expect(title).not.toBeChecked();
    expect(within(confirm).queryByRole("checkbox", { name: /Also withhold/ })).toBeNull();
    expect(confirm.textContent).not.toMatch(/same JARVIS message/);
    fireEvent.click(within(confirm).getByRole("button", { name: "Withhold this message" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ include_identical: false, hide_title: false }));
    expect(await within(dialog).findByText("Withheld. 0 promoted item(s) emptied.")).toBeInTheDocument();
  });

  it("still withholds when the preview cannot be loaded, and leaves the title to the server", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", admin, withholdRoutes({
      "GET /api/brain/messages/101/withhold-preview": { status: 500, json: { detail: "the database refused this change; nothing was saved" } },
    }));
    const dialog = await screen.findByRole("dialog", { name: "Weekly SEO sweep" });
    await within(dialog).findByText("Run the weekly sweep");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[0]);
    const confirm = await screen.findByRole("dialog", { name: "Withhold message #0" });
    expect(await within(confirm).findByText(/^Could not list the other stored copies of this message/)).toBeInTheDocument();
    expect(within(confirm).queryByRole("checkbox")).toBeNull();
    fireEvent.click(within(confirm).getByRole("button", { name: "Withhold this message" }));
    await waitFor(() => expect(calls.find((c) => c.method === "POST")?.body).toEqual({ include_identical: false, hide_title: null }));
  });

  it("says why a withheld message is withheld, and that a title is hidden", async () => {
    const held = [
      message({ id: 130, content: "", withheld: true, flags: { withheld_by_admin: { note: "withheld by an admin", via: "same text", review: "pending" } } }),
      message({ id: 131, position: 1, role: "assistant", content: "", withheld: true, flags: { withheld_by_admin: { note: "withheld by an admin", guard: "key" } } }),
      message({ id: 132, position: 2, role: "assistant", content: "", withheld: true, flags: { withheld_by_admin: { note: "withheld by an admin" } } }),
    ];
    renderAt("/brain?tab=conversations&session=11", admin, {
      "GET /api/brain/sessions": { sessions: [session()], total: 1 },
      "GET /api/brain/sessions/11": { session: session({ title: "", flags: { attribution: "job id", title_withheld: true } }), messages: held, total: 3, hidden: 0 },
    });
    const dialog = await screen.findByRole("dialog", { name: "Untitled conversation" });
    expect(await within(dialog).findByText("held for review")).toBeInTheDocument();
    expect(within(dialog).getByText("withheld by the database guard")).toBeInTheDocument();
    expect(within(dialog).getByText("withheld by an admin")).toBeInTheDocument();
    expect(within(dialog).getByText("title hidden by an admin")).toBeInTheDocument();
    expect(withheldByAdmin({ released_at: now })).toMatch(/^released/);
    expect(withheldByAdmin({ review: "kept" })).toBe("withheld by an admin (kept after review)");
  });

  it("shows how many changed versions were rejected, never a hash (review 2, S16)", () => {
    const { container } = render(<FlagChips flags={{ rejected_versions: 2 }} />);
    expect(within(container).getByText("2 changed versions were rejected before")).toBeInTheDocument();
    cleanup();
    const one = render(<FlagChips flags={{ rejected_versions: 1 }} />);
    expect(within(one.container).getByText("a changed version was rejected before")).toBeInTheDocument();
    expect(one.container.textContent).not.toMatch(/[0-9a-f]{64}/);
  });

  it("puts every count into words", () => {
    expect(previewText(preview({ same_message_rows: [], held_rows: [], rowless_copies: 0 }))).toEqual([]);
    expect(previewText(preview({ same_message_rows: [row(1, 1), row(2, 2)], held_rows: [], rowless_copies: 0 }))).toEqual([
      "2 other stored rows are the same JARVIS message and are withheld too; 0 are held for your review.",
    ]);
    expect(identicalLabel(1)).toBe("Also withhold 1 other message with exactly this text in this conversation and its continuations and branches");
  });

  it("counts rows outside the admin's scope in one sentence of their own, never per list (review 1 R19)", () => {
    // One total: a count per list would tell whether a hidden row's text equals this one (same message) or not (held).
    expect(previewText(preview({ same_message_rows: [row(1, 1)], held_rows: [], rowless_copies: 0, rows_outside_your_scope: 1 }))).toEqual([
      "1 other stored row is the same JARVIS message and is withheld too; 0 are held for your review.",
      "1 stored row outside what you may open is not listed; the withhold treats it by the same rules.",
    ]);
    expect(previewText(preview({ same_message_rows: [], held_rows: [], rowless_copies: 0, rows_outside_your_scope: 2 }))).toEqual([
      "2 stored rows outside what you may open are not listed; the withhold treats them by the same rules.",
    ]);
    // The old per-list fields, if a server still sent them, add nothing to the visible counts.
    const old = { ...preview({ same_message_rows: [], held_rows: [], rowless_copies: 0 }), same_message_rows_hidden: 1, held_rows_hidden: 0 };
    expect(previewText(old)).toEqual([]);
  });
});

describe("JARVIS Brain: held for review (0.6.2)", () => {
  const hold = {
    message: 141, session: 11, title: "Weekly SEO sweep", position: 5, role: "assistant", at: "2026-03-01T08:05:00Z", content_chars: 120, state: "live",
    rule: "same message id, different text", withheld_message: { id: 102, position: 1, role: "assistant", at: "2026-03-01T08:01:00Z", content_chars: 118 },
    compare: "different text", neighbours: [101, 103],
  };
  const hiddenTitle = { ...hold, message: 142, title: null, position: 7, rule: "same text", compare: "identical text", withheld_message: { id: null, position: null, role: "user", at: null, content_chars: null } };
  function holdRoutes(over: Record<string, unknown> = {}) {
    return {
      "GET /api/brain/items?status=quarantined&limit=100": { items: [], total: 0 },
      "GET /api/brain/items?status=pending_review&limit=100": { items: [], total: 0 },
      "GET /api/brain/conflicts": { conflicts: [], total: 0 },
      "GET /api/brain/items?include_deleted=true&limit=200": { items: [], total: 0 },
      "GET /api/brain/holds": { holds: [hold, hiddenTitle], total: 2 },
      "POST /api/brain/holds/141/keep": { json: { message: {}, review: "kept" } },
      "POST /api/brain/holds/142/release": { json: { message: {}, released: true } },
      ...over,
    };
  }

  it("lists held messages with the evidence and never their text, and keeps one withheld", async () => {
    const calls = renderAt("/brain?tab=review", admin, holdRoutes());
    const card = (await screen.findByRole("heading", { name: "Held for review" })).closest(".card") as HTMLElement;
    expect(within(card).getByText(HOLDS_INTRO)).toBeInTheDocument();
    expect(await within(card).findByRole("button", { name: "Message #5 (Assistant) in Weekly SEO sweep" })).toBeInTheDocument();
    expect(within(card).getByRole("button", { name: "Message #7 (Assistant) in a conversation whose title is hidden" })).toBeInTheDocument();
    expect(within(card).getByText(holdRuleText("same message id, different text"))).toBeInTheDocument();
    expect(within(card).getByText(holdRuleText("same text"))).toBeInTheDocument();
    expect(within(card).getAllByText(/120 characters/).length).toBe(2);
    expect(within(card).getByText(/different text$/)).toBeInTheDocument();
    expect(within(card).getByText(/The withheld message it may be: #1 \(Assistant, 118 characters/)).toBeInTheDocument();
    expect(within(card).getByText(/The withheld message it may be: no longer stored \(User\)/)).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: "Keep message #5 (Assistant) withheld" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/brain/holds/141/keep")).toBe(true));
  });

  it("shows the server's evidence sentence for a hold, in the list and in the release dialog (rules062-confirm)", async () => {
    const evidence = "same message id; the withheld original is not in this backup (its row is gone, or JARVIS rewrote it since), so the text could not be compared; probably the same message";
    const uncompared = { ...hiddenTitle, rule: "same message id, nothing to compare", compare: "nothing to compare", evidence };
    renderAt("/brain?tab=review", admin, holdRoutes({ "GET /api/brain/holds": { holds: [hold, uncompared], total: 2 } }));
    const card = (await screen.findByRole("heading", { name: "Held for review" })).closest(".card") as HTMLElement;
    expect(await within(card).findByText(`Evidence: ${evidence}.`)).toBeInTheDocument();
    expect(within(card).getByText(holdRuleText("same message id, nothing to compare"))).toBeInTheDocument();
    expect(within(card).getAllByText(/^Evidence: /).length).toBe(1); // a hold without the field shows no sentence
    fireEvent.click(within(card).getByRole("button", { name: "Release message #7 (Assistant)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #7" });
    expect(within(confirm).getByText(`Evidence: ${evidence}.`)).toBeInTheDocument();
  });

  it("says when a hold's text contains the withheld message's text (rules062)", () => {
    expect(holdRuleText("same message id, contains the withheld text")).toBe(
      "It shares the withheld message's JARVIS id and its text contains the withheld message's text, with more around it. Releasing brings back the withheld text too.",
    );
  });

  it("says what a same-time hold is: the withheld message's conversation, role and time, nothing to compare (review 2, S2)", () => {
    expect(holdRuleText("same conversation, role and time; nothing to compare")).toBe(
      "It is in the withheld message's own JARVIS conversation, with its role and very time (JARVIS keeps a message's time when it writes it again), but no row of the withheld message is here to compare it with: probably the same message. Releasing brings back whatever it holds.",
    );
  });

  it("releases a held message only after the admin confirms what comes back", async () => {
    const calls = renderAt("/brain?tab=review", admin, holdRoutes());
    const card = (await screen.findByRole("heading", { name: "Held for review" })).closest(".card") as HTMLElement;
    fireEvent.click(await within(card).findByRole("button", { name: "Release message #7 (Assistant)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #7" });
    expect(within(confirm).getByText(RELEASE_CONFIRM)).toBeInTheDocument();
    expect(RELEASE_CONFIRM).toBe("Its text comes back at the next import; promoted copies stay empty.");
    expect(within(confirm).getByText(holdRuleText("same text"))).toBeInTheDocument();
    expect(calls.some((c) => c.path.endsWith("/release"))).toBe(false);
    fireEvent.click(within(confirm).getByRole("button", { name: "Release" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/brain/holds/142/release")).toBe(true));
  });

  it("shows the API's refusal when a release is refused", async () => {
    renderAt("/brain?tab=review", admin, holdRoutes({
      "POST /api/brain/holds/142/release": { status: 400, json: { detail: "it is the same message as a withheld one: it cannot be released" } },
    }));
    const card = (await screen.findByRole("heading", { name: "Held for review" })).closest(".card") as HTMLElement;
    fireEvent.click(await within(card).findByRole("button", { name: "Release message #7 (Assistant)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #7" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Release" }));
    expect(await within(confirm).findByText(/it cannot be released/)).toBeInTheDocument();
  });

  it("is not shown to operators, who never ask for it", async () => {
    const calls = renderAt("/brain?tab=review", operator, holdRoutes());
    await screen.findByRole("heading", { name: "Open conflicts" });
    expect(screen.queryByRole("heading", { name: "Held for review" })).toBeNull();
    expect(calls.some((c) => c.path.startsWith("/api/brain/holds"))).toBe(false);
  });
});

describe("JARVIS Brain: item dialog", () => {
  const flagged = item({
    status: "quarantined", sensitivity: "customer", needs_admin: true,
    flags: { secrets_redacted: { api_key: 2 }, instruction_like: ["approval_bypass"], approval_conflict: true, script: true, template: "shipped default", truncated: true, masked: { email_address: 1 } },
  });

  it("shows flags in plain words, the quarantine reason, versions and every admin action to an admin", async () => {
    const calls = renderAt("/brain?item=5", admin, {
      "GET /api/brain/items/5": { item: flagged, versions },
      "POST /api/brain/items/5/review": { json: { item: { ...flagged, status: "approved" } } },
      "POST /api/brain/items/5/versions/1/redact": { json: {} },
      "DELETE /api/brain/items/5": { json: { item: { ...flagged, deleted_at: now } } },
    });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    for (const chip of ["secrets removed", "instruction-like", "approval conflict", "script (never run)", "template (shipped default)", "truncated", CUSTOMER_PILL]) {
      expect(within(dialog).getByText(chip)).toBeInTheDocument();
    }
    expect(within(dialog).getByText("secrets removed")).toHaveAttribute("title", "Removed: api key ×2 (the values were never stored)");
    expect(within(dialog).getByText(QUARANTINED_TEXT)).toBeInTheDocument();
    expect(QUARANTINED_TEXT.toLowerCase()).toContain("quarantined: held a secret (removed) or an instruction-like rule; an admin must review it");
    expect(within(dialog).getByText("knowledge/refund-policy.md")).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Versions" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "Reject" })).toBeEnabled();
    expect(within(dialog).getByRole("button", { name: "Archive" })).toBeEnabled();
    // A quarantined item is not moved to pending review, where operators would read it.
    expect(within(dialog).queryByRole("button", { name: "Reopen for review" })).toBeNull();
    fireEvent.change(within(dialog).getByLabelText("Review note"), { target: { value: "checked" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/brain/items/5/review")?.body).toEqual({ decision: "approve", note: "checked" }));

    fireEvent.click(within(dialog).getByRole("button", { name: "Redact version 1" }));
    const confirm = await screen.findByRole("dialog", { name: "Redact version 1" });
    fireEvent.click(within(confirm).getByRole("button", { name: "Redact this version" }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/api/brain/items/5/versions/1/redact")).toBe(true));

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/brain/items/5")).toBe(true));
  });

  it("tells an admin-redacted version from a rescan's re-cleaned one", async () => {
    const history = [
      { ...versions[0], version: 3, change: "redacted", body: "Refunds within 30 days. Key [secret removed].", note: "rescan" },
      { ...versions[0], version: 2, change: "edited", title: "", body: "" },
      versions[1],
    ];
    renderAt("/brain?item=5", admin, { "GET /api/brain/items/5": { item: item({ version: 3 }), versions: history } });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    expect(within(dialog).getByText("Refunds within 30 days. Key [secret removed].")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Redact version 3" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Redact version 2" })).toBeNull();
    expect(within(dialog).getAllByText("Text redacted by an admin.")).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: "Redact version 1" })).toBeInTheDocument();
  });

  it("shows a source-withheld item as emptied: a risk chip in plain words, no Approve, Reopen or Edit, delete and purge offered", async () => {
    const emptied = item({
      title: "Withheld: the archived message it was promoted from was withheld", body: "", tags: [], status: "rejected", origin: "promoted", import_run_id: null,
      source_ref: "JARVIS session abc · message 1", needs_admin: true, version: 4,
      flags: { message_id: 102, archive_session_id: 11, message_source_key: "jarvis:message:abc:1", source_withheld: true },
    });
    const calls = renderAt("/brain?item=5", admin, {
      "GET /api/brain/items/5": { item: emptied, versions },
      "DELETE /api/brain/items/5": { json: { item: { ...emptied, deleted_at: now } } },
    });
    const dialog = await screen.findByRole("dialog", { name: emptied.title as string });
    expect(within(dialog).getByText("source message withheld")).toHaveClass("br-chip-risk");
    expect(within(dialog).getByText(SOURCE_WITHHELD_TEXT)).toBeInTheDocument();
    expect(within(dialog).queryByText(QUARANTINED_TEXT)).toBeNull();
    for (const name of ["Approve", "Reject", "Archive", "Reopen for review", "Edit"]) {
      expect(within(dialog).queryByRole("button", { name })).toBeNull();
    }
    expect(within(dialog).queryByRole("region", { name: "Review" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/brain/items/5")).toBe(true));
    cleanup();

    renderAt("/brain?item=5", admin, { "GET /api/brain/items/5": { item: { ...emptied, deleted_at: now }, versions } });
    const deleted = await screen.findByRole("dialog", { name: emptied.title as string });
    expect(within(deleted).getByRole("button", { name: "Restore" })).toBeInTheDocument();
    fireEvent.click(within(deleted).getByRole("button", { name: "Purge for good" }));
    const confirm = await screen.findByRole("dialog", { name: "Purge item #5" });
    expect(confirm.textContent).toContain(PURGE_PROMOTED_TEXT);
    expect(confirm.textContent).not.toMatch(/remembered as\s+purged/);
  });

  it("never calls a flagged copy that still holds its text emptied (0.6.0 left it), and offers no Approve, Reopen or Edit", async () => {
    const left = item({
      title: "Plan for the renewal", body: "The renewal plan in full.", status: "approved", origin: "promoted", import_run_id: null,
      source_ref: "JARVIS session abc · message 1", needs_admin: true, version: 2,
      flags: { message_id: 102, archive_session_id: 11, message_source_key: "jarvis:message:abc:1", source_withheld: true },
    });
    renderAt("/brain?item=5", admin, { "GET /api/brain/items/5": { item: left, versions } });
    const dialog = await screen.findByRole("dialog", { name: "Plan for the renewal" });
    expect(within(dialog).getByText(SOURCE_WITHHELD_PENDING_TEXT)).toBeInTheDocument();
    expect(within(dialog).queryByText(SOURCE_WITHHELD_TEXT)).toBeNull();
    for (const name of ["Approve", "Reopen for review", "Edit"]) {
      expect(within(dialog).queryByRole("button", { name })).toBeNull();
    }
    expect(emptiedCopy(left)).toBe(false);
    expect(emptiedCopy({ ...left, title: WITHHELD_COPY_TITLE, body: "", tags: [] })).toBe(true);
  });

  it("never shows message_source_key as a chip", async () => {
    renderAt("/brain?item=5", admin, {
      "GET /api/brain/items/5": { item: item({ origin: "promoted", flags: { message_id: 102, archive_session_id: 11, message_source_key: "jarvis:message:abc:1" } }), versions },
    });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    expect(dialog.querySelector(".br-chips")).toBeNull();
    expect(dialog.textContent).not.toMatch(/message source key|jarvis:message:abc:1/);
    expect(within(dialog).getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("offers restore and a typed purge for a deleted item", async () => {
    const deleted = item({ deleted_at: now });
    const calls = renderAt("/brain?item=5", admin, {
      "GET /api/brain/items/5": { item: deleted, versions },
      "DELETE /api/brain/items/5/purge": { json: {} },
    });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    expect(within(dialog).getByRole("button", { name: "Restore" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Approve" })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Purge for good" }));
    const confirm = await screen.findByRole("dialog", { name: "Purge item #5" });
    const button = within(confirm).getByRole("button", { name: "Purge for good" });
    fireEvent.change(within(confirm).getByRole("textbox"), { target: { value: "6" } });
    expect(button).toBeDisabled();
    fireEvent.change(within(confirm).getByRole("textbox"), { target: { value: "5" } });
    fireEvent.click(button);
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/brain/items/5/purge")).toBe(true));
    expect(await screen.findByText(/was purged/)).toBeInTheDocument();
  });

  it("locks review for operators when an item needs an admin, and offers no admin actions or redaction", async () => {
    renderAt("/brain?item=5", operator, { "GET /api/brain/items/5": { item: item({ kind: "instruction", kind_label: "Operating instruction (reference)", needs_admin: true }), versions } });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    expect(within(dialog).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Edit" })).toBeDisabled();
    expect(within(dialog).getByText(/Needs an admin: operating instructions, skills and workflow references are approved by an admin/)).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: "Versions" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Redact/ })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Delete" })).toBeNull();
  });

  it("lets operators edit a plain item with a note", async () => {
    const calls = renderAt("/brain?item=5", operator, {
      "GET /api/brain/items/5": { item: item(), versions },
      "PATCH /api/brain/items/5": { json: { item: item({ title: "Refund policy (2026)", version: 3 }) } },
    });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Edit" }));
    fireEvent.change(within(dialog).getByLabelText("Title"), { target: { value: "Refund policy (2026)" } });
    fireEvent.change(within(dialog).getByLabelText("Note for the version history"), { target: { value: "year" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save as a new version" }));
    await waitFor(() => expect(calls.find((c) => c.method === "PATCH")?.body).toEqual({ title: "Refund policy (2026)", note: "year" }));
  });

  it("offers operators no admin-only kind when editing, and admins every kind", async () => {
    const kindsIn = (dialog: HTMLElement) => Array.from((within(dialog).getByLabelText("Kind") as HTMLSelectElement).options).map((o) => o.value);
    renderAt("/brain?item=5", operator, { "GET /api/brain/items/5": { item: item(), versions } });
    let dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Edit" }));
    expect(kindsIn(dialog)).not.toContain("instruction");
    expect(kindsIn(dialog)).not.toContain("skill");
    expect(kindsIn(dialog)).not.toContain("workflow");
    expect(kindsIn(dialog)).toContain("business");
    expect(within(dialog).getByText(/kinds only an admin can give an item/)).toBeInTheDocument();
    cleanup();

    renderAt("/brain?item=5", admin, { "GET /api/brain/items/5": { item: item(), versions } });
    dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Edit" }));
    expect(kindsIn(dialog)).toEqual(expect.arrayContaining(["instruction", "skill", "workflow", "business"]));
  });

  it("says so in a dialog that can be closed when an answer has a shape it does not understand", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderAt("/brain?tab=search&item=5", operator, { "GET /api/brain/items/5": { item: item(), versions: "not a list" } });
    const dialog = await screen.findByRole("dialog", { name: "Could not be shown" });
    expect(within(dialog).getByRole("alert")).toHaveTextContent(/This memory item could not be shown: the API answered in a form this dashboard does not understand/);
    // The page itself is still there.
    expect(screen.getByRole("heading", { level: 1, name: "JARVIS Brain" })).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.location.search).toBe("?tab=search");
  });

  it("shows viewers the text and source only", async () => {
    renderAt("/brain?item=5", viewer, { "GET /api/brain/items/5": { item: item({ status: "approved" }) } });
    const dialog = await screen.findByRole("dialog", { name: "Refund policy" });
    expect(within(dialog).getByText("Refunds within 30 days.")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: "Edit" })).toBeNull();
    expect(within(dialog).queryByRole("heading", { name: "Versions" })).toBeNull();
    expect(within(dialog).queryByRole("link", { name: /Run #7/ })).toBeNull();
  });
});

describe("JARVIS Brain: agent knowledge and skills", () => {
  it("shows what each agent reads, where it uses memory, and counts only when the API returns them", async () => {
    renderAt("/brain?tab=agents", viewer, { "GET /api/brain/agents": { agents } });
    const supervisor = (await screen.findByRole("heading", { name: "AI Supervisor", level: 2 })).closest(".card") as HTMLElement;
    expect(within(supervisor).getByText("answers about JARVIS's history")).toBeInTheDocument();
    expect(within(supervisor).getByText(/Conversation archive: every imported conversation except withheld ones/)).toBeInTheDocument();
    expect(supervisor).toHaveTextContent("30 approved items it reads (of those visible to you)");
    const cr = screen.getByRole("heading", { name: "Credit Repair & Dispute Management" }).closest(".card") as HTMLElement;
    expect(within(cr).getByText("Counts are not shown to you.")).toBeInTheDocument();
    expect(within(cr).getByText("Not used: nothing this agent does reads memory today.")).toBeInTheDocument();
  });

  it("maps each JARVIS skill to its capabilities and compatibility", async () => {
    renderAt("/brain?tab=skills", operator, { "GET /api/brain/skills": { skills: [skill] } });
    expect(await screen.findByRole("button", { name: "roas-report" })).toBeInTheDocument();
    expect(screen.getByText("ads_report")).toBeInTheDocument();
    expect(screen.getByText("Mapped")).toBeInTheDocument();
    expect(screen.getByText("Bobby")).toBeInTheDocument();
    expect(screen.getByText("Sample data until a Meta Ads integration is connected.")).toBeInTheDocument();
  });

  it("tells viewers why they see no skills", async () => {
    renderAt("/brain?tab=skills", viewer, { "GET /api/brain/skills": { skills: [] } });
    expect(await screen.findByText(/Skill references are reviewed by operators and admins/)).toBeInTheDocument();
  });
});

describe("JARVIS Brain: workflow drafts", () => {
  it("lists drafts with no control that could enable anything", async () => {
    renderAt("/brain?tab=workflows", admin, { "GET /api/brain/workflows": { drafts: [draft(), draft({ id: 4, name: "triage-inbox", compatibility: "reference_only", capability: "support_overview", status: "draft", enabled_in_source: false })] } });
    expect(await screen.findByRole("button", { name: "roas-report" })).toBeInTheDocument();
    expect(screen.getByText("2 drafts")).toBeInTheDocument();
    expect(within(page()).queryByRole("switch")).toBeNull();
    expect(within(page()).queryByRole("checkbox")).toBeNull();
    expect(within(page()).queryByRole("button", { name: /enable|resume|run/i })).toBeNull();
    expect(screen.getAllByText("never runs")).toHaveLength(2);
  });

  it("converts a reviewed draft only after the admin confirms the JARVIS job is off, into a disabled schedule", async () => {
    const calls = renderAt("/brain?tab=workflows&draft=3", admin, {
      "GET /api/brain/workflows": { drafts: [draft()] },
      "GET /api/brain/workflows/3": { draft: draft(), schedule_exists: false },
      "POST /api/brain/workflows/3/convert": { json: { draft: draft({ status: "converted", schedule_id: 12 }), schedule: { id: 12, name: "jarvis-roas-report", enabled: false } } },
    });
    const dialog = await screen.findByRole("dialog", { name: "JARVIS job roas-report" });
    expect(within(dialog).getByText(/JARVIS ran cron in the PC's local time \(America\/New_York\); Command Center schedules run in UTC/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Weekdays at 08:00 UTC/)).toBeInTheDocument();
    expect(within(dialog).getByText(/From the JARVIS backup/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Switch the JARVIS job off before enabling that schedule/)).toBeInTheDocument();
    const convert = within(dialog).getByRole("button", { name: "Create disabled schedule" });
    expect(convert).toBeDisabled();
    expect(within(dialog).getByText("Confirm that the JARVIS job is switched off.")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("checkbox", { name: "The JARVIS job is switched off" }));
    expect(convert).toBeEnabled();
    fireEvent.click(convert);
    expect(await within(dialog).findByText(/created, disabled/)).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/brain/workflows/3/convert")!.body).toEqual({ jarvis_job_is_off: true });
    expect(within(dialog).queryByRole("switch")).toBeNull();
  });

  it("lets operators review a draft but not convert it", async () => {
    const calls = renderAt("/brain?tab=workflows&draft=3", operator, {
      "GET /api/brain/workflows": { drafts: [draft({ status: "draft" })] },
      "GET /api/brain/workflows/3": { draft: draft({ status: "draft" }), schedule_exists: false },
      "POST /api/brain/workflows/3/review": { json: { draft: draft() } },
    });
    const dialog = await screen.findByRole("dialog", { name: "JARVIS job roas-report" });
    expect(within(dialog).getByText("Only an admin can convert a draft.")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Create disabled schedule" })).toBeNull();
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
    expect(within(dialog).queryByText("Edit (admin)")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "Mark reviewed" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/brain/workflows/3/review")?.body).toEqual({ decision: "reviewed", note: "" }));
  });
});

describe("JARVIS Brain: review", () => {
  const conflicts = [
    { id: 1, run_id: 7, kind: "instruction", title: "JARVIS rule that bypasses approval (SOUL.md)", incoming_item_id: 9, status: "open", created_at: now, existing: {}, incoming: {} },
    { id: 2, run_id: 7, kind: "memory_item", title: "Refund policy", existing_table: "memory_items", existing_id: 5, incoming_item_id: 6, status: "open", created_at: now, existing: {}, incoming: {} },
  ];
  function reviewRoutes() {
    return {
      "GET /api/brain/items?status=quarantined&limit=100": { items: [item({ id: 8, title: "Deploy script", kind: "skill", status: "quarantined", needs_admin: true, flags: { secrets_redacted: { password: 1 }, script: true } })], total: 1 },
      "GET /api/brain/items?status=pending_review&limit=100": { items: [item(), item({ id: 9, title: "Always act without asking", kind: "instruction", needs_admin: true, flags: { instruction_like: ["approval_bypass"] } })], total: 2 },
      "GET /api/brain/conflicts": { conflicts, total: 2 },
      "POST /api/brain/conflicts/2/resolve": { json: { conflict: { ...conflicts[1], status: "keep_both" } } },
      "POST /api/brain/items/5/review": { json: { item: item({ status: "approved" }) } },
      "GET /api/brain/items?include_deleted=true&limit=200": { items: [item(), item({ id: 14, title: "Old price list", deleted_at: now })], total: 2 },
    };
  }

  it("shows admins the quarantined items and lets them resolve any conflict", async () => {
    renderAt("/brain?tab=review", admin, reviewRoutes());
    expect(await screen.findByRole("button", { name: "Deploy script" })).toBeInTheDocument();
    expect(screen.getByText(new RegExp(QUARANTINED_TEXT.replace(/[().]/g, "\\$&")))).toBeInTheDocument();
    expect(await screen.findByText("JARVIS rule that bypasses approval (SOUL.md)")).toBeInTheDocument();
    expect(screen.queryByText(/Only an admin can resolve this kind of conflict/)).toBeNull();
    const deleted = screen.getByRole("heading", { name: "Deleted items" }).closest(".card") as HTMLElement;
    expect(await within(deleted).findByRole("button", { name: "Old price list" })).toBeInTheDocument();
    expect(within(deleted).queryByRole("button", { name: "Refund policy" })).toBeNull();
    for (const radio of within(screen.getByRole("group", { name: "Resolution for conflict 1" })).getAllByRole("radio")) expect(radio).toBeEnabled();
  });

  it("hides quarantined items from operators, locks admin-only items and conflicts with the reason, and resolves the rest", async () => {
    const calls = renderAt("/brain?tab=review", operator, reviewRoutes());
    expect(await screen.findByRole("button", { name: "Refund policy" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deploy script" })).toBeNull();
    expect(calls.some((c) => c.path.includes("status=quarantined") || c.path.includes("include_deleted"))).toBe(false);
    expect(screen.queryByRole("heading", { name: "Deleted items" })).toBeNull();
    expect(screen.getByText(/are reviewed by admins and are not listed for you/)).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Approve Always act without asking" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Approve Refund policy" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/brain/items/5/review")?.body).toEqual({ decision: "approve", note: "" }));

    const locked = (await screen.findByText("JARVIS rule that bypasses approval (SOUL.md)")).closest("li") as HTMLElement;
    expect(within(locked).getByText(/Only an admin can resolve this kind of conflict/)).toBeInTheDocument();
    for (const radio of within(locked).getAllByRole("radio")) expect(radio).toBeDisabled();
    expect(within(locked).getByRole("button", { name: "Resolve" })).toBeDisabled();
    expect(within(locked).getByText(/It never becomes a rule: approvals stay with the Command Center/)).toBeInTheDocument();

    const open = screen.getAllByText("Refund policy", { selector: "strong" })[0].closest("li") as HTMLElement;
    expect(within(open).getByText("Archive the existing item; the incoming item stays pending for review.")).toBeInTheDocument();
    fireEvent.click(within(open).getByRole("radio", { name: /Keep both/ }));
    fireEvent.click(within(open).getByRole("button", { name: "Resolve" }));
    await waitFor(() => expect(calls.find((c) => c.path === "/api/brain/conflicts/2/resolve")?.body).toEqual({ resolution: "keep_both", note: "" }));
  });

  it("never offers to approve an item emptied because its source message was withheld (0.6.1)", async () => {
    const emptied = item({
      id: 21, title: "Withheld: the archived message it was promoted from was withheld", body: "", tags: [], status: "quarantined", origin: "promoted",
      needs_admin: true, flags: { message_id: 102, archive_session_id: 11, message_source_key: "jarvis:message:abc:1", source_withheld: true },
    });
    const calls = renderAt("/brain?tab=review", admin, { ...reviewRoutes(), "GET /api/brain/items?status=quarantined&limit=100": { items: [emptied], total: 1 } });
    const approve = await screen.findByRole("button", { name: `Approve ${emptied.title}` });
    expect(approve).toBeDisabled();
    expect(approve).toHaveAttribute("title", SOURCE_WITHHELD_TEXT);
    const row = approve.closest("li") as HTMLElement;
    expect(within(row).getByText("source message withheld")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: `Reject ${emptied.title}` })).toBeEnabled();
    fireEvent.click(approve);
    expect(calls.some((c) => c.method === "POST" && c.path === "/api/brain/items/21/review")).toBe(false);
  });
});

describe("JARVIS Brain: conflicts that need an admin for one side", () => {
  it("offers operators only keep both and dismiss, and says why", async () => {
    const skill = { id: 3, run_id: 7, kind: "memory_item", title: "Keyword research", existing_table: "memory_items", existing_id: 11, incoming_item_id: 12, status: "open", created_at: now, existing: {}, incoming: {}, needs_admin: true };
    renderAt("/brain?tab=review", operator, {
      "GET /api/brain/items?status=pending_review&limit=100": { items: [], total: 0 },
      "GET /api/brain/conflicts": { conflicts: [skill], total: 1 },
    });
    const card = (await screen.findByText("Keyword research", { selector: "strong" })).closest("li") as HTMLElement;
    expect(within(card).getByText(/an admin decides those/)).toBeInTheDocument();
    expect(within(card).getByRole("radio", { name: /Keep existing/ })).toBeDisabled();
    expect(within(card).getByRole("radio", { name: /Use incoming/ })).toBeDisabled();
    expect(within(card).getByRole("radio", { name: /Keep both/ })).toBeEnabled();
    expect(within(card).getByRole("radio", { name: /Dismiss/ })).toBeEnabled();
  });
});

describe("JARVIS Brain: import history", () => {
  const report = {
    totals: { sessions: 5, messages: 133, files: 20, jobs: 5, urls: 2 },
    by_action: { imported: 120, withheld: 4, excluded: 9 },
    by_reason: { "rewound or superseded": 3, empty: 6 },
    secrets: { types: { anthropic_api_key: 2, slack_token: 1 }, where: { messages: 2, files: 1, drafts: 0 } },
    restricted: { types: { ssn: 1 }, sessions_withheld: 1, files_excluded: 0 },
    masked: { email_address: 5, phone_number: 2 },
    verification: { ok: true, checks: 8 },
  };
  const detail = (over: Record<string, unknown> = {}) => ({
    run: { ...run7, report }, records_by_action: { imported: 120, withheld: 4, excluded: 9 }, records_by_type: { message: 100, file: 20 }, can_roll_back: true, reviewed_items: 3, ...over,
  });

  it("lists runs and shows a run's counts, verification and credential rotation list (types, never values)", async () => {
    renderAt("/brain?tab=imports", operator, { "GET /api/brain/imports": { runs: [run7] }, "GET /api/brain/imports/7": detail() });
    fireEvent.click(await screen.findByRole("button", { name: "Run #7" }));
    const dialog = await screen.findByRole("dialog", { name: "Import run #7" });
    expect(await within(dialog).findByRole("heading", { name: "Rotate these kinds of credential" })).toBeInTheDocument();
    expect(within(dialog).getByText("anthropic api key")).toBeInTheDocument();
    expect(within(dialog).getByText("found 2 times")).toBeInTheDocument();
    expect(within(dialog).getByText("slack token")).toBeInTheDocument();
    expect(within(dialog).getByText("Passed")).toBeInTheDocument();
    expect(within(dialog).getAllByText("imported").length).toBeGreaterThan(0);
    // Operators see the history but cannot roll back.
    expect(within(dialog).queryByRole("button", { name: /Roll back/ })).toBeNull();
  });

  it("rolls back only after the admin types the run number, with the discard option and the API's own errors", async () => {
    let reply: { status?: number; json: unknown } = { status: 400, json: { detail: "confirm: type the run number" } };
    const calls = renderAt("/brain?tab=imports&run=7", admin, {
      "GET /api/brain/imports": { runs: [run7] },
      "GET /api/brain/imports/7": detail(),
      "POST /api/brain/imports/7/rollback": () => reply,
    });
    const dialog = await screen.findByRole("dialog", { name: "Import run #7" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "Roll back run #7…" }));
    const confirm = await screen.findByRole("dialog", { name: "Roll back import run #7" });
    expect(within(confirm).getByText(/3 items/)).toBeInTheDocument();
    const go = within(confirm).getByRole("button", { name: "Roll back run #7" });
    const typed = within(confirm).getByLabelText(/Type the run number/);
    fireEvent.change(typed, { target: { value: "8" } });
    expect(go).toBeDisabled();
    fireEvent.change(typed, { target: { value: "#7" } });
    expect(go).toBeDisabled();
    fireEvent.change(typed, { target: { value: "7" } });
    expect(go).toBeEnabled();

    fireEvent.click(go);
    expect(await within(confirm).findByText("The API did not accept the confirmation: type the run number.")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "/api/brain/imports/7/rollback")!.body).toEqual({ confirm: "7", discard_reviewed: false });

    reply = { status: 400, json: { detail: "3 items of run 7 were reviewed or edited since: roll back with discard_reviewed" } };
    fireEvent.click(go);
    expect(await within(confirm).findByText("Not rolled back: 3 items of run 7 were reviewed or edited since: roll back with discard_reviewed")).toBeInTheDocument();

    reply = { json: { run: { ...run7, status: "rolled_back" } } };
    fireEvent.click(within(confirm).getByRole("checkbox", { name: "Discard reviewed items" }));
    fireEvent.click(go);
    expect(await within(dialog).findByText("Run #7 was rolled back.")).toBeInTheDocument();
    const posts = calls.filter((c) => c.path === "/api/brain/imports/7/rollback");
    expect(posts.at(-1)!.body).toEqual({ confirm: "7", discard_reviewed: true });
  });

  it("loads the run again when a rollback is refused, so items reviewed meanwhile can be discarded", async () => {
    let loads = 0;
    renderAt("/brain?tab=imports&run=7", admin, {
      "GET /api/brain/imports": { runs: [run7] },
      "GET /api/brain/imports/7": () => ({ json: detail({ reviewed_items: loads++ === 0 ? 0 : 2 }) }),
      "POST /api/brain/imports/7/rollback": { status: 400, json: { detail: "2 items of run 7 were reviewed or edited since: roll back with discard_reviewed" } },
    });
    const dialog = await screen.findByRole("dialog", { name: "Import run #7" });
    fireEvent.click(await within(dialog).findByRole("button", { name: "Roll back run #7…" }));
    const confirm = await screen.findByRole("dialog", { name: "Roll back import run #7" });
    expect(within(confirm).getByText("No item from this run has been reviewed or edited since.")).toBeInTheDocument();
    expect(within(confirm).queryByRole("checkbox")).toBeNull();
    fireEvent.change(within(confirm).getByLabelText(/Type the run number/), { target: { value: "7" } });
    fireEvent.click(within(confirm).getByRole("button", { name: "Roll back run #7" }));
    expect(await within(confirm).findByText(/^Not rolled back: 2 items of run 7/)).toBeInTheDocument();
    expect(await within(confirm).findByRole("checkbox", { name: "Discard reviewed items" })).toBeInTheDocument();
    expect(within(confirm).getByText("2 items")).toBeInTheDocument();
  });

  it("never says 'no secrets' for a run that did not finish, and lists what it found so far", async () => {
    const stopped = { ...run7, status: "failed", report: {}, checkpoint: { phase: "sessions", index: 3, tally: { secrets: { types: { anthropic_api_key: 4 } } } } };
    renderAt("/brain?tab=imports&run=7", admin, { "GET /api/brain/imports": { runs: [stopped] }, "GET /api/brain/imports/7": detail({ run: stopped }) });
    const dialog = await screen.findByRole("dialog", { name: "Import run #7" });
    expect(await within(dialog).findByText(/this list is partial/)).toBeInTheDocument();
    expect(within(dialog).getByText("anthropic api key")).toBeInTheDocument();
    expect(within(dialog).queryByText(/No secrets were found/)).toBeNull();
  });

  it("says there is no list yet when a run stopped before finding anything", async () => {
    const stopped = { ...run7, status: "interrupted", report: {}, checkpoint: { phase: "files", index: 0 } };
    renderAt("/brain?tab=imports&run=7", admin, { "GET /api/brain/imports": { runs: [stopped] }, "GET /api/brain/imports/7": detail({ run: stopped }) });
    const dialog = await screen.findByRole("dialog", { name: "Import run #7" });
    expect(await within(dialog).findByText(/No list yet: the run did not finish/)).toBeInTheDocument();
    expect(within(dialog).queryByText(/No secrets were found/)).toBeNull();
  });

  it("explains when a run cannot be rolled back", async () => {
    renderAt("/brain?tab=imports&run=7", admin, { "GET /api/brain/imports": { runs: [run7] }, "GET /api/brain/imports/7": detail({ can_roll_back: false }) });
    const dialog = await screen.findByRole("dialog", { name: "Import run #7" });
    expect(await within(dialog).findByText(/cannot be rolled back now/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /Roll back run/ })).toBeNull();
  });

  it("says when nothing was imported", async () => {
    renderAt("/brain?tab=imports", admin, { "GET /api/brain/imports": { runs: [] } });
    expect(await screen.findByText("No JARVIS import yet.")).toBeInTheDocument();
  });
});

describe("Task view: reference notes from memory", () => {
  const task = (data: Record<string, unknown>) => ({
    task: {
      id: 5, created_at: now, updated_at: now, request_id: null, schedule_id: null, agent: "seo_content", capability: "keyword_brief", params: { topic: "wp" }, risk: "read",
      status: "succeeded", attempts: 1, started_at: now, finished_at: now, source: "dashboard", created_by: "admin@test.local", summary: "Brief written", result: { summary: "ok", data }, error: "",
    },
    logs: [],
    approval: null,
  });

  it("lists the notes with links into the JARVIS Brain", async () => {
    renderAt("/tasks?id=5", admin, { "GET /api/tasks": { tasks: [], total: 0 }, "GET /api/tasks/5": task({ memory_notes: [{ id: 31, kind: "agent_note", title: "Target keywords" }] }) });
    const dialog = await screen.findByRole("dialog", { name: "Task #5" });
    expect(await within(dialog).findByRole("heading", { name: "Reference notes from memory" })).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Target keywords" })).toHaveAttribute("href", "/brain?item=31");
    expect(within(dialog).getByText("· agent note")).toBeInTheDocument();
  });

  it("shows nothing when the task has no notes (or the API removed them)", async () => {
    renderAt("/tasks?id=5", viewer, { "GET /api/tasks": { tasks: [], total: 0 }, "GET /api/tasks/5": task({}) });
    const dialog = await screen.findByRole("dialog", { name: "Task #5" });
    await within(dialog).findByText("Brief written");
    expect(within(dialog).queryByText("Reference notes from memory")).toBeNull();
  });
});
