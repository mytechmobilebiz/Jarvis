// JARVIS Brain 0.6.2 option C (rebuild R6, docs/JARVIS_BRAIN_SPEC.md section 5): the withhold dialog's group, the holds
// panel's uncertain group and group view, the D3 states (later_withhold, other_not_compared, not_checked, cut) and the
// uncertain link in words, the per-row release that needs the typed word unless the server says one click may release
// it (fail closed), the per-conversation one-click release only when the server allows it, and the option C counters.
// Mocked API data only; never real text.
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import {
  canReleaseUnrelated,
  carrierIntro,
  finishDecisionsText,
  GROUP_TITLE_TEXT,
  groupPreviewText,
  holdGroupKey,
  holdStateNotes,
  LATER_WITHHOLD_TEXT,
  NOT_CHECKED_TEXT,
  OTHER_NOT_COMPARED_TEXT,
  releaseNeedsTyping,
  titlesText,
  typedReasonText,
  UNCERTAIN_TEXT,
  unrelatedBlockedText,
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
const overview = {
  memory: { total: 0, by_status: {}, by_kind: {}, by_agent: {} },
  archive: { sessions: 1, messages: 3, withheld_messages: 0, first_at: null, last_at: null, by_staff: {} },
  review: { pending_items: 0, quarantined_items: 0, open_conflicts: 0 },
  workflows: { drafts: 0, by_status: {} },
  imports: { runs: 0, latest: null },
  ai_context: "off",
  instance_label: "",
};

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.pushState({}, "", "/");
});

function renderAt(path: string, over: Record<string, unknown> = {}) {
  window.history.pushState({}, "", path);
  const calls = mockFetch({ "GET /api/auth/me": { user: admin }, "GET /api/system/status": status, "GET /api/brain/overview": overview, ...over });
  render(<App />);
  return calls;
}

// ---------------------------------------------------------------------------------------------------------------------
// The holds panel.

const base = {
  session: 31, title: "Deal call", role: "assistant", at: "2026-03-01T08:05:00Z", content_chars: 120, state: "live", rule: "held with its conversation: a withheld conversation group",
  withheld_message: { id: 700, position: 1, role: "assistant", at: "2026-03-01T08:01:00Z", content_chars: 118 }, neighbours: [], confirm_required: false,
  preselected: false, decided_on: "c83ee64b358a", frozen: false, form: "plain", later_withhold: false, uncertain: false, joined: "continuation", joined_state: "member",
};
const uncertainHold = {
  ...base, message: 501, session: 32, title: null, position: 2, rule: "held with its conversation: an uncertain link to a withheld conversation", group: "uncertain",
  relations: ["same_time", "uncertain"], compare: "other text", evidence: "the same time and role", confirm_required: true, one_click: false, uncertain: true,
  joined: "child", joined_state: "uncertain",
};
const laterNone = { ...base, message: 601, position: 10, rule: "new in a later backup", group: "later", relations: ["none"], compare: "other text",
  evidence: "no relation to the withheld text was found", one_click: true };
const laterStale = { ...base, message: 602, position: 11, rule: "new in a later backup", group: "later", relations: ["later_withhold"], compare: "other text",
  evidence: "a later withhold: not compared with its withheld text yet", one_click: false, later_withhold: true };
const otherNotCompared = { ...base, message: 603, session: 33, title: "Venue list", position: 3, group: "other", relations: ["other_not_compared"],
  compare: "other text", evidence: "not compared with every other withheld text", one_click: false, joined: "reset" };
const noneButTool = { ...base, message: 604, session: 33, title: "Venue list", position: 4, role: "tool", group: "other", relations: ["none"], compare: "other text",
  evidence: "no relation to the withheld text was found", one_click: false, form: "tool", joined: "reset" };
const futureGroup = { ...base, message: 605, session: 34, title: "Elsewhere", position: 0, role: "user", group: "carried_elsewhere", relations: ["contains"],
  compare: "contains the withheld text", evidence: "contains the withheld text", confirm_required: true, one_click: false, joined: "", joined_state: "",
  withheld_message: { id: null, position: null, role: "user", at: null, content_chars: null } };

function holdRoutes(holds: unknown[], over: Record<string, unknown> = {}) {
  return {
    "GET /api/brain/items?status=quarantined&limit=100": { items: [], total: 0 },
    "GET /api/brain/items?status=pending_review&limit=100": { items: [], total: 0 },
    "GET /api/brain/conflicts": { conflicts: [], total: 0 },
    "GET /api/brain/items?include_deleted=true&limit=200": { items: [], total: 0 },
    "GET /api/brain/holds": { holds, total: holds.length },
    "POST /api/brain/holds/501/release": { json: { released: true } },
    "POST /api/brain/holds/601/release": { json: { released: true } },
    "POST /api/brain/holds/604/release": { json: { released: true } },
    ...over,
  };
}
const card = async () => (await screen.findByRole("heading", { name: "Held for review" })).closest(".card") as HTMLElement;

describe("JARVIS Brain option C: the holds panel (rebuild R6)", () => {
  it("shows an uncertain hold in its own group, never one-click, with what an uncertain link means (O-D1)", async () => {
    renderAt("/brain?tab=review", holdRoutes([uncertainHold]));
    const c = await card();
    const group = await within(c).findByRole("region", { name: "Uncertain link to a withheld conversation" });
    expect(within(group).getByRole("button", { name: "Message #2 (Assistant) in a conversation whose title is hidden" })).toBeInTheDocument();
    expect(within(group).getByText("uncertain link to a withheld conversation", { selector: ".br-chip" })).toBeInTheDocument();
    expect(within(group).getByText(/^Its conversation is in a withheld message's group by a link that is not certain/).textContent).toBe(UNCERTAIN_TEXT);
    expect(within(group).getByText("release needs RELEASE")).toBeInTheDocument();
    expect(within(group).queryByRole("button", { name: "Release this conversation's rows with no relation" })).toBeNull();
    expect(within(group).getByText(/this conversation's link to a withheld conversation is uncertain/)).toBeInTheDocument();
  });

  it("never drops a hold whose group the dashboard does not know: it is listed under Other held messages", async () => {
    renderAt("/brain?tab=review", holdRoutes([futureGroup]));
    const c = await card();
    const other = await within(c).findByRole("region", { name: "Other held messages" });
    expect(within(other).getByRole("button", { name: "Message #0 (User) in Elsewhere" })).toBeInTheDocument();
  });

  it("asks for the typed word for a 'none' hold the server does not call one-click (a tool row), and sends it (B6)", async () => {
    const calls = renderAt("/brain?tab=review", holdRoutes([noneButTool]));
    const c = await card();
    fireEvent.click(await within(c).findByRole("button", { name: "Release message #4 (Tool result)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #4" });
    const go = within(confirm).getByRole("button", { name: "Release" });
    expect(go).toBeDisabled();
    fireEvent.change(within(confirm).getByRole("textbox"), { target: { value: "RELEASE" } });
    fireEvent.click(go);
    await waitFor(() => expect(calls.find((x) => x.method === "POST" && x.path === "/api/brain/holds/604/release")?.body).toEqual({ confirm: "RELEASE" }));
    expect(within(c).getByText("a tool row", { selector: ".br-chip" })).toBeInTheDocument();
  });

  it("says why the typed word is needed for a 'none' row one click may not release", async () => {
    renderAt("/brain?tab=review", holdRoutes([noneButTool]));
    const c = await card();
    fireEvent.click(await within(c).findByRole("button", { name: "Release message #4 (Tool result)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #4" });
    expect(within(confirm).getByText(/^It is not a row one click may release \(only a plain user or assistant text/)).toBeInTheDocument();
    expect(within(confirm).getByText(/^It is not a row/).textContent).toBe(typedReasonText([noneButTool]));
  });

  it("asks for the typed word when the server sends no one-click verdict at all (an older server), fail closed", async () => {
    const { one_click: _drop, ...noVerdict } = laterNone;
    renderAt("/brain?tab=review", holdRoutes([noVerdict]));
    const c = await card();
    fireEvent.click(await within(c).findByRole("button", { name: "Release message #10 (Assistant)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #10" });
    expect(within(confirm).getByRole("textbox")).toBeInTheDocument();
    expect(within(c).queryByRole("button", { name: "Release this conversation's rows with no relation" })).toBeNull();
  });

  it("releases with a plain confirmation only a hold the server says one click may release (control)", async () => {
    const calls = renderAt("/brain?tab=review", holdRoutes([laterNone]));
    const c = await card();
    fireEvent.click(await within(c).findByRole("button", { name: "Release message #10 (Assistant)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #10" });
    expect(within(confirm).queryByRole("textbox")).toBeNull();
    fireEvent.click(within(confirm).getByRole("button", { name: "Release" }));
    await waitFor(() => expect(calls.find((x) => x.method === "POST" && x.path === "/api/brain/holds/601/release")?.body).toEqual({}));
  });

  it("marks each hold as one-click or needing the typed word", async () => {
    renderAt("/brain?tab=review", holdRoutes([laterNone, laterStale]));
    const c = await card();
    expect(await within(c).findByText("one-click release")).toBeInTheDocument();
    expect(within(c).getByText("release needs RELEASE")).toBeInTheDocument();
  });

  it("says a later withhold made a hold's 'none' stale, and offers no one-click release for its conversation (N-A, D3)", async () => {
    renderAt("/brain?tab=review", holdRoutes([laterNone, laterStale]));
    const c = await card();
    const later = await within(c).findByRole("region", { name: "New in a later backup" });
    expect(within(later).queryByRole("button", { name: "Release this conversation's rows with no relation" })).toBeNull();
    expect(within(later).getByText(/some rows were not compared with every withheld text/)).toBeInTheDocument();
    expect(within(later).getByText(/^A message was withheld after this row was compared/).textContent).toBe(LATER_WITHHOLD_TEXT);
    expect(within(later).getByText("a later withhold: not compared with its text yet", { selector: ".br-chip" })).toBeInTheDocument();
  });

  it("says a hold was not compared with every other withheld text (other_not_compared), and that release needs the typed word", async () => {
    renderAt("/brain?tab=review", holdRoutes([otherNotCompared]));
    const c = await card();
    const other = await within(c).findByRole("region", { name: "Other held messages" });
    expect(within(other).getByText(/^It was compared with its own withheld text, but not with every other withheld text/).textContent).toBe(OTHER_NOT_COMPARED_TEXT);
    expect(within(other).getByText("not compared with every other withheld text", { selector: ".br-chip" })).toBeInTheDocument();
    fireEvent.click(within(other).getByRole("button", { name: "Release message #3 (Assistant)" }));
    const confirm = await screen.findByRole("dialog", { name: "Release message #3" });
    expect(within(confirm).getByRole("textbox")).toBeInTheDocument();
    expect(within(confirm).getByText(/not compared with every withheld text/)).toBeInTheDocument();
  });

  it("has a group view: by withheld message, then by conversation, with how each joined and the uncertain links counted", async () => {
    renderAt("/brain?tab=review", holdRoutes([uncertainHold, laterNone, laterStale, otherNotCompared, futureGroup]));
    const c = await card();
    fireEvent.click(await within(c).findByRole("button", { name: "By withheld message and conversation" }));
    const w = within(c).getByRole("region", { name: "Withheld message #1 (Assistant)" });
    expect(within(w).getByRole("heading", { level: 3 }).textContent).toBe("Withheld message #1 (Assistant) (4 rows in 3 conversations, 1 by an uncertain link)");
    const convs = Array.from(w.querySelectorAll(".br-hold-conversation")) as HTMLElement[];
    expect(convs.map((x) => x.querySelector("h4")?.textContent)).toEqual([
      "A conversation whose title is hidden (1 row)a child conversationuncertain link",
      "Deal call (2 rows)a continuation",
      "Venue list (1 row)a reset",
    ]);
    // the withheld messages the admin may not open share one bucket, never told apart
    const hidden = within(c).getByRole("region", { name: "A withheld message you may not open, or no longer stored" });
    expect(within(hidden).getByText("not in its group: held on its own", { selector: ".br-chip" })).toBeInTheDocument();
    expect(c.textContent).not.toMatch(/[0-9a-f]{64}/);
    fireEvent.click(within(c).getByRole("button", { name: "By how they were held" }));
    expect(within(c).getByRole("region", { name: "Uncertain link to a withheld conversation" })).toBeInTheDocument();
  });

  it("puts the rules in plain functions", () => {
    expect(holdGroupKey({ group: "uncertain" })).toBe("uncertain");
    expect(holdGroupKey({ group: "something new" })).toBe("other");
    expect(holdGroupKey({})).toBe("other");
    expect(releaseNeedsTyping({ relations: ["none"], one_click: true, uncertain: true })).toBe(true);
    expect(releaseNeedsTyping({ relations: ["none"], one_click: true, later_withhold: true })).toBe(true);
    expect(releaseNeedsTyping({ relations: ["none"], one_click: false })).toBe(true);
    for (const blocking of ["uncertain", "later_withhold", "other_not_compared", "not_checked", "not_in_backup"]) {
      expect(canReleaseUnrelated([{ relations: ["none"], one_click: true }, { relations: [blocking] }])).toBe(false);
    }
    expect(canReleaseUnrelated([{ relations: ["none"], one_click: true }, { relations: ["same_uid"], uncertain: true }])).toBe(false);
    expect(canReleaseUnrelated([{ relations: ["none"], one_click: true }])).toBe(true);
    expect(unrelatedBlockedText([{ relations: ["none"], one_click: true }])).toBeNull();
    expect(unrelatedBlockedText([{ relations: ["same_time"] }])).toMatch(/^No one-click release: no row here is a plain user or assistant text/);
    expect(holdStateNotes({ relations: ["not_checked", "cut"] })).toEqual([NOT_CHECKED_TEXT, expect.stringMatching(/^It holds the beginning of the withheld text/)]);
    expect(typedReasonText([{ relations: ["identical"] }])).toBe("This row is related to the withheld text: releasing can bring that text back.");
    expect(typedReasonText([{ relations: ["none"], uncertain: true }])).toMatch(/uncertain: it is never released in one click/);
  });
});

// ---------------------------------------------------------------------------------------------------------------------
// The counters.

describe("JARVIS Brain option C: the counters (rebuild R6)", () => {
  const decisions = {
    unsettled_decisions: 0, unreviewed_settle_holds: 0, later_backup_holds: 0, held_for_review: 7, frozen_conversations: 0, legacy_lock: false, tripwire: null,
    group_conversations: 4, uncertain_conversations: 1, group_rows_held: 6, group_rows_readable: 0, uncertain_waiting: 2,
  };

  it("shows the groups' counters and says the export waits on uncertain rows", async () => {
    renderAt("/brain", { "GET /api/brain/overview": { ...overview, decisions } });
    expect(await screen.findByText("Finish decisions needed.")).toBeInTheDocument();
    expect(screen.getByText(/2 rows of conversations whose link to a withheld conversation is uncertain wait for your review/)).toBeInTheDocument();
    const counters = (await screen.findByRole("heading", { name: "Withhold decisions" })).closest(".card") as HTMLElement;
    const value = (label: string) => within(counters).getByText(label).nextElementSibling?.textContent;
    expect(value("Conversations in withheld groups")).toBe("4");
    expect(value("Of them, by an uncertain link")).toBe("1");
    expect(value("Rows of uncertain conversations waiting (the export waits)")).toBe("2");
    expect(value("Rows held with their conversation, waiting for review")).toBe("6");
    expect(value("Rows of group conversations readable without a release")).toBe("0");
  });

  it("never shows a counter an older server does not send as 0, and warns when group rows are readable", async () => {
    const { uncertain_waiting: _u, group_conversations: _g, ...older } = decisions;
    renderAt("/brain", { "GET /api/brain/overview": { ...overview, decisions: { ...older, group_rows_readable: 3 } } });
    const counters = (await screen.findByRole("heading", { name: "Withhold decisions" })).closest(".card") as HTMLElement;
    expect(within(counters).getByText("Rows of uncertain conversations waiting (the export waits)").nextElementSibling?.textContent).toBe("not reported");
    expect(within(counters).getByText("Conversations in withheld groups").nextElementSibling?.textContent).toBe("not reported");
    const readable = within(counters).getByText("Rows of group conversations readable without a release").nextElementSibling as HTMLElement;
    expect(readable.textContent).toBe("3");
    expect(readable.className).toMatch(/br-count-bad/);
    expect(screen.getByText(/3 rows of conversations in withheld groups are readable without a release/)).toBeInTheDocument();
  });

  it("puts the banner lines in a plain function", () => {
    expect(finishDecisionsText({ unsettled_decisions: 0, unreviewed_settle_holds: 0, uncertain_waiting: 1 })).toEqual([
      '1 row of conversations whose link to a withheld conversation is uncertain waits for your review (Review tab, "Uncertain link"). The decisions file cannot be exported until each is kept or released.',
    ]);
    expect(finishDecisionsText({ unsettled_decisions: 0, unreviewed_settle_holds: 0, uncertain_waiting: 0, group_rows_readable: 0 })).toEqual([]);
  });
});

// ---------------------------------------------------------------------------------------------------------------------
// The withhold dialog.

describe("JARVIS Brain option C: the withhold dialog (rebuild R6)", () => {
  const sess = {
    id: 11, source_system: "jarvis", source_key: "jarvis:session:abc", source_session_id: "abc", title: "Deal call", staff: "jarvis", agent: "supervisor",
    origin: "", channel: "", lineage: "", parent_source_session_id: "", job_id: "", job_name: "", started_at: "2026-03-01T08:00:00Z", ended_at: null,
    end_reason: "", hermes_archived: false, source_message_count: 2, imported_messages: 2, withheld_messages: 0, tool_calls: 0, sensitivity: "general",
    labels: {}, summary: "", flags: {}, import_run_id: 7, created_at: now, updated_at: now,
  };
  const msg = {
    id: 101, session_id: 11, position: 0, role: "user", content: "What is the deal worth?", truncated: false, tool_name: "", tool_calls: [],
    at: "2026-03-01T08:00:00Z", state: "live", sensitivity: "general", withheld: false, labels: [], redactions: {}, flags: {},
  };
  const row = (id: number, position: number, session = 11) => ({ id, session, position, role: "assistant", at: "2026-03-01T08:00:00Z", state: "live", withheld: false });
  function preview(over: Record<string, unknown> = {}) {
    return {
      message: 101, same_message_rows: [], held_rows: [], identical_text_rows: [], rowless_copies: 0, items_with_this_text: [], first_user_message: true,
      decision_backup: { label: "state-2026-10-01", sha256: "c83ee64b358a" },
      carrier_rows: [{ ...row(160, 2, 40), relations: ["part_of"], evidence: "part of the withheld text" }],
      titles_with_this_text: 1,
      group: { conversations: 2, uncertain: 1, how: { child: 1, reset: 1 }, rows: 5 },
      ...over,
    };
  }
  function routes(over: Record<string, unknown> = {}) {
    return {
      "GET /api/brain/sessions": { sessions: [sess], total: 1 },
      "GET /api/brain/sessions/11": { session: sess, messages: [msg], total: 1, hidden: 0 },
      "GET /api/brain/messages/101/promoted": { message: 101, count: 0, items: [] },
      "GET /api/brain/messages/101/withhold-preview": preview(),
      ...over,
    };
  }
  async function openDialog() {
    const dialog = await screen.findByRole("dialog", { name: "Deal call" });
    await within(dialog).findByText("What is the deal worth?");
    fireEvent.click(within(dialog).getAllByRole("button", { name: "Withhold" })[0]);
    return screen.findByRole("dialog", { name: "Withhold message #0" });
  }

  it("says what the group holds with the message, the uncertain conversations, the carriers outside it and the hidden titles", async () => {
    const calls = renderAt("/brain?tab=conversations&session=11", routes());
    const confirm = await openDialog();
    const group = await within(confirm).findByLabelText("The withheld message's group");
    expect(group.textContent).toContain(
      "2 other conversations you may open are in this message's group (1 a child conversation, 1 a reset). Every stored row of this conversation and of those conversations (5 readable rows there) is held with it for your review.",
    );
    expect(group.textContent).toContain("1 conversation of the group is in it by an uncertain link (a parent not found, or two)");
    expect(within(confirm).getByText(/^1 stored row outside this message's group carries its text in another form/).textContent).toBe(carrierIntro(1));
    expect(within(confirm).getByText("Message #2 in conversation 40 (Assistant): part of it (a reply quote or the row's own text)")).toBeInTheDocument();
    expect(within(confirm).getByText(/^1 conversation title outside the group carries this text/).textContent).toBe(titlesText(1));
    expect(within(confirm).getByText(/^The title of this conversation and of every conversation in its group is hidden by membership/).textContent).toBe(GROUP_TITLE_TEXT);
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("still says the whole group is held when the server sends no group counts", async () => {
    renderAt("/brain?tab=conversations&session=11", routes({ "GET /api/brain/messages/101/withhold-preview": preview({ group: undefined, carrier_rows: undefined, titles_with_this_text: undefined }) }));
    const confirm = await openDialog();
    expect((await within(confirm).findByText(/^Every stored row of this conversation and of the conversations in its group/)).textContent).toBe(groupPreviewText(undefined)[0]);
  });

  it("puts the group in words", () => {
    expect(groupPreviewText({ conversations: 0, uncertain: 0, how: {}, rows: 0 })).toEqual([
      "No other conversation you may open is in this message's group. Every other stored row of this conversation is held with it for your review.",
    ]);
    expect(groupPreviewText({ conversations: 1, uncertain: 0, how: { delegated: 1 }, rows: 1 })[0]).toBe(
      "1 other conversation you may open is in this message's group (1 a delegated sub-agent). Every stored row of this conversation and of that conversation (1 readable row there) is held with it for your review.",
    );
  });
});
