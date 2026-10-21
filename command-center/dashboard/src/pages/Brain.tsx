// The JARVIS Brain (0.6.0; docs/JARVIS_BRAIN_SPEC.md section 5): what the Command Center keeps from JARVIS, the owner's previous
// assistant. Its knowledge, skills, scheduled jobs and conversations are imported as reference data: nothing here approves,
// runs or schedules anything. The API applies each person's scope; this page only offers the controls a role may use and says
// plainly what is loading, what is empty, what is limited and what is inferred.
import { Component, FormEvent, KeyboardEvent, ReactNode, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  type ArchiveMessage,
  type ArchiveSession,
  type BrainAgent,
  type BrainHold,
  type BrainOverview,
  type BrainSearch,
  type ConflictKind,
  type ConflictResolution,
  type ImportReport,
  type ImportRun,
  type ImportRunDetail,
  type MemoryConflict,
  type MemoryItem,
  type MemoryItemVersion,
  type MemoryKind,
  type Schedule,
  type WithholdPreview,
  type WithholdResult,
  type WorkflowDraft,
} from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, Tile } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { confirmReason, cronHint, TypedConfirm } from "./shared";
import "./Brain.css";

const BASE = "/api/brain";

// ---------------------------------------------------------------------------------------------------------------------
// Names (backend app/brain/__init__.py: KINDS, AGENTS, STAFF, STAFF_AGENT).

export const KIND_LABELS: Record<MemoryKind, string> = {
  business: "Business memory",
  knowledge: "Shared knowledge",
  agent_note: "Agent note",
  instruction: "Operating instruction (reference)",
  skill: "Skill reference",
  workflow: "Workflow reference",
  project: "Project and task context",
  decision: "Decision",
  accomplishment: "Accomplishment",
  problem: "Problem",
};
const KINDS = Object.keys(KIND_LABELS) as MemoryKind[];
/** What a viewer may read (backend policy.BUSINESS_KINDS): facts, not rules or code. */
const BUSINESS_KINDS: MemoryKind[] = ["business", "knowledge", "agent_note", "project", "decision", "accomplishment", "problem"];
/** Kinds that always need an admin to approve or edit into (backend policy.needs_admin). */
const ADMIN_KINDS: MemoryKind[] = ["instruction", "skill", "workflow"];

const AGENT_TITLES: Record<string, string> = {
  supervisor: "AI Supervisor",
  wordpress_security: "WordPress Security & Repair",
  seo_content: "SEO & Content",
  social_marketing: "Social Media & Marketing",
  server_management: "VPS & Server Management",
  communications: "Email & Customer Communications",
  monitoring: "Website & Infrastructure Monitoring",
  credit_repair: "Credit Repair & Dispute Management",
};
const STAFF = ["jarvis", "sarah", "scout", "tom", "ava", "bobby"];
const HISTORY_LABELS = ["accomplishment", "problem", "decision"];

// ---------------------------------------------------------------------------------------------------------------------
// The exact words the specification asks for.

export const WITHHELD_SESSION = "Withheld: this conversation contained restricted data; it is kept only in the JARVIS backup";
export const QUARANTINED_TEXT = "Quarantined: held a secret (removed) or an instruction-like rule; an admin must review it.";
export const SOURCE_WITHHELD_TEXT =
  "Its source message was withheld: its text, and the text of every earlier version, was removed for good. It cannot be edited, approved or reopened; delete and purge it when it is no longer needed.";
export const SOURCE_WITHHELD_PENDING_TEXT =
  "Its source message was withheld, but its text is not removed yet: the next start of the Command Center, or a rescan, removes it. Until then only admins see it, and it cannot be edited, approved or reopened.";
/** The title of a memory item emptied because its source message was withheld (backend policy.WITHHELD_COPY_TITLE). */
export const WITHHELD_COPY_TITLE = "Withheld: the archived message it was promoted from was withheld";
export const WITHHOLD_REMOVES =
  "Its text, tool calls and labels are removed from the Command Center for good, and the conversation's summary, labels and counts are worked out again without it. Answers and reference notes that quote it are replaced with a removal note.";
export const WITHHOLD_NOT_REACHED =
  "Not reached: the JARVIS backup, Command Center backups, exports and decisions files already saved, the test copy, Docker container logs, and text already sent to an AI model or written into drafts. The conversation's title and related messages (a paired tool result, a summary, a copy in a branch) are kept; withhold them separately. This cannot be undone.";
export const PURGE_PROMOTED_TEXT = "The conversation message it came from is kept; withhold that message to remove its text.";
/** 0.6.2: what a release of a held row does (the importer restores it from the source; emptied copies stay empty). */
export const RELEASE_CONFIRM = "Its text comes back at the next import; promoted copies stay empty.";
export const HOLDS_INTRO =
  "These messages were withheld because they may be a message an admin withheld: they share its JARVIS id but their text differs, or they repeat its exact text outside a copy JARVIS made. Their text is not shown. Keep one withheld for good, or release it: certain copies (the same JARVIS row, a carried-on copy) are never listed here and cannot be released.";

/** Emptied because its source message was withheld (backend store.emptied()): flagged and without text. A copy 0.6.0
 * flagged can still hold its text until the next start or a rescan empties it. */
export function emptiedCopy(item: Pick<MemoryItem, "flags" | "title" | "body" | "tags">): boolean {
  return !!item.flags?.source_withheld && item.title === WITHHELD_COPY_TITLE && !item.body && !item.tags?.length;
}

/** What withholding a message removes, given how many memory items were promoted from it (null: they could not be
 * counted, with the error). The API empties every copy whatever this says (GET /messages/{id}/promoted counts them). */
export function withholdText(count: number | null, error?: string | null): string[] {
  const lines = [WITHHOLD_REMOVES];
  if (count === null) {
    lines.push(`Could not count the promoted copies (${error || "no answer"}); any copies are emptied too.`);
  } else if (count > 0) {
    lines.push(
      `${count.toLocaleString()} memory item(s) were promoted from this message. Their title, text, tags and review notes, and every earlier version, are removed too, including edits made since (copy any words of your own into a new item first). Each stays as an empty placeholder an admin can delete and purge.`,
    );
  }
  lines.push(WITHHOLD_NOT_REACHED);
  return lines;
}
/** The withhold preview in words (0.6.2): the other stored rows of the same JARVIS message, withheld too, and those held for
 * the admin's review; copies with no row of their own. Empty when the message is stored once. */
export function previewText(p: WithholdPreview | null | undefined): string[] {
  if (!p) return [];
  const n = p.same_message_rows.length + (p.same_message_rows_hidden ?? 0);
  const m = p.held_rows.length + (p.held_rows_hidden ?? 0);
  const lines: string[] = [];
  if (n || m) {
    lines.push(
      `${plural(n, "other stored row")} ${n === 1 ? "is" : "are"} the same JARVIS message and ${n === 1 ? "is" : "are"} withheld too; ${m.toLocaleString()} ${m === 1 ? "is" : "are"} held for your review.`,
    );
  }
  if (p.rowless_copies > 0) {
    lines.push(`${plural(p.rowless_copies, "copy", "copies")} JARVIS keeps without a stored row of its own will never be imported with this text either.`);
  }
  return lines;
}

/** "Also withhold 2 other messages with exactly this text in this conversation" (unticked by default: identical words
 * elsewhere are not the same message). */
export function identicalLabel(k: number): string {
  return `Also withhold ${plural(k, "other message")} with exactly this text in this conversation`;
}

/** What a hold's rule means: what releasing it brings back. */
export function holdRuleText(rule: string): string {
  switch (rule) {
    case "same message id, different text":
      return "It shares the withheld message's JARVIS id, but its text differs from the withheld message as JARVIS holds it now. Releasing brings back that different text.";
    case "copied by JARVIS, nothing to compare":
      return "JARVIS copied it into a branch or continuation at the very time of the withheld message, whose row JARVIS no longer has, so the texts could not be compared. Releasing brings back whatever it holds.";
    case "same message id, contains the withheld text":
      return "It shares the withheld message's JARVIS id and its text contains the withheld message's text, with more around it. Releasing brings back the withheld text too.";
    case "same text":
      return "It repeats the withheld message's exact text, outside a copy JARVIS made. Releasing brings back text identical to the withheld message.";
    case "upgrade: same text":
      return "Found when 0.6.2 was installed: an earlier version stored the withheld message's exact text here. Releasing brings back that text.";
    case "upgrade: same message id":
    case "same message id":
      return "It shares the withheld message's JARVIS id; its text could not be compared. Releasing brings back whatever it holds.";
    default:
      return `Held by the rule "${rule}".`;
  }
}

export const CUSTOMER_PILL = "customer data (masked)";
export const NO_IMPORT = "No JARVIS import yet";
export const AI_OFF = "Off: memory never goes into AI prompts.";

/** "1 message hidden: …", "3 messages hidden: …": messages outside an operator's scope (customer data). */
export function hiddenText(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? "message" : "messages"} hidden: customer data (admins only)`;
}

// ---------------------------------------------------------------------------------------------------------------------
// Small helpers.

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

function errText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function plural(n: number, word: string, many = `${word}s`): string {
  return `${n.toLocaleString()} ${n === 1 ? word : many}`;
}

function cap(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/** "Same title" -> "same title", but "JARVIS rule" stays as it is. */
function lowerFirst(s: string): string {
  return /^[A-Z]{2,}\b/.test(s) ? s : s.charAt(0).toLowerCase() + s.slice(1);
}

function words(key: string): string {
  return key.replace(/_/g, " ");
}

function agentTitle(agent: string): string {
  return agent && agent !== "(shared)" ? (AGENT_TITLES[agent] ?? agent) : "Shared (every agent)";
}

function staffName(staff: string): string {
  return staff ? cap(staff) : "Unknown";
}

function kindLabel(kind: string, given?: string): string {
  return given || KIND_LABELS[kind as MemoryKind] || words(kind);
}

/** "…" while loading (never 0), "—" when it could not be loaded. */
function count(v: number | null | undefined, failed = false): string {
  if (v == null) return failed ? "—" : "…";
  return v.toLocaleString();
}

const dateFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const fullFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

/** An absolute time ("12 Mar 2025, 09:14"): JARVIS's history is years old, so "400 d ago" would say little. */
function When({ iso, dateOnly = false }: { iso: string | null | undefined; dateOnly?: boolean }) {
  if (!iso) return <span className="muted">unknown</span>;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return <span className="muted">unknown</span>;
  return (
    <time dateTime={iso} title={fullFmt.format(d)}>
      {dateOnly ? dateFmt.format(d) : fullFmt.format(d)}
    </time>
  );
}

function entries(rec: Record<string, unknown> | undefined | null): [string, number][] {
  return Object.entries(rec ?? {}).filter((e): e is [string, number] => typeof e[1] === "number");
}

/** "3 approved · 2 pending review": the non-zero counts, biggest first. */
function breakdown(rec: Record<string, unknown> | undefined | null, label: (k: string) => string = words): string {
  return entries(rec)
    .filter(([, n]) => n > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n.toLocaleString()} ${label(k)}`)
    .join(" · ");
}

/** A short, safe rendering of a JSON value (report fields, conflict sides). */
function brief(v: unknown): string {
  if (v == null || v === "") return "—";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (Array.isArray(v)) return v.map(brief).join(", ");
  return Object.entries(v as Record<string, unknown>)
    .map(([k, x]) => `${words(k)}: ${brief(x)}`)
    .join(" · ");
}

// ---------------------------------------------------------------------------------------------------------------------
// Pills and chips.

const ITEM_STATUS: Record<string, [string, string]> = {
  pending_review: ["pending", "Pending review"],
  quarantined: ["warning", "Quarantined"],
  approved: ["approved", "Approved"],
  rejected: ["rejected", "Rejected"],
  archived: ["archived", "Archived"],
};
const RUN_STATUS: Record<string, [string, string]> = {
  running: ["running", "Running"],
  completed: ["completed", "Completed"],
  failed: ["failed", "Failed"],
  interrupted: ["degraded", "Interrupted"],
  rolled_back: ["archived", "Rolled back"],
};
const DRAFT_STATUS: Record<string, [string, string]> = {
  draft: ["draft", "Draft"],
  reviewed: ["queued", "Reviewed"],
  converted: ["unknown", "Converted"],
  rejected: ["rejected", "Rejected"],
};
const DRAFT_WORDS: Record<string, string> = { draft: "to review", reviewed: "reviewed", converted: "converted (schedule disabled)", rejected: "rejected" };
const COMPAT: Record<string, [string, string, string]> = {
  mapped: ["up", "Mapped", "A Command Center capability does the same job."],
  partial: ["degraded", "Partial", "A capability exists, but something JARVIS did is missing or now needs approval."],
  reference_only: ["unknown", "Reference only", "No counterpart in the Command Center: kept as text."],
};

function ItemStatus({ item }: { item: Pick<MemoryItem, "status" | "deleted_at"> }) {
  if (item.deleted_at) return <Pill value="failed" label="Deleted" />;
  const [v, l] = ITEM_STATUS[item.status] ?? ["unknown", words(item.status)];
  return <Pill value={v} label={l} />;
}

function RunStatus({ status }: { status: string }) {
  const [v, l] = RUN_STATUS[status] ?? ["unknown", words(status)];
  return <Pill value={v} label={l} />;
}

function DraftStatus({ status }: { status: string }) {
  const [v, l] = DRAFT_STATUS[status] ?? ["unknown", words(status)];
  return <Pill value={v} label={l} />;
}

function Compat({ value }: { value: string | undefined }) {
  if (!value) return <span className="muted">—</span>;
  const [v, l, help] = COMPAT[value] ?? ["unknown", words(value), ""];
  return (
    <span title={help}>
      <Pill value={v} label={l} />
    </span>
  );
}

export function CustomerPill() {
  return (
    <span className="pill tone-purple br-customer" title="E-mail addresses and phone numbers are masked. Admins only; never sent to an AI model.">
      {CUSTOMER_PILL}
    </span>
  );
}

/** Flags that keep an item out of AI prompts and need an admin (backend policy.RISK_FLAGS). */
const RISK_FLAGS = ["instruction_like", "approval_conflict", "secrets_redacted", "script", "plugin_or_hook", "custom", "owner_profile", "source_withheld", "purged_before"];
const FLAG_TEXT: Record<string, string | ((v: unknown) => string)> = {
  secrets_redacted: "secrets removed",
  instruction_like: "instruction-like",
  approval_conflict: "approval conflict",
  script: "script (never run)",
  plugin_or_hook: "plugin or hook (never run)",
  custom: "custom skill (no counterpart)",
  owner_profile: "owner's profile",
  template: (v) => (typeof v === "string" && v ? `template (${v})` : "template"),
  truncated: "truncated",
  conflict: "same title as another item",
  replaces_source_key: "changed version of an imported file",
  reasoning_removed: "reasoning removed",
  from_content_folder: "from the content folder",
  time_assumed_local: "time assumed local",
  rejected_source_sha256: "a changed version was rejected before",
  restricted: "restricted data removed",
  withheld: "withheld by an admin",
  source_withheld: "source message withheld",
  purged_before: "the same text as an item an admin purged",
  // 0.6.2: why a message is withheld (flags.withheld_by_admin) and a title an admin hid.
  withheld_by_admin: (v) => withheldByAdmin(v),
  title_withheld: "title hidden by an admin",
};

/** A withheld message's flags.withheld_by_admin in words: held for review, kept, released, or withheld by the database
 * guard (the importer tried to store its text and the database removed it). */
export function withheldByAdmin(v: unknown): string {
  const f = v && typeof v === "object" ? (v as Record<string, unknown>) : {};
  if (f.released_at) return "released: its text comes back at the next import";
  if (f.review === "pending") return "held for review";
  if (f.guard) return "withheld by the database guard";
  if (f.review === "kept") return "withheld by an admin (kept after review)";
  return "withheld by an admin";
}
/** Flags that describe rather than warn (a skill's mapping, the masking shown by the customer pill). */
const NOT_CHIPS = new Set([
  "skill", "staff", "capabilities", "compatibility", "note", "frontmatter", "language", "executable", "masked", "attribution", "reference_only",
  "source_session", "text_kind", "message_id", "archive_session_id", "unknown_role", "message_source_key",
]);

function secretTypes(v: unknown): string {
  if (!v || typeof v !== "object") return "";
  return entries(v as Record<string, unknown>)
    .map(([k, n]) => `${words(k)} ×${n}`)
    .join(", ");
}

export function FlagChips({ flags, sensitivity }: { flags: Record<string, unknown> | undefined | null; sensitivity?: string }) {
  const chips: { key: string; text: string; risky: boolean; title?: string }[] = [];
  for (const [k, v] of Object.entries(flags ?? {})) {
    if (!v || NOT_CHIPS.has(k) || (Array.isArray(v) && !v.length)) continue;
    const t = FLAG_TEXT[k];
    const text = typeof t === "function" ? t(v) : (t ?? words(k));
    const title = k === "secrets_redacted" ? `Removed: ${secretTypes(v) || "secrets"} (the values were never stored)` : k === "instruction_like" && Array.isArray(v) ? `Matched: ${v.map(String).map(words).join(", ")}` : undefined;
    chips.push({ key: k, text, risky: RISK_FLAGS.includes(k), title });
  }
  const customer = sensitivity === "customer" || !!flags?.masked;
  if (!chips.length && !customer) return null;
  return (
    <span className="br-chips">
      {customer ? <CustomerPill /> : null}
      {chips.map((c) => (
        <span key={c.key} className={`br-chip${c.risky ? " br-chip-risk" : ""}`} title={c.title}>
          {c.text}
        </span>
      ))}
    </span>
  );
}

function LabelChips({ labels }: { labels: string[] | Record<string, number> | undefined }) {
  const list: [string, number | null][] = Array.isArray(labels) ? labels.map((l) => [l, null]) : entries(labels).filter(([, n]) => n > 0);
  if (!list.length) return null;
  return (
    <span className="br-labels" title="Labels come from keyword matching, not from reading the text: they can be wrong.">
      {list.map(([l, n]) => (
        <span key={l} className={`br-label br-label-${l}`}>
          {n != null ? `${n.toLocaleString()} ` : ""}
          {l}
        </span>
      ))}
      <span className="br-inferred">matched by keywords</span>
    </span>
  );
}

/** Why an item needs an admin to approve or edit it (backend policy.needs_admin). */
function adminReason(item: MemoryItem): string {
  if (item.status === "quarantined") return "it is quarantined";
  if (ADMIN_KINDS.includes(item.kind)) return "operating instructions, skills and workflow references are approved by an admin";
  if (item.agent === "credit_repair") return "Credit Repair items are approved by an admin";
  const risky = RISK_FLAGS.filter((f) => item.flags?.[f]).map((f) => {
    const t = FLAG_TEXT[f];
    return typeof t === "function" ? t(item.flags[f]) : (t ?? words(f));
  });
  return risky.length ? `it is flagged: ${risky.join(", ")}` : "the API says so";
}

// ---------------------------------------------------------------------------------------------------------------------
// Confirmation dialog for one-way actions that need no typed name.

function ConfirmModal({
  title,
  action,
  children,
  error,
  busy,
  onConfirm,
  onCancel,
}: {
  title: string;
  action: string;
  children: ReactNode;
  error?: string | null;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="alert alert-warn br-confirm-text">{children}</div>
      <ErrorNote error={error} />
      <div className="row-actions">
        <button className="btn danger" type="button" disabled={busy} onClick={onConfirm}>
          {action}
        </button>
        <button className="btn" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// One memory item: text, flags, source, versions; review, edit and the admin actions.

const REVIEW_ACTIONS: { decision: "approve" | "reject" | "archive" | "reopen"; label: string; to: string }[] = [
  { decision: "approve", label: "Approve", to: "approved" },
  { decision: "reject", label: "Reject", to: "rejected" },
  { decision: "archive", label: "Archive", to: "archived" },
  { decision: "reopen", label: "Reopen for review", to: "pending_review" },
];

const ORIGIN: Record<string, string> = { import: "Imported from JARVIS", manual: "Written in the Command Center", promoted: "Promoted from a JARVIS conversation" };

function EditItemForm({ item, onSaved, onCancel }: { item: MemoryItem; onSaved: () => void; onCancel: () => void }) {
  const { user } = useAuth();
  const [title, setTitle] = useState(item.title);
  const [body, setBody] = useState(item.body);
  const [tags, setTags] = useState((item.tags ?? []).join(", "));
  const [kind, setKind] = useState<MemoryKind>(item.kind);
  const [agent, setAgent] = useState(item.agent);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Moving an item into or out of Credit Repair needs an admin with the sensitive-data permission.
  const creditRepair = item.agent === "credit_repair" || (user.role === "admin" && !!user.can_access_sensitive);
  // An operator's edit is refused when the new state needs an admin (policy.needs_admin): so no admin-only kind is offered.
  const kinds = user.role === "admin" ? KINDS : KINDS.filter((k) => !ADMIN_KINDS.includes(k) || k === item.kind);

  async function save(e: FormEvent) {
    e.preventDefault();
    const tagList = tags
      .split(",")
      .map((t) => t.trim())
      .filter(Boolean);
    const changes: Record<string, unknown> = {};
    if (title !== item.title) changes.title = title;
    if (body !== item.body) changes.body = body;
    if (tagList.join(",") !== (item.tags ?? []).join(",")) changes.tags = tagList;
    if (kind !== item.kind) changes.kind = kind;
    if (agent !== item.agent) changes.agent = agent;
    if (!Object.keys(changes).length) {
      setError("Nothing was changed.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.patch(`${BASE}/items/${item.id}`, { ...changes, note });
      onSaved();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="br-edit" onSubmit={save}>
      <label>
        Title
        <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} required />
      </label>
      <label>
        Text
        <textarea rows={10} value={body} onChange={(e) => setBody(e.target.value)} />
      </label>
      <div className="br-edit-row">
        <label>
          Kind
          <select value={kind} onChange={(e) => setKind(e.target.value as MemoryKind)}>
            {kinds.map((k) => (
              <option key={k} value={k}>
                {KIND_LABELS[k]}
              </option>
            ))}
          </select>
        </label>
        <label>
          Agent
          <select value={agent} onChange={(e) => setAgent(e.target.value)}>
            <option value="">Shared (every agent)</option>
            {Object.entries(AGENT_TITLES)
              .filter(([k]) => k !== "credit_repair" || creditRepair)
              .map(([k, t]) => (
                <option key={k} value={k}>
                  {t}
                </option>
              ))}
          </select>
        </label>
      </div>
      <label>
        Tags (comma-separated)
        <input value={tags} onChange={(e) => setTags(e.target.value)} />
      </label>
      <label>
        Note for the version history
        <input value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} placeholder="Why this changed (optional)" />
      </label>
      <p className="muted small">
        Saving creates a new version. An approved item goes back to review. Secrets and contact details in the new text are removed or masked as on import;
        text with restricted data (SSNs, card or bank details) is refused.
        {user.role === "admin" ? "" : " Operating instructions, skills and workflow references are kinds only an admin can give an item."}
      </p>
      <ErrorNote error={error} />
      <div className="row-actions">
        <button className="btn primary" type="submit" disabled={busy}>
          Save as a new version
        </button>
        <button className="btn" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

/** A version an admin redacted: its title and body were blanked (spec 3; the title may hold a "redacted" marker). The
 * `redacted` change alone does not say so: `brain-import rescan` also writes "redacted" versions, which keep their
 * re-cleaned text. */
function isRedacted(v: Pick<MemoryItemVersion, "title" | "body">): boolean {
  const title = (v.title ?? "").trim();
  return !v.body && (!title || /^\W*redacted\W*$/i.test(title));
}

function VersionList({ item, versions, onChanged }: { item: MemoryItem; versions: MemoryItemVersion[]; onChanged: () => void }) {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const [redact, setRedact] = useState<MemoryItemVersion | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function doRedact(v: MemoryItemVersion) {
    setBusy(true);
    setError(null);
    try {
      await api.post(`${BASE}/items/${item.id}/versions/${v.version}/redact`);
      setRedact(null);
      onChanged();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  if (!versions.length) return <Empty>No versions recorded.</Empty>;
  return (
    <>
      <ol className="br-versions" reversed>
        {versions.map((v) => {
          const redacted = isRedacted(v);
          return (
            <li key={v.version} className="br-version">
              <div className="br-version-head">
                <strong>Version {v.version}</strong> <span className="br-chip">{words(v.change)}</span>
                <span className="muted small">
                  {v.changed_by || "unknown"} · <When iso={v.changed_at} />
                </span>
                {isAdmin && !redacted ? (
                  <button className="btn small ghost" type="button" onClick={() => setRedact(v)} aria-label={`Redact version ${v.version}`}>
                    Redact
                  </button>
                ) : null}
              </div>
              {v.note ? <div className="muted small">Note: {v.note}</div> : null}
              {redacted ? (
                <div className="muted small">Text redacted by an admin.</div>
              ) : (
                <details className="br-details">
                  <summary>Show this version's text</summary>
                  <p>
                    <strong>{v.title}</strong> <span className="muted small">({kindLabel(v.kind)} · {agentTitle(v.agent)} · {words(v.status)})</span>
                  </p>
                  <pre className="br-text">{v.body || "(no text)"}</pre>
                </details>
              )}
            </li>
          );
        })}
      </ol>
      {redact ? (
        <ConfirmModal
          title={`Redact version ${redact.version}`}
          action="Redact this version"
          busy={busy}
          error={error}
          onConfirm={() => void doRedact(redact)}
          onCancel={() => {
            setRedact(null);
            setError(null);
          }}
        >
          The title and text of version {redact.version} are blanked for good; the record that the version existed stays. Use this for a version that held
          something it should not. It cannot be undone.
        </ConfirmModal>
      ) : null}
    </>
  );
}

export function ItemModal({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged?: () => void }) {
  const { user, canOperate } = useAuth();
  const isAdmin = user.role === "admin";
  const detail = usePoll(() => api.get<{ item: MemoryItem; versions?: MemoryItemVersion[] }>(`${BASE}/items/${id}`), 0, [id]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);
  const [purging, setPurging] = useState(false);
  const [purgeError, setPurgeError] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const item = detail.data?.item;

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      setNote("");
      await detail.refresh();
      onChanged?.();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  async function purge() {
    setPurgeError(null);
    try {
      await api.del(`${BASE}/items/${id}/purge`);
      setPurging(false);
      setGone(true);
      onChanged?.();
    } catch (err) {
      setPurgeError(errText(err));
    }
  }

  if (gone) {
    return (
      <Modal title={`Memory item #${id}`} onClose={onClose}>
        <div className="alert alert-good">Item #{id} was purged: it, its versions and its conflicts are gone, and a later import will not bring it back.</div>
      </Modal>
    );
  }

  const locked = !!item && item.needs_admin && !isAdmin;
  const deleted = !!item?.deleted_at;
  // Its source message was withheld: never edited, approved or reopened again (backend store.py). Emptied once its
  // text is gone; a copy 0.6.0 flagged keeps it until the next start or a rescan.
  const sourceWithheld = !!item?.flags?.source_withheld;
  const emptied = !!item && emptiedCopy(item);
  return (
    <Modal title={item ? item.title : `Memory item #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error} />
      {!item ? (
        detail.error ? null : <p className="muted">Loading…</p>
      ) : (
        <div className="br-item">
          <p className="br-item-meta">
            <ItemStatus item={item} /> <span className="br-kind">{kindLabel(item.kind, item.kind_label)}</span>
            <span className="muted">· {agentTitle(item.agent)} · item #{item.id} · version {item.version}</span>
          </p>
          <FlagChips flags={item.flags} sensitivity={item.sensitivity} />
          {emptied ? (
            <p className="alert alert-warn small br-quarantined">{SOURCE_WITHHELD_TEXT}</p>
          ) : sourceWithheld ? (
            <p className="alert alert-warn small br-quarantined">{SOURCE_WITHHELD_PENDING_TEXT}</p>
          ) : item.status === "quarantined" ? (
            <p className="alert alert-warn small br-quarantined">{QUARANTINED_TEXT}</p>
          ) : null}
          {item.origin !== "manual" ? (
            <p className="muted small">Imported text is reference only: it never becomes an instruction, an approval or a setting, and code still decides every approval.</p>
          ) : null}
          {editing ? (
            <EditItemForm
              item={item}
              onCancel={() => setEditing(false)}
              onSaved={() => {
                setEditing(false);
                void detail.refresh();
                onChanged?.();
              }}
            />
          ) : (
            <pre className="br-text br-item-body">{item.body || "(no text)"}</pre>
          )}

          <h3>Source</h3>
          <dl className="kv br-kv">
            <dt>Origin</dt>
            <dd>{ORIGIN[item.origin] ?? item.origin}</dd>
            {item.source_ref ? (
              <>
                <dt>Source</dt>
                <dd>
                  <code className="br-ref">{item.source_ref}</code>
                  {item.source_system ? <span className="muted small"> ({item.source_system === "jarvis" ? "JARVIS" : item.source_system})</span> : null}
                </dd>
              </>
            ) : null}
            {item.source_at ? (
              <>
                <dt>Source date</dt>
                <dd>
                  <When iso={item.source_at} />
                </dd>
              </>
            ) : null}
            {item.import_run_id ? (
              <>
                <dt>Import run</dt>
                <dd>{canOperate ? <Link to={`/brain?tab=imports&run=${item.import_run_id}`}>Run #{item.import_run_id}</Link> : `Run #${item.import_run_id}`}</dd>
              </>
            ) : null}
            {item.tags?.length ? (
              <>
                <dt>Tags</dt>
                <dd>{item.tags.join(", ")}</dd>
              </>
            ) : null}
            <dt>Created</dt>
            <dd>
              <When iso={item.created_at} /> {item.created_by ? `by ${item.created_by}` : ""}
            </dd>
            <dt>Last change</dt>
            <dd>
              <When iso={item.updated_at} /> {item.updated_by ? `by ${item.updated_by}` : ""}
            </dd>
            {item.reviewed_at ? (
              <>
                <dt>Reviewed</dt>
                <dd>
                  <When iso={item.reviewed_at} /> by {item.reviewed_by || "unknown"}
                  {item.review_note ? <span className="muted"> · “{item.review_note}”</span> : null}
                </dd>
              </>
            ) : null}
            {deleted ? (
              <>
                <dt>Deleted</dt>
                <dd>
                  <When iso={item.deleted_at} />
                </dd>
              </>
            ) : null}
          </dl>

          {canOperate && !deleted && !editing && !sourceWithheld ? (
            <section className="br-actions" aria-label="Review">
              <h3>Review</h3>
              {locked ? (
                <p className="br-locked small">
                  <Icon name="lock" size={14} /> Needs an admin: {adminReason(item)}.
                </p>
              ) : item.needs_admin ? (
                <p className="muted small">Needs an admin: {adminReason(item)}.</p>
              ) : null}
              <div className="row-actions">
                <input aria-label="Review note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} disabled={locked} />
                {/* "Reopen" on a quarantined item would only move it to pending review, where operators can read it: a quarantined
                    item is approved, rejected or archived by an admin instead. */}
                {REVIEW_ACTIONS.filter((a) => a.to !== item.status && !(a.decision === "reopen" && item.status === "quarantined")).map((a) => (
                  <button
                    key={a.decision}
                    type="button"
                    className={`btn small${a.decision === "approve" ? " primary" : ""}`}
                    disabled={busy || locked}
                    onClick={() => void act(() => api.post(`${BASE}/items/${id}/review`, { decision: a.decision, note }))}
                  >
                    {a.label}
                  </button>
                ))}
                <button type="button" className="btn small" disabled={busy || locked} onClick={() => setEditing(true)}>
                  <Icon name="docText" size={14} /> Edit
                </button>
              </div>
              {item.status === "approved" ? (
                <p className="muted small">Approved: agents may read it as reference. {item.sensitivity === "general" && !RISK_FLAGS.some((f) => item.flags?.[f]) ? "" : "It never goes into an AI prompt."}</p>
              ) : null}
            </section>
          ) : null}

          {isAdmin ? (
            <section className="br-actions" aria-label="Admin">
              <h3>Admin</h3>
              <div className="row-actions">
                {deleted ? (
                  <>
                    <button type="button" className="btn small" disabled={busy} onClick={() => void act(() => api.post(`${BASE}/items/${id}/restore`))}>
                      Restore
                    </button>
                    <button type="button" className="btn small danger" disabled={busy} onClick={() => setPurging(true)}>
                      Purge for good
                    </button>
                    <span className="muted small">A restored item is reviewed again.</span>
                  </>
                ) : (
                  <>
                    <button type="button" className="btn small danger" disabled={busy} onClick={() => void act(() => api.del(`${BASE}/items/${id}`))}>
                      Delete
                    </button>
                    <span className="muted small">Hidden everywhere and kept with its versions until purged; it can be restored.</span>
                  </>
                )}
              </div>
            </section>
          ) : null}
          <ErrorNote error={error} />

          {canOperate ? (
            <section aria-label="Versions">
              <h3>Versions</h3>
              {detail.data?.versions ? (
                <VersionList item={item} versions={detail.data.versions} onChanged={() => void detail.refresh()} />
              ) : (
                <p className="muted small">The API did not return the version history.</p>
              )}
            </section>
          ) : null}
        </div>
      )}
      {purging && item ? (
        <TypedConfirm
          title={`Purge item #${item.id}`}
          name={String(item.id)}
          action="Purge for good"
          error={purgeError}
          onConfirm={purge}
          onCancel={() => {
            setPurging(false);
            setPurgeError(null);
          }}
        >
          This deletes item #{item.id}, every version of it and its conflicts permanently.{" "}
          {item.origin === "promoted" ? PURGE_PROMOTED_TEXT : "A later import will not bring it back (its source is remembered as purged)."} This cannot be
          undone.
        </TypedConfirm>
      ) : null}
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// One conversation: its messages, with promote (operators) and withhold (admins).

const ROLE_LABEL: Record<string, string> = { user: "User", assistant: "Assistant", tool: "Tool result" };
const MESSAGE_PAGE = 200;

interface SessionDetail {
  session: ArchiveSession;
  messages: ArchiveMessage[];
  total: number;
  hidden: number;
}

function attribution(s: Pick<ArchiveSession, "staff" | "flags">): string {
  const a = s.flags?.attribution;
  if (a === "job id") return "inferred from the scheduled job";
  if (a === "default") return "not attributable: kept for Jarvis";
  return "";
}

function PromoteModal({
  message,
  session,
  onClose,
  onOpenItem,
  onChanged,
}: {
  message: ArchiveMessage;
  session: ArchiveSession;
  onClose: () => void;
  onOpenItem: (id: number) => void;
  onChanged?: () => void;
}) {
  const firstLine = (message.content || "").split("\n").find((l) => l.trim()) ?? "";
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  // A message a secret was removed from becomes a quarantined item, which only an admin may create.
  const needsAdmin = !isAdmin && Object.keys(message.redactions ?? {}).length > 0;
  const kinds = isAdmin ? KINDS : KINDS.filter((k) => !ADMIN_KINDS.includes(k));
  const [kind, setKind] = useState<MemoryKind>("knowledge");
  const [title, setTitle] = useState(firstLine.trim().slice(0, 120));
  // Text from a Credit Repair conversation stays in Credit Repair (the API refuses any other agent).
  const creditRepair = session.agent === "credit_repair";
  const [agent, setAgent] = useState(session.agent);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<MemoryItem | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ item: MemoryItem }>(`${BASE}/messages/${message.id}/promote`, { kind, title: title.trim(), agent });
      setCreated(r.item);
      onChanged?.();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Promote to memory" onClose={onClose}>
      {created ? (
        <>
          <div className="alert alert-good">
            {created.status === "quarantined"
              ? `Saved as memory item #${created.id}, quarantined: a secret was removed from this message, so an admin reviews it. Nothing reads it until it is approved.`
              : `Saved as memory item #${created.id}, waiting for review. Nothing reads it until it is approved.`}
          </div>
          <div className="row-actions">
            <button className="btn primary" type="button" onClick={() => onOpenItem(created.id)}>
              Open item #{created.id}
            </button>
            <button className="btn" type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      ) : (
        <form onSubmit={submit}>
          <p className="muted small">
            Creates a memory item from this message, waiting for review. The message stays in the archive. The item's source reads “JARVIS session{" "}
            {session.source_session_id} · message {message.position}”, and it survives a rollback of the import.
          </p>
          <label>
            Title
            <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} required />
          </label>
          <div className="br-edit-row">
            <label>
              Kind
              <select value={kind} onChange={(e) => setKind(e.target.value as MemoryKind)}>
                {kinds.map((k) => (
                  <option key={k} value={k}>
                    {KIND_LABELS[k]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Agent
              {creditRepair ? (
                <select value="credit_repair" disabled>
                  <option value="credit_repair">{AGENT_TITLES.credit_repair} (stays in Credit Repair)</option>
                </select>
              ) : (
                <select value={agent} onChange={(e) => setAgent(e.target.value)}>
                  <option value="">Shared (every agent)</option>
                  {Object.entries(AGENT_TITLES)
                    .filter(([k]) => k !== "credit_repair")
                    .map(([k, t]) => (
                      <option key={k} value={k}>
                        {t}
                      </option>
                    ))}
                </select>
              )}
            </label>
          </div>
          {needsAdmin ? (
            <p className="br-locked small">
              <Icon name="lock" size={14} /> A secret was removed from this message, so its item is quarantined: an admin promotes it.
            </p>
          ) : null}
          <ErrorNote error={error} />
          <div className="row-actions">
            <button className="btn primary" type="submit" disabled={busy || !title.trim() || needsAdmin}>
              Promote
            </button>
            <button className="btn" type="button" onClick={onClose}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}

function MessageRow({
  m,
  focused,
  canPromote,
  canWithhold,
  onPromote,
  onWithhold,
}: {
  m: ArchiveMessage;
  focused: boolean;
  canPromote: boolean;
  canWithhold: boolean;
  onPromote: () => void;
  onWithhold: () => void;
}) {
  const role = ROLE_LABEL[m.role] ?? cap(m.role);
  const secrets = Object.keys(m.redactions ?? {}).length > 0;
  const text = m.withheld ? null : m.content ? <pre className="br-text">{m.content}</pre> : <p className="muted small">(no text)</p>;
  return (
    <li className={`br-msg br-role-${m.role}${focused ? " br-msg-focus" : ""}`} id={`br-msg-${m.id}`} aria-current={focused ? "true" : undefined}>
      <div className="br-msg-head">
        <span className="br-role">{role}</span>
        {m.tool_name ? <code className="br-tool">{m.tool_name}</code> : null}
        <span className="muted small">
          <When iso={m.at} /> · #{m.position}
        </span>
        {m.state === "compacted" ? <span className="br-chip" title="JARVIS had compacted this message (summarised it away from its live context).">compacted</span> : null}
        {m.truncated ? <span className="br-chip">truncated</span> : null}
        {secrets ? (
          <span className="br-chip br-chip-risk" title={`Removed: ${secretTypes(m.redactions) || "secrets"}`}>
            secrets removed
          </span>
        ) : null}
        {m.sensitivity === "customer" ? <CustomerPill /> : null}
        <LabelChips labels={m.labels} />
      </div>
      {m.withheld ? (
        <p className="br-withheld small">
          Withheld: no text is kept for this message.
          {m.flags?.withheld_by_admin ? <span className="br-chip br-chip-risk">{withheldByAdmin(m.flags.withheld_by_admin)}</span> : null}
        </p>
      ) : m.role === "tool" ? (
        <details className="br-details">
          <summary>Show the tool result</summary>
          {text}
        </details>
      ) : (
        text
      )}
      {m.tool_calls?.length ? (
        <details className="br-details">
          <summary>{plural(m.tool_calls.length, "tool call")}</summary>
          <ul className="br-calls">
            {m.tool_calls.map((c, i) => (
              <li key={i}>
                <code>{c.name}</code>
                {c.arguments ? <pre className="br-text">{c.arguments}</pre> : null}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {(canPromote || canWithhold) && !m.withheld ? (
        <div className="row-actions br-msg-actions">
          {canPromote ? (
            <button className="btn small" type="button" onClick={onPromote}>
              Promote to memory
            </button>
          ) : null}
          {canWithhold ? (
            <button className="btn small ghost" type="button" onClick={onWithhold}>
              Withhold
            </button>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/** The 0.6.2 part of the withhold dialog: the other stored rows of the same JARVIS message (withheld too) and those held
 * for review, the opt-in for identical text elsewhere in the conversation, the title option, and the imported memory
 * items that contain the text (listed to purge by hand: nothing is removed automatically). */
function WithholdChoices({
  preview,
  includeIdentical,
  setIncludeIdentical,
  hideTitle,
  setHideTitle,
  onOpenItem,
}: {
  preview: { data: WithholdPreview | null; error: string | null };
  includeIdentical: boolean;
  setIncludeIdentical: (v: boolean) => void;
  hideTitle: boolean | null;
  setHideTitle: (v: boolean) => void;
  onOpenItem: (id: number) => void;
}) {
  const p = preview.data;
  if (!p) {
    return (
      <p>
        Could not list the other stored copies of this message ({preview.error || "no answer"}); every stored row of the same JARVIS message is withheld too, and the
        title is hidden when this is the conversation's first user message.
      </p>
    );
  }
  const identical = p.identical_text_rows.length + (p.identical_text_rows_hidden ?? 0);
  const items = p.items_with_this_text ?? [];
  return (
    <>
      {previewText(p).map((line) => (
        <p key={line}>{line}</p>
      ))}
      {identical > 0 ? (
        <label className="check">
          <input type="checkbox" checked={includeIdentical} onChange={(e) => setIncludeIdentical(e.target.checked)} />
          {identicalLabel(identical)}
        </label>
      ) : null}
      <label className="check">
        <input type="checkbox" checked={!!hideTitle} onChange={(e) => setHideTitle(e.target.checked)} />
        Hide the conversation title
      </label>
      {items.length ? (
        <div className="br-preview-items">
          <p>
            {plural(items.length, "imported memory item")} {items.length === 1 ? "contains" : "contain"} this text. They are not removed: open one to delete and purge it.
          </p>
          <ul className="br-list small">
            {items.map((it) => (
              <li key={it.id}>
                <button type="button" className="link-button" onClick={() => onOpenItem(it.id)}>
                  {it.title}
                </button>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </>
  );
}

export function SessionModal({
  id,
  focus,
  onClose,
  onOpenItem,
  onChanged,
}: {
  id: number;
  focus: number | null;
  onClose: () => void;
  onOpenItem: (id: number) => void;
  /** After a promote or a withhold: the lists behind the dialog load again. */
  onChanged?: () => void;
}) {
  const { user, canOperate } = useAuth();
  const isAdmin = user.role === "admin";
  const [offset, setOffset] = useState(0);
  const tries = useRef(0);
  const located = useRef(false);
  const detail = usePoll(
    () => api.get<SessionDetail>(`${BASE}/sessions/${id}${qs({ limit: MESSAGE_PAGE, offset })}`).then((r) => ({ ...r, offset })),
    0,
    [id, offset],
  );
  const [promote, setPromote] = useState<ArchiveMessage | null>(null);
  const [withhold, setWithhold] = useState<ArchiveMessage | null>(null);
  const [withholdError, setWithholdError] = useState<string | null>(null);
  /** The memory items promoted from the message being withheld (undefined while they are counted). */
  const [copies, setCopies] = useState<{ count: number | null; error: string | null } | undefined>(undefined);
  /** 0.6.2: the other stored rows of the same message, held rows and identical text (undefined while it loads). */
  const [preview, setPreview] = useState<{ data: WithholdPreview | null; error: string | null } | undefined>(undefined);
  const [includeIdentical, setIncludeIdentical] = useState(false);
  /** null: the server decides (it hides the title for the conversation's first user message). */
  const [hideTitle, setHideTitle] = useState<boolean | null>(null);
  const [withheldNote, setWithheldNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const d = detail.data;
  const current = d && d.offset === offset ? d : null;

  // Opened at one message (from Search): page forward until it is in view, then scroll to it.
  useEffect(() => {
    if (!focus || !current || located.current) return;
    if (current.messages.some((m) => m.id === focus)) {
      located.current = true;
      document.getElementById(`br-msg-${focus}`)?.scrollIntoView?.({ block: "center" });
      return;
    }
    if (offset + current.messages.length < current.total && tries.current < 25) {
      tries.current += 1;
      setOffset(offset + MESSAGE_PAGE);
    }
  }, [focus, current, offset]);

  // The withhold dialog says how many promoted memory items it empties, and which other stored rows go with the message
  // (0.6.2: the withhold preview), before the admin confirms.
  useEffect(() => {
    setCopies(undefined);
    setPreview(undefined);
    setIncludeIdentical(false);
    setHideTitle(null);
    if (!withhold) return;
    let live = true;
    api.get<{ count: number }>(`${BASE}/messages/${withhold.id}/promoted`).then(
      (r) => live && setCopies({ count: r.count, error: null }),
      (err) => live && setCopies({ count: null, error: errText(err) }),
    );
    api.get<WithholdPreview>(`${BASE}/messages/${withhold.id}/withhold-preview`).then(
      (r) => {
        if (!live) return;
        setPreview({ data: r, error: null });
        setHideTitle(!!r.first_user_message);
      },
      (err) => live && setPreview({ data: null, error: errText(err) }),
    );
    return () => {
      live = false;
    };
  }, [withhold]);

  async function doWithhold(m: ArchiveMessage) {
    setBusy(true);
    setWithholdError(null);
    try {
      const r = await api.post<WithholdResult>(`${BASE}/messages/${m.id}/withhold`, { include_identical: includeIdentical, hide_title: hideTitle });
      setWithhold(null);
      const note = [`Withheld. ${(r?.copies?.items?.length ?? 0).toLocaleString()} promoted item(s) emptied.`];
      if (r?.also_withheld?.length) note.push(`${plural(r.also_withheld.length, "other stored row")} of the same JARVIS message withheld too.`);
      if (r?.held_for_review?.length) note.push(`${r.held_for_review.length.toLocaleString()} held for your review (JARVIS Brain → Review).`);
      if (r?.identical_withheld) note.push(`${plural(r.identical_withheld, "message")} with exactly this text withheld.`);
      if (r?.title_hidden) note.push("The conversation's title is hidden.");
      setWithheldNote(note.join(" "));
      await detail.refresh();
      onChanged?.();
    } catch (err) {
      setWithholdError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  const s = d?.session;
  const restricted = s?.sensitivity === "restricted";
  const shownFrom = current && current.messages.length ? offset + 1 : 0;
  const shownTo = current ? offset + current.messages.length : 0;
  return (
    <Modal title={s ? s.title || (restricted ? "Withheld conversation" : "Untitled conversation") : `Conversation #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error} />
      {!s ? (
        detail.error ? null : <p className="muted">Loading…</p>
      ) : (
        <div className="br-session">
          <dl className="kv br-kv">
            <dt>Staff</dt>
            <dd>
              {staffName(s.staff)} → {agentTitle(s.agent)}
              {attribution(s) ? <span className="br-inferred"> ({attribution(s)})</span> : null}
            </dd>
            <dt>Started</dt>
            <dd>
              <When iso={s.started_at} />
              {s.ended_at ? (
                <>
                  {" "}
                  · ended <When iso={s.ended_at} />
                </>
              ) : null}
              {s.flags?.time_assumed_local ? <span className="br-inferred"> (time assumed local)</span> : null}
            </dd>
            <dt>Origin</dt>
            <dd>
              {s.origin === "scheduled" ? `Scheduled job${s.job_name ? ` ${s.job_name}` : ""}` : "Chat"}
              {s.channel ? ` · ${s.channel}` : ""}
              {s.lineage ? ` · ${words(s.lineage)} of ${s.parent_source_session_id || "another conversation"}` : ""}
            </dd>
            <dt>Messages</dt>
            <dd>
              {plural(s.imported_messages, "message")} imported of {s.source_message_count.toLocaleString()} in JARVIS
              {s.withheld_messages ? ` · ${s.withheld_messages.toLocaleString()} withheld` : ""}
              {s.tool_calls ? ` · ${plural(s.tool_calls, "tool call")}` : ""}
            </dd>
            <dt>JARVIS id</dt>
            <dd>
              <code className="br-ref">{s.source_session_id}</code>
            </dd>
            {s.summary ? (
              <>
                <dt>Summary</dt>
                <dd>{s.summary}</dd>
              </>
            ) : null}
          </dl>
          <div className="br-session-chips">
            {s.sensitivity === "customer" ? <CustomerPill /> : null}
            {s.flags?.title_withheld ? (
              <span className="br-chip" title="An admin withheld a message this title could repeat, so the title is hidden.">
                {FLAG_TEXT.title_withheld as string}
              </span>
            ) : null}
            <LabelChips labels={s.labels} />
          </div>
          {restricted ? (
            <div className="alert alert-warn br-withheld-session" role="note">
              {WITHHELD_SESSION}
            </div>
          ) : null}
          {d && d.hidden > 0 ? <p className="br-hidden small">{hiddenText(d.hidden)}</p> : null}
          {withheldNote ? (
            <p className="alert alert-good small" role="status">
              {withheldNote}
            </p>
          ) : null}
          {restricted ? null : !current ? (
            <p className="muted">Loading messages…</p>
          ) : current.messages.length ? (
            <>
              <ol className="br-messages">
                {current.messages.map((m) => (
                  <MessageRow
                    key={m.id}
                    m={m}
                    focused={m.id === focus}
                    canPromote={canOperate}
                    canWithhold={isAdmin}
                    onPromote={() => setPromote(m)}
                    onWithhold={() => setWithhold(m)}
                  />
                ))}
              </ol>
            </>
          ) : (
            <Empty>No messages of this conversation are visible to you.</Empty>
          )}
          {current && current.total > MESSAGE_PAGE ? (
            <div className="br-pager">
              <span className="small">
                Showing messages {shownFrom.toLocaleString()}–{shownTo.toLocaleString()} of {current.total.toLocaleString()}
              </span>
              <button
                className="btn small"
                type="button"
                disabled={offset === 0}
                onClick={() => {
                  located.current = true;
                  setOffset(Math.max(0, offset - MESSAGE_PAGE));
                }}
              >
                Previous
              </button>
              <button
                className="btn small"
                type="button"
                disabled={shownTo >= current.total}
                onClick={() => {
                  located.current = true;
                  setOffset(offset + MESSAGE_PAGE);
                }}
              >
                Next
              </button>
            </div>
          ) : null}
        </div>
      )}
      {promote && s ? <PromoteModal message={promote} session={s} onClose={() => setPromote(null)} onOpenItem={onOpenItem} onChanged={onChanged} /> : null}
      {withhold ? (
        <ConfirmModal
          title={`Withhold message #${withhold.position}`}
          action="Withhold this message"
          busy={busy || copies === undefined || preview === undefined}
          error={withholdError}
          onConfirm={() => void doWithhold(withhold)}
          onCancel={() => {
            setWithhold(null);
            setWithholdError(null);
          }}
        >
          {copies === undefined || preview === undefined ? (
            <p>Counting the memory items promoted from this message…</p>
          ) : (
            <>
              {withholdText(copies.count, copies.error).map((line) => (
                <p key={line}>{line}</p>
              ))}
              <WithholdChoices
                preview={preview}
                includeIdentical={includeIdentical}
                setIncludeIdentical={setIncludeIdentical}
                hideTitle={hideTitle}
                setHideTitle={setHideTitle}
                onOpenItem={onOpenItem}
              />
            </>
          )}
        </ConfirmModal>
      ) : null}
    </Modal>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Overview.

function Overview({ rev }: { rev: number }) {
  const { user, canOperate } = useAuth();
  const isAdmin = user.role === "admin";
  const ov = usePoll(() => api.get<BrainOverview>(`${BASE}/overview`), 30_000, [rev]);
  const d = ov.data;
  const failed = !d && !!ov.error;
  const latest = d?.imports?.latest ?? null;
  const runs = d?.imports?.runs ?? null;
  const noImport = runs === 0;
  const aiOn = d?.ai_context === "approved_general";

  return (
    <>
      <ErrorNote error={ov.error ? `Could not load the overview: ${ov.error}` : null} />
      <div className={`tiles${canOperate ? " br-tiles-6" : ""}`}>
        <Tile
          icon="brain"
          tone="blue"
          label="Memory items"
          hint={d ? breakdown(d.memory?.by_status) || "No memory items yet" : failed ? "Could not load" : "Loading…"}
        >
          <span className="tile-value tabular">{count(d?.memory?.total, failed || (!!d && !d.memory))}</span>
          {d && !canOperate ? <span className="tile-of">approved, visible to you</span> : null}
        </Tile>
        {canOperate ? (
          <Tile
            icon="approvals"
            tone={!d?.review ? "muted" : d.review.pending_items || d.review.quarantined_items ? "warn" : "green"}
            label="Waiting for review"
            to="/brain?tab=review"
            hint={
              d?.review && !isAdmin
                ? "Quarantined items are reviewed by admins (not counted for you)"
                : d?.review
                ? d.review.quarantined_items
                  ? `${plural(d.review.quarantined_items, "quarantined item")}: an admin must review ${d.review.quarantined_items === 1 ? "it" : "them"}`
                  : "No quarantined items"
                : d
                  ? "Not reported by the API"
                  : failed
                    ? "Could not load"
                    : "Loading…"
            }
          >
            <span className="tile-value tabular">{count(d?.review?.pending_items, failed || (!!d && !d.review))}</span>
            {d?.review ? <span className="tile-of">pending</span> : null}
          </Tile>
        ) : null}
        {canOperate ? (
          <Tile
            icon="alert"
            tone={!d?.review ? "muted" : d.review.open_conflicts ? "orange" : "green"}
            label="Open conflicts"
            to="/brain?tab=review"
            hint={
              d?.review
                ? d.review.open_conflicts
                  ? "An import found something a person must decide"
                  : "Nothing to decide"
                : d
                  ? "Not reported by the API"
                  : failed
                    ? "Could not load"
                    : "Loading…"
            }
          >
            <span className="tile-value tabular">{count(d?.review?.open_conflicts, failed || (!!d && !d.review))}</span>
          </Tile>
        ) : null}
        <Tile
          icon="chat"
          tone="cyan"
          label="Conversation archive"
          to={canOperate ? "/brain?tab=conversations" : undefined}
          hint={
            d?.archive ? (
              <>
                {plural(d.archive.messages ?? 0, "message")}
                {d.archive.withheld_messages ? ` · ${d.archive.withheld_messages.toLocaleString()} withheld` : ""}
                {d.archive.first_at && d.archive.last_at ? (
                  <>
                    {" "}
                    · <When iso={d.archive.first_at} dateOnly /> to <When iso={d.archive.last_at} dateOnly />
                  </>
                ) : null}
                {!canOperate ? " · totals only: conversations are read by operators and admins" : ""}
              </>
            ) : d ? (
              "Not reported by the API"
            ) : failed ? (
              "Could not load"
            ) : (
              "Loading…"
            )
          }
        >
          <span className="tile-value tabular">{count(d?.archive?.sessions, failed || (!!d && !d.archive))}</span>
          {d?.archive ? <span className="tile-of">{d.archive.sessions === 1 ? "conversation" : "conversations"}</span> : null}
        </Tile>
        {canOperate ? (
          <Tile
            icon="workflows"
            tone="purple"
            label="Workflow drafts"
            to="/brain?tab=workflows"
            hint={d?.workflows ? breakdown(d.workflows.by_status, (k) => DRAFT_WORDS[k] ?? words(k)) || "None imported" : d ? "Not reported by the API" : failed ? "Could not load" : "Loading…"}
          >
            <span className="tile-value tabular">{count(d?.workflows?.drafts, failed || (!!d && !d.workflows))}</span>
            {d?.workflows ? <span className="tile-of">never run; converting creates a disabled schedule</span> : null}
          </Tile>
        ) : null}
        <Tile
          icon="history"
          tone={latest?.status === "failed" ? "bad" : latest?.status === "completed" ? "green" : "muted"}
          label="Last import"
          to={canOperate ? "/brain?tab=imports" : undefined}
          hint={
            d
              ? latest
                ? `Run #${latest.id}${latest.label ? ` (${latest.label})` : ""} · ${plural(runs ?? 1, "run")} in all`
                : runs === 0
                  ? NO_IMPORT
                  : "Not reported by the API"
              : failed
                ? "Could not load"
                : "Loading…"
          }
        >
          {latest ? (
            <span className="br-tile-status">
              <RunStatus status={latest.status} /> <When iso={latest.finished_at ?? latest.started_at} />
            </span>
          ) : (
            <span className="tile-value">{runs === 0 ? "None" : d || failed ? "—" : "…"}</span>
          )}
        </Tile>
      </div>

      {noImport ? (
        <Card className="br-empty-card">
          <div className="br-empty-head">
            <span className="br-empty-icon" aria-hidden>
              <Icon name="brain" size={22} />
            </span>
            <div>
              <h2>{NO_IMPORT}</h2>
              <p className="muted">
                Nothing from JARVIS is in the Command Center yet: no knowledge, skills, jobs or conversations. The owner runs the import with{" "}
                <code>scripts\jarvis-import.ps1</code>, first on the test copy, in stages (a plan, a small sample, about 10 %, then everything), reviewing each
                stage here. See <Link to="/kb#jarvis-brain">Knowledge Base › The JARVIS Brain</Link>.
              </p>
            </div>
          </div>
        </Card>
      ) : null}

      <div className="grid-2 br-grid">
        <Card
          title={
            <>
              <Icon name="sparkles" size={18} className="card-icon" /> Memory in AI prompts
            </>
          }
        >
          {!d ? (
            <p className="muted">{failed ? "Not known: the overview did not load." : "Loading…"}</p>
          ) : (
            <>
              <p className="br-ai">
                <Pill value={aiOn ? "live" : "disabled"} label={aiOn ? "On" : "Off"} />{" "}
                {d.ai_context === "off" || !d.ai_context ? (
                  AI_OFF
                ) : aiOn ? (
                  "On: approved, general memory (business facts, shared knowledge, agent notes, project context) may be added to AI drafting prompts, marked as reference data."
                ) : (
                  `Setting: ${d.ai_context}`
                )}
              </p>
              <p className="muted small">
                Whatever the setting, customer data, operating instructions, skills, quarantined items and anything flagged never go into an AI prompt. Agents
                still read approved notes locally, as “Reference notes from memory” on their tasks; that needs no AI.
              </p>
              {user.role === "admin" ? (
                <Link className="btn small" to="/settings">
                  <Icon name="settings" size={14} /> Change in Settings
                </Link>
              ) : (
                <p className="muted small">An admin changes it on the Settings page (“JARVIS Brain in AI prompts”).</p>
              )}
            </>
          )}
        </Card>
        <Card
          title={
            <>
              <Icon name="history" size={18} className="card-icon" /> Latest import
            </>
          }
          actions={
            canOperate && runs ? (
              <Link className="small" to="/brain?tab=imports">
                Import history
              </Link>
            ) : null
          }
        >
          {!d ? (
            <p className="muted">{failed ? "Not known: the overview did not load." : "Loading…"}</p>
          ) : !latest ? (
            <Empty>{runs === 0 ? `${NO_IMPORT}.` : "The API did not report the latest import."}</Empty>
          ) : (
            <dl className="kv br-kv">
              <dt>Run</dt>
              <dd>
                #{latest.id} {latest.label ? <span className="muted">({latest.label})</span> : null} <RunStatus status={latest.status} />
              </dd>
              <dt>Started</dt>
              <dd>
                <When iso={latest.started_at} /> {latest.created_by ? `by ${latest.created_by}` : ""}
              </dd>
              <dt>Finished</dt>
              <dd>{latest.finished_at ? <When iso={latest.finished_at} /> : <span className="muted">not finished</span>}</dd>
              {latest.error ? (
                <>
                  <dt>Error</dt>
                  <dd className="text-bad">{latest.error}</dd>
                </>
              ) : null}
            </dl>
          )}
        </Card>
      </div>

      {d && (entries(d.memory?.by_kind).length || entries(d.archive?.by_staff).length) ? (
        <Card
          title={
            <>
              <Icon name="database" size={18} className="card-icon" /> What is kept
            </>
          }
        >
          <dl className="kv br-kv">
            {entries(d.memory?.by_kind).length ? (
              <>
                <dt>Memory by kind</dt>
                <dd>{breakdown(d.memory?.by_kind, (k) => kindLabel(k).toLowerCase())}</dd>
              </>
            ) : null}
            {entries(d.memory?.by_agent).length ? (
              <>
                <dt>Memory by agent</dt>
                <dd>{breakdown(d.memory?.by_agent, (k) => agentTitle(k))}</dd>
              </>
            ) : null}
            {entries(d.archive?.by_staff).length ? (
              <>
                <dt>Conversations by staff</dt>
                <dd>{breakdown(d.archive?.by_staff, staffName)}</dd>
              </>
            ) : null}
          </dl>
          <p className="muted small">Counts cover what you may see{user.role === "admin" ? "" : ", not what is kept for other roles"}.</p>
        </Card>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Search.

const SEARCH_LIMITS = [20, 50];

interface SearchForm {
  q: string;
  scope: "all" | "memory" | "archive";
  kind: string;
  agent: string;
  staff: string;
  label: string;
  since: string;
  until: string;
}
const EMPTY_SEARCH: SearchForm = { q: "", scope: "all", kind: "", agent: "", staff: "", label: "", since: "", until: "" };

function SearchTab({ rev, onOpenItem, onOpenMessage }: { rev: number; onOpenItem: (id: number) => void; onOpenMessage: (session: number, message: number) => void }) {
  const { canOperate } = useAuth();
  const [form, setForm] = useState<SearchForm>({ ...EMPTY_SEARCH, scope: canOperate ? "all" : "memory" });
  const [query, setQuery] = useState<SearchForm | null>(null);
  const [limit, setLimit] = useState(SEARCH_LIMITS[0]);
  const key = query ? JSON.stringify({ ...query, limit }) : "";
  const result = usePoll(
    () => {
      if (!query) return Promise.resolve(null);
      const scope = canOperate ? query.scope : "memory";
      const archive = scope !== "memory";
      return api
        .get<BrainSearch>(
          `${BASE}/search${qs({
            q: query.q.trim(),
            scope,
            kind: scope !== "archive" ? query.kind : "",
            agent: query.agent,
            staff: archive ? query.staff : "",
            label: archive ? query.label : "",
            since: archive ? query.since : "",
            until: archive ? query.until : "",
            limit,
          })}`,
        )
        .then((r) => ({ key, r }));
    },
    0,
    [key, rev],
  );
  const current = result.data && result.data.key === key ? result.data.r : null;
  const loading = !!query && !current && !result.error;
  const scope = canOperate ? (query?.scope ?? form.scope) : "memory";
  const set = (k: keyof SearchForm, v: string) => setForm((f) => ({ ...f, [k]: v }));

  function submit(e: FormEvent) {
    e.preventDefault();
    setQuery({ ...form });
  }

  return (
    <>
      <Card>
        <form className="br-search" onSubmit={submit} role="search" aria-label="Search the JARVIS Brain">
          <label className="br-search-q">
            <span className="sr-only">Search words</span>
            <Icon name="search" size={16} className="br-search-icon" />
            <input type="search" value={form.q} onChange={(e) => set("q", e.target.value)} placeholder={canOperate ? "Search memory and conversations" : "Search memory"} />
          </label>
          {canOperate ? (
            <label>
              <span className="sr-only">Where</span>
              <select aria-label="Where to search" value={form.scope} onChange={(e) => set("scope", e.target.value)}>
                <option value="all">Memory and conversations</option>
                <option value="memory">Memory only</option>
                <option value="archive">Conversations only</option>
              </select>
            </label>
          ) : null}
          <label>
            <span className="sr-only">Kind</span>
            <select aria-label="Kind" value={form.kind} onChange={(e) => set("kind", e.target.value)} disabled={form.scope === "archive"}>
              <option value="">Any kind</option>
              {(canOperate ? KINDS : BUSINESS_KINDS).map((k) => (
                <option key={k} value={k}>
                  {KIND_LABELS[k]}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span className="sr-only">Agent</span>
            <select aria-label="Agent" value={form.agent} onChange={(e) => set("agent", e.target.value)}>
              <option value="">Any agent</option>
              {Object.entries(AGENT_TITLES).map(([k, t]) => (
                <option key={k} value={k}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          {canOperate && form.scope !== "memory" ? (
            <>
              <label>
                <span className="sr-only">Staff</span>
                <select aria-label="JARVIS staff" value={form.staff} onChange={(e) => set("staff", e.target.value)}>
                  <option value="">Any staff</option>
                  {STAFF.map((s) => (
                    <option key={s} value={s}>
                      {staffName(s)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span className="sr-only">Label</span>
                <select aria-label="Label" value={form.label} onChange={(e) => set("label", e.target.value)}>
                  <option value="">Any label</option>
                  {HISTORY_LABELS.map((l) => (
                    <option key={l} value={l}>
                      {cap(l)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="br-date">
                From
                <input type="date" value={form.since} onChange={(e) => set("since", e.target.value)} />
              </label>
              <label className="br-date">
                To
                <input type="date" value={form.until} onChange={(e) => set("until", e.target.value)} />
              </label>
            </>
          ) : null}
          <label>
            <span className="sr-only">Results per section</span>
            <select aria-label="Results per section" value={limit} onChange={(e) => setLimit(Number(e.target.value))}>
              {SEARCH_LIMITS.map((n) => (
                <option key={n} value={n}>
                  Up to {n}
                </option>
              ))}
            </select>
          </label>
          <button className="btn primary" type="submit">
            <Icon name="search" size={15} /> Search
          </button>
        </form>
        {!canOperate ? <p className="muted small">You see approved business knowledge. Conversations are searched by operators and admins.</p> : null}
        {canOperate && form.scope !== "memory" ? <p className="muted small">Conversation labels come from keyword matching. Withheld messages are never searched.</p> : null}
      </Card>

      <ErrorNote error={result.error ? `Search failed: ${result.error}` : null} />
      {!query ? (
        <Empty>Type some words and press Search. With no words, the newest entries are listed.</Empty>
      ) : loading ? (
        <Empty>Searching…</Empty>
      ) : current ? (
        <>
          {query.q.trim() && !current.filtered ? (
            <p className="alert alert-info small">None of these words can be searched on (they are too common or too short), so the newest entries are listed instead.</p>
          ) : null}
          {scope !== "archive" ? (
            <Card
              className="br-results"
              title={
                <>
                  <Icon name="brain" size={18} className="card-icon" /> Memory
                </>
              }
              actions={<span className="small">{resultCount(current.memory.hits.length, current.memory.total, "item")}</span>}
            >
              {current.memory.hits.length ? (
                <ul className="br-hits">
                  {current.memory.hits.map((h) => (
                    <li key={h.id} className="br-hit">
                      <button type="button" className="br-hit-title link-button" onClick={() => onOpenItem(h.id)}>
                        {h.title}
                      </button>
                      <div className="br-hit-meta small">
                        <ItemStatus item={h} /> <span>{kindLabel(h.kind, h.kind_label)}</span>{" "}
                        <span className="muted">
                          · {agentTitle(h.agent)}
                          {h.source_ref ? <> · source {h.source_ref}</> : ` · ${ORIGIN[h.origin] ?? h.origin}`}
                        </span>
                        <FlagChips flags={h.flags} sensitivity={h.sensitivity} />
                      </div>
                      {h.excerpt ? <p className="br-excerpt">{h.excerpt}</p> : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>No memory items match{query.q.trim() ? ` “${query.q.trim()}”` : ""}.</Empty>
              )}
            </Card>
          ) : null}
          {canOperate && scope !== "memory" ? (
            <Card
              className="br-results"
              title={
                <>
                  <Icon name="chat" size={18} className="card-icon" /> Conversations
                </>
              }
              actions={<span className="small">{resultCount(current.archive.hits.length, current.archive.total, "message")}</span>}
            >
              {current.archive.hits.length ? (
                <ul className="br-hits">
                  {current.archive.hits.map((h) => (
                    <li key={h.message.id} className="br-hit">
                      <button type="button" className="br-hit-title link-button" onClick={() => onOpenMessage(h.session.id, h.message.id)}>
                        {h.session.title || "Untitled conversation"}
                      </button>
                      <div className="br-hit-meta small">
                        <span>{ROLE_LABEL[h.message.role] ?? cap(h.message.role)}</span>
                        <span className="muted">
                          · <When iso={h.message.at ?? h.session.started_at} /> · {staffName(h.session.staff)} → {agentTitle(h.session.agent)}
                        </span>
                        {h.message.sensitivity === "customer" ? <CustomerPill /> : null}
                        <LabelChips labels={h.message.labels} />
                      </div>
                      {h.excerpt ? <p className="br-excerpt">{h.excerpt}</p> : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>No conversation messages match{query.q.trim() ? ` “${query.q.trim()}”` : ""}.</Empty>
              )}
            </Card>
          ) : null}
        </>
      ) : null}
    </>
  );
}

/** "Showing 20 of 134 items", "3 items". */
function resultCount(shown: number, total: number, noun: string): string {
  return total > shown ? `Showing ${shown.toLocaleString()} of ${plural(total, noun)}` : plural(total, noun);
}

// ---------------------------------------------------------------------------------------------------------------------
// Conversations.

const SESSION_PAGE = 50;

interface SessionFilters {
  q: string;
  staff: string;
  agent: string;
  origin: string;
  label: string;
  since: string;
  until: string;
}
const NO_FILTERS: SessionFilters = { q: "", staff: "", agent: "", origin: "", label: "", since: "", until: "" };

function Conversations({ rev, onOpen }: { rev: number; onOpen: (id: number) => void }) {
  const [form, setForm] = useState<SessionFilters>(NO_FILTERS);
  const [applied, setApplied] = useState<SessionFilters>(NO_FILTERS);
  const [offset, setOffset] = useState(0);
  const key = JSON.stringify({ ...applied, offset });
  const list = usePoll(
    () => api.get<{ sessions: ArchiveSession[]; total: number }>(`${BASE}/sessions${qs({ ...applied, limit: SESSION_PAGE, offset })}`).then((r) => ({ key, r })),
    0,
    [key, rev],
  );
  const current = list.data && list.data.key === key ? list.data.r : null;
  const filtered = Object.values(applied).some(Boolean);
  const set = (k: keyof SessionFilters, v: string) => setForm((f) => ({ ...f, [k]: v }));

  function submit(e: FormEvent) {
    e.preventDefault();
    setOffset(0);
    setApplied({ ...form });
  }
  function clear() {
    setForm(NO_FILTERS);
    setApplied(NO_FILTERS);
    setOffset(0);
  }

  const from = current && current.sessions.length ? offset + 1 : 0;
  const to = current ? offset + current.sessions.length : 0;
  return (
    <Card
      title={
        <>
          <Icon name="chat" size={18} className="card-icon" /> JARVIS's conversations
        </>
      }
      actions={
        current ? (
          <span className="small" role="status">
            {current.total ? `Showing ${from.toLocaleString()}–${to.toLocaleString()} of ${plural(current.total, "conversation")}` : "No conversations"}
          </span>
        ) : null
      }
    >
      <form className="br-filters" onSubmit={submit} aria-label="Filter conversations">
        <label className="br-search-q">
          <span className="sr-only">Words in the title or summary</span>
          <Icon name="search" size={16} className="br-search-icon" />
          <input type="search" value={form.q} onChange={(e) => set("q", e.target.value)} placeholder="Words in the title or summary" />
        </label>
        <select aria-label="JARVIS staff" value={form.staff} onChange={(e) => set("staff", e.target.value)}>
          <option value="">Any staff</option>
          {STAFF.map((s) => (
            <option key={s} value={s}>
              {staffName(s)}
            </option>
          ))}
        </select>
        <select aria-label="Agent" value={form.agent} onChange={(e) => set("agent", e.target.value)}>
          <option value="">Any agent</option>
          {Object.entries(AGENT_TITLES).map(([k, t]) => (
            <option key={k} value={k}>
              {t}
            </option>
          ))}
        </select>
        <select aria-label="Origin" value={form.origin} onChange={(e) => set("origin", e.target.value)}>
          <option value="">Chats and jobs</option>
          <option value="chat">Chats</option>
          <option value="scheduled">Scheduled jobs</option>
        </select>
        <select aria-label="Label" value={form.label} onChange={(e) => set("label", e.target.value)}>
          <option value="">Any label</option>
          {HISTORY_LABELS.map((l) => (
            <option key={l} value={l}>
              {cap(l)}
            </option>
          ))}
        </select>
        <label className="br-date">
          From
          <input type="date" value={form.since} onChange={(e) => set("since", e.target.value)} />
        </label>
        <label className="br-date">
          To
          <input type="date" value={form.until} onChange={(e) => set("until", e.target.value)} />
        </label>
        <button className="btn primary" type="submit">
          Apply
        </button>
        {filtered ? (
          <button className="btn ghost" type="button" onClick={clear}>
            <Icon name="close" size={14} /> Clear filters
          </button>
        ) : null}
      </form>
      <p className="muted small">
        Staff are inferred from scheduled-job ids (everything else counts as Jarvis), and labels from keyword matching. Customer conversations are shown to admins
        only; a conversation that held restricted data is withheld.
      </p>
      <ErrorNote error={list.error ? `Could not load conversations: ${list.error}` : null} />
      {!current ? (
        list.error ? null : <Empty>Loading conversations…</Empty>
      ) : !current.sessions.length ? (
        <Empty>{filtered ? "No conversations match these filters." : "No JARVIS conversations have been imported, or none are visible to you."}</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table br-table br-sessions">
            <thead>
              <tr>
                <th scope="col">Started</th>
                <th scope="col">Conversation</th>
                <th scope="col">Staff</th>
                <th scope="col">Messages</th>
                <th scope="col">Labels</th>
              </tr>
            </thead>
            <tbody>
              {current.sessions.map((s) => {
                const restricted = s.sensitivity === "restricted";
                return (
                  <tr key={s.id}>
                    <td className="br-c-when" data-label="Started">
                      <When iso={s.started_at} />
                    </td>
                    <td className="br-c-main">
                      <button type="button" className="link-button br-row-title" onClick={() => onOpen(s.id)}>
                        {s.title || (restricted ? "Withheld conversation" : "Untitled conversation")}
                      </button>
                      <div className="muted small">
                        {s.origin === "scheduled" ? `Scheduled job${s.job_name ? ` ${s.job_name}` : ""}` : "Chat"}
                        {s.channel ? ` · ${s.channel}` : ""}
                        {s.lineage ? ` · ${words(s.lineage)}` : ""}
                      </div>
                      {restricted ? <div className="br-withheld small">{WITHHELD_SESSION}</div> : s.summary ? <div className="br-summary small">{s.summary}</div> : null}
                      {s.sensitivity === "customer" ? <CustomerPill /> : null}
                    </td>
                    <td data-label="Staff">
                      {staffName(s.staff)}
                      <div className="muted small">{agentTitle(s.agent)}</div>
                      {attribution(s) ? <div className="br-inferred">{attribution(s)}</div> : null}
                    </td>
                    <td className="tabular" data-label="Messages">
                      {s.imported_messages.toLocaleString()} <span className="muted small">of {s.source_message_count.toLocaleString()}</span>
                      {s.withheld_messages ? <div className="muted small">{s.withheld_messages.toLocaleString()} withheld</div> : null}
                    </td>
                    <td data-label="Labels">
                      {entries(s.labels).some(([, n]) => n > 0) ? <LabelChips labels={s.labels} /> : <span className="muted">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {current && current.total > SESSION_PAGE ? (
        <div className="br-pager">
          <button className="btn small" type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - SESSION_PAGE))}>
            Previous
          </button>
          <button className="btn small" type="button" disabled={to >= current.total} onClick={() => setOffset(offset + SESSION_PAGE)}>
            Next
          </button>
        </div>
      ) : null}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Agent knowledge.

const ARCHIVE_SCOPE: Record<string, string> = {
  all: "every imported conversation except withheld ones",
  own: "the conversations of the JARVIS staff mapped to it",
  none: "none",
};

function AgentKnowledge({ rev }: { rev: number }) {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const list = usePoll(() => api.get<{ agents: BrainAgent[] }>(`${BASE}/agents`), 0, [rev]);
  const agents = list.data?.agents ?? null;
  return (
    <>
      <p className="muted br-lead">
        What each agent may read from memory. Agents read approved items only, never quarantined, rejected or deleted ones, and reading changes nothing they
        are allowed to do: every approval is still decided by the action's risk.
      </p>
      <ErrorNote error={list.error ? `Could not load the agents: ${list.error}` : null} />
      {!agents ? (
        list.error ? null : <Empty>Loading agents…</Empty>
      ) : !agents.length ? (
        <Empty>The API listed no agents.</Empty>
      ) : (
        <div className="br-agents">
          {agents.map((a) => (
            <Card key={a.agent} className="br-agent" title={a.title || agentTitle(a.agent)}>
              <ul className="br-reads" aria-label={`What ${a.title || agentTitle(a.agent)} reads`}>
                <Reads on={a.reads_shared}>Shared knowledge</Reads>
                <Reads on={a.reads_own}>Its own notes</Reads>
                <Reads on={a.reads_other_agents}>Other agents' notes (never Credit Repair's)</Reads>
                <Reads on={a.reads_customer_items}>Customer items (contact details masked; never sent to an AI model)</Reads>
                <Reads on={a.archive !== "none"}>Conversation archive: {ARCHIVE_SCOPE[a.archive] ?? a.archive}</Reads>
              </ul>
              <p className="muted small">Statuses read: {(a.statuses ?? ["approved"]).map(words).join(", ")}.</p>
              <h3>Where it uses memory</h3>
              {a.used_by?.length ? (
                <ul className="br-used">
                  {a.used_by.map((u) => (
                    <li key={u}>{u}</li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">Not used: nothing this agent does reads memory today.</p>
              )}
              {a.items ? (
                <p className="small br-agent-counts">
                  <strong className="tabular">{a.items.approved.toLocaleString()}</strong> approved {a.items.approved === 1 ? "item" : "items"} it reads
                  {isAdmin && user.can_access_sensitive ? "" : " (of those visible to you)"}
                  {entries(a.items.by_kind).length ? <span className="muted"> ({breakdown(a.items.by_kind, (k) => kindLabel(k).toLowerCase())})</span> : null}
                  {a.sessions != null ? (
                    <>
                      {" "}
                      · <strong className="tabular">{a.sessions.toLocaleString()}</strong> {a.sessions === 1 ? "conversation" : "conversations"}
                    </>
                  ) : null}
                </p>
              ) : (
                <p className="muted small">Counts are not shown to you.</p>
              )}
            </Card>
          ))}
        </div>
      )}
    </>
  );
}

function Reads({ on, children }: { on: boolean; children: ReactNode }) {
  return (
    <li className={on ? "br-yes" : "br-no"}>
      <Icon name={on ? "check" : "close"} size={14} />
      <span className="sr-only">{on ? "Reads: " : "Does not read: "}</span>
      {children}
    </li>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Skills.

function Skills({ rev, onOpenItem }: { rev: number; onOpenItem: (id: number) => void }) {
  const { canOperate } = useAuth();
  const list = usePoll(() => api.get<{ skills: MemoryItem[] }>(`${BASE}/skills`), 0, [rev]);
  const skills = list.data?.skills ?? null;
  return (
    <Card
      title={
        <>
          <Icon name="book" size={18} className="card-icon" /> JARVIS's skills
        </>
      }
      actions={skills ? <span className="small">{plural(skills.length, "skill")}</span> : null}
    >
      <p className="muted small">
        Each JARVIS skill and the Command Center capabilities that do the same job. Skills are kept as text for reference: nothing here runs, and scripts,
        plugins and hooks are never executed.
      </p>
      <ErrorNote error={list.error ? `Could not load the skills: ${list.error}` : null} />
      {!skills ? (
        list.error ? null : <Empty>Loading skills…</Empty>
      ) : !skills.length ? (
        <Empty>{canOperate ? "No skill references have been imported." : "Skill references are reviewed by operators and admins; viewers see approved business knowledge only."}</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table br-table br-skills">
            <thead>
              <tr>
                <th scope="col">Skill</th>
                <th scope="col">JARVIS staff → agent</th>
                <th scope="col">Capabilities</th>
                <th scope="col">Compatibility</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {skills.map((s) => {
                const f = s.flags ?? {};
                const fm = (f.frontmatter ?? {}) as { name?: string; description?: string; version?: string };
                const caps = Array.isArray(f.capabilities) ? (f.capabilities as unknown[]).map(String) : [];
                const name = fm.name || (typeof f.skill === "string" ? f.skill : "") || s.title;
                return (
                  <tr key={s.id}>
                    <td className="br-c-main">
                      <button type="button" className="link-button br-row-title" onClick={() => onOpenItem(s.id)}>
                        {name}
                      </button>
                      {fm.description ? <div className="muted small">{fm.description}</div> : null}
                      {typeof f.note === "string" && f.note ? <div className="small br-note">{f.note}</div> : null}
                      <FlagChips flags={f} sensitivity={s.sensitivity} />
                    </td>
                    <td data-label="Staff → agent">
                      {typeof f.staff === "string" && f.staff ? staffName(f.staff) : "—"}
                      <div className="muted small">{agentTitle(s.agent)}</div>
                    </td>
                    <td data-label="Capabilities">
                      {caps.length ? (
                        caps.map((c) => (
                          <code key={c} className="br-cap">
                            {c}
                          </code>
                        ))
                      ) : (
                        <span className="muted">None</span>
                      )}
                    </td>
                    <td data-label="Compatibility">
                      <Compat value={typeof f.compatibility === "string" ? f.compatibility : undefined} />
                    </td>
                    <td data-label="Status">
                      <ItemStatus item={s} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Workflows: JARVIS's scheduled jobs as drafts. A draft never runs; nothing on this tab can enable anything.

function overlapText(v: unknown): string {
  if (!v) return "";
  return Array.isArray(v) ? v.map(String).join(" ") : String(v);
}

function DraftModal({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const detail = usePoll(() => api.get<{ draft: WorkflowDraft; schedule_exists: boolean }>(`${BASE}/workflows/${id}`), 0, [id]);
  const [note, setNote] = useState("");
  const [off, setOff] = useState(false);
  const [cron, setCron] = useState<string | null>(null);
  const [params, setParams] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [converted, setConverted] = useState<Schedule | null>(null);
  const d = detail.data?.draft;

  async function act(fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await detail.refresh();
      onChanged();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  async function saveEdit() {
    if (!d) return;
    const body: Record<string, unknown> = {};
    if (cron !== null && cron.trim() !== d.cron) body.cron = cron.trim();
    if (params !== null) {
      try {
        body.params = JSON.parse(params || "{}");
      } catch {
        setError("Parameters must be valid JSON.");
        return;
      }
    }
    if (!Object.keys(body).length) {
      setError("Nothing was changed.");
      return;
    }
    await act(async () => {
      await api.patch(`${BASE}/workflows/${id}`, body);
      setCron(null);
      setParams(null);
    });
  }

  async function convert() {
    await act(async () => {
      const r = await api.post<{ draft: WorkflowDraft; schedule: Schedule }>(`${BASE}/workflows/${id}/convert`, { jarvis_job_is_off: off });
      setConverted(r.schedule);
    });
  }

  const blockers: string[] = [];
  if (d) {
    if (d.compatibility === "reference_only") blockers.push("No Command Center capability does this job: it stays a reference.");
    if (d.status === "converted") blockers.push("Already converted.");
    else if (d.status !== "reviewed") blockers.push("Mark the draft reviewed first.");
    if (!d.cron) blockers.push("No valid cron expression: set one first.");
    if (d.enabled_in_source && !off) blockers.push("Confirm that the JARVIS job is switched off.");
  }
  const hint = d?.cron ? cronHint(d.cron) : null;
  const history = d?.history ?? {};
  return (
    <Modal title={d ? `JARVIS job ${d.name}` : `Workflow draft #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error} />
      {!d ? (
        detail.error ? null : <p className="muted">Loading…</p>
      ) : (
        <div className="br-draft">
          <p className="br-item-meta">
            <DraftStatus status={d.status} /> <Compat value={d.compatibility} />{" "}
            <span className="muted">
              · {staffName(d.staff)} → {agentTitle(d.agent)}
            </span>
          </p>
          <p className="alert alert-info small">
            A draft never runs. {d.status === "converted" ? "It was converted into a schedule that was created disabled." : "Converting it (an admin) creates a Command Center schedule that is created disabled."}
          </p>
          <dl className="kv br-kv">
            <dt>Would run</dt>
            <dd>
              {d.capability && d.compatibility !== "reference_only" ? <code>{`${d.agent}.${d.capability}`}</code> : <span className="muted">nothing (reference only)</span>}
              {Object.keys(d.params ?? {}).length ? <span className="muted small"> with {JSON.stringify(d.params)}</span> : null}
            </dd>
            <dt>Trigger</dt>
            <dd>{cap(d.trigger)}</dd>
            <dt>Schedule</dt>
            <dd>
              {d.cron ? <code>{d.cron}</code> : <span className="text-warn">no valid cron</span>}
              {d.schedule_display ? <span className="muted"> · in JARVIS: {d.schedule_display}</span> : null}
              <div className="muted small">
                JARVIS ran cron in the PC's local time{d.timezone ? ` (${d.timezone})` : ""}; Command Center schedules run in UTC.
                {hint ? ` As a schedule, this cron would run: ${hint}.` : ""} Adjust the cron before converting if the time matters.
              </div>
            </dd>
            <dt>In JARVIS</dt>
            <dd>{d.enabled_in_source ? <span className="text-warn">switched on in JARVIS</span> : "switched off in JARVIS"}</dd>
            {d.deliver ? (
              <>
                <dt>JARVIS delivered to</dt>
                <dd>{d.deliver} (delivery is not part of the draft)</dd>
              </>
            ) : null}
            <dt>Approval</dt>
            <dd>
              {d.approval?.command_center ? <div>Command Center: {d.approval.command_center}</div> : null}
              {d.approval?.jarvis ? <div className="muted small">JARVIS: {d.approval.jarvis}</div> : null}
              {d.approval?.jarvis_rules?.length ? (
                <div className="muted small">JARVIS's instructions for it matched: {d.approval.jarvis_rules.map(words).join(", ")} (reference only; the Command Center decides approvals)</div>
              ) : null}
            </dd>
            <dt>Error handling</dt>
            <dd>
              {d.error_handling?.command_center ? <div>Command Center: {d.error_handling.command_center}</div> : null}
              {d.error_handling?.jarvis?.length ? (
                <ul className="br-list small">
                  {d.error_handling.jarvis.map((l, i) => (
                    <li key={i}>JARVIS: {l}</li>
                  ))}
                </ul>
              ) : null}
            </dd>
            <dt>Duplicate guard</dt>
            <dd>
              {d.duplicate_guard?.rule ? <div>{d.duplicate_guard.rule}</div> : null}
              {overlapText(d.duplicate_guard?.overlaps) ? <div className="text-warn small">Overlaps: {overlapText(d.duplicate_guard?.overlaps)}</div> : null}
            </dd>
            <dt>History</dt>
            <dd>
              {typeof history.runs === "number" ? (
                <>
                  {plural(history.runs, "run")}
                  {history.first && history.last ? (
                    <>
                      {" "}
                      from <When iso={history.first} dateOnly /> to <When iso={history.last} dateOnly />
                    </>
                  ) : null}
                  {typeof history.with_problems === "number" ? ` · ${history.with_problems.toLocaleString()} with problems (matched by keywords)` : ""}
                  <div className="muted small">From {history.from || "the JARVIS backup"}.</div>
                  {history.last_runs?.length ? (
                    <div className="muted small">
                      Last runs:{" "}
                      {history.last_runs.map((r, i) => (
                        <span key={i}>
                          {i ? ", " : ""}
                          <When iso={r} />
                        </span>
                      ))}
                    </div>
                  ) : null}
                </>
              ) : (
                <span className="muted">No history recorded.</span>
              )}
            </dd>
            {d.notes?.length ? (
              <>
                <dt>Notes</dt>
                <dd>
                  <ul className="br-list">
                    {d.notes.map((n, i) => (
                      <li key={i}>{n}</li>
                    ))}
                  </ul>
                </dd>
              </>
            ) : null}
            {d.status === "converted" ? (
              <>
                <dt>Schedule</dt>
                <dd>
                  {d.schedule_id ? `#${d.schedule_id} ` : ""}
                  {detail.data?.schedule_exists ? (
                    <Link to="/schedules">open Schedules</Link>
                  ) : (
                    <span className="text-warn">the schedule no longer exists</span>
                  )}
                </dd>
              </>
            ) : null}
            {d.reviewed_at ? (
              <>
                <dt>Reviewed</dt>
                <dd>
                  <When iso={d.reviewed_at} /> by {d.reviewed_by || "unknown"}
                </dd>
              </>
            ) : null}
          </dl>
          {d.spec ? (
            <details className="br-details">
              <summary>JARVIS's prompt and skill text</summary>
              <pre className="br-text">{d.spec}</pre>
            </details>
          ) : null}

          {d.status !== "converted" ? (
            <section className="br-actions" aria-label="Review the draft">
              <h3>Review</h3>
              <div className="row-actions">
                <input aria-label="Review note" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} />
                {d.status !== "reviewed" ? (
                  <button className="btn small primary" type="button" disabled={busy} onClick={() => void act(() => api.post(`${BASE}/workflows/${id}/review`, { decision: "reviewed", note }))}>
                    Mark reviewed
                  </button>
                ) : null}
                {d.status !== "rejected" ? (
                  <button className="btn small" type="button" disabled={busy} onClick={() => void act(() => api.post(`${BASE}/workflows/${id}/review`, { decision: "rejected", note }))}>
                    Reject
                  </button>
                ) : null}
                {d.status !== "draft" ? (
                  <button className="btn small" type="button" disabled={busy} onClick={() => void act(() => api.post(`${BASE}/workflows/${id}/review`, { decision: "draft", note }))}>
                    Back to draft
                  </button>
                ) : null}
              </div>
            </section>
          ) : null}

          {isAdmin && d.status !== "converted" ? (
            <section className="br-actions" aria-label="Edit the draft">
              <h3>Edit (admin)</h3>
              <div className="br-edit-row">
                <label>
                  Cron (UTC once converted)
                  <input className="mono" value={cron ?? d.cron} onChange={(e) => setCron(e.target.value)} />
                </label>
                <label>
                  Parameters (JSON)
                  <textarea className="mono" rows={3} value={params ?? JSON.stringify(d.params ?? {}, null, 2)} onChange={(e) => setParams(e.target.value)} />
                </label>
              </div>
              <button className="btn small" type="button" disabled={busy || (cron === null && params === null)} onClick={() => void saveEdit()}>
                Save cron and parameters
              </button>
            </section>
          ) : null}

          {d.status !== "converted" ? (
            <section className="br-actions br-convert" aria-label="Convert to a schedule">
              <h3>Convert to a schedule</h3>
              <p className="small">
                Creates the schedule <code>jarvis-{d.name}</code>, <strong>disabled</strong>. It runs nothing until someone enables it on Schedules. Switch the JARVIS
                job off before enabling it, so the work is not done twice.
              </p>
              {isAdmin ? (
                <>
                  {d.enabled_in_source ? (
                    <label className="check">
                      <input type="checkbox" checked={off} onChange={(e) => setOff(e.target.checked)} />
                      The JARVIS job is switched off
                    </label>
                  ) : null}
                  {blockers.length ? (
                    <ul className="br-list muted small">
                      {blockers.map((b) => (
                        <li key={b}>{b}</li>
                      ))}
                    </ul>
                  ) : null}
                  <button className="btn small primary" type="button" disabled={busy || blockers.length > 0} onClick={() => void convert()}>
                    Create disabled schedule
                  </button>
                </>
              ) : (
                <p className="br-locked small">
                  <Icon name="lock" size={14} /> Only an admin can convert a draft.
                </p>
              )}
            </section>
          ) : null}
          {converted ? (
            <div className="alert alert-good">
              Schedule <code>{converted.name}</code> (#{converted.id}) created, disabled. <Link to="/schedules">Open Schedules</Link>
            </div>
          ) : null}
          <ErrorNote error={error} />
        </div>
      )}
    </Modal>
  );
}

function Workflows({ rev, openDraft }: { rev: number; openDraft: (id: number) => void }) {
  const list = usePoll(() => api.get<{ drafts: WorkflowDraft[] }>(`${BASE}/workflows`), 0, [rev]);
  const drafts = list.data?.drafts ?? null;
  return (
    <Card
      title={
        <>
          <Icon name="workflows" size={18} className="card-icon" /> JARVIS's scheduled jobs
        </>
      }
      actions={drafts ? <span className="small">{plural(drafts.length, "draft")}</span> : null}
    >
      <p className="muted small">
        Each job is kept as a draft, and every draft is off: it never runs. An admin can turn a reviewed draft into a Command Center schedule, which is created
        disabled. JARVIS's times were in the PC's local time; Command Center schedules run in UTC.
      </p>
      <ErrorNote error={list.error ? `Could not load the drafts: ${list.error}` : null} />
      {!drafts ? (
        list.error ? null : <Empty>Loading drafts…</Empty>
      ) : !drafts.length ? (
        <Empty>No JARVIS jobs have been imported.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table br-table br-drafts">
            <thead>
              <tr>
                <th scope="col">Job</th>
                <th scope="col">Would run</th>
                <th scope="col">JARVIS schedule</th>
                <th scope="col">In JARVIS</th>
                <th scope="col">Compatibility</th>
                <th scope="col">Draft</th>
              </tr>
            </thead>
            <tbody>
              {drafts.map((d) => (
                <tr key={d.id}>
                  <td className="br-c-main">
                    <button type="button" className="link-button br-row-title" onClick={() => openDraft(d.id)}>
                      {d.name}
                    </button>
                    <div className="muted small">
                      {staffName(d.staff)} → {agentTitle(d.agent)}
                    </div>
                  </td>
                  <td data-label="Would run">
                    {d.capability && d.compatibility !== "reference_only" ? <code className="br-cap">{d.capability}</code> : <span className="muted">nothing (reference only)</span>}
                  </td>
                  <td data-label="JARVIS schedule">
                    {d.cron ? <code>{d.cron}</code> : <span className="text-warn small">no valid cron</span>}
                    {d.schedule_display ? <div className="muted small">{d.schedule_display}</div> : null}
                  </td>
                  <td data-label="In JARVIS">{d.enabled_in_source ? <span className="text-warn">on</span> : <span className="muted">off</span>}</td>
                  <td data-label="Compatibility">
                    <Compat value={d.compatibility} />
                  </td>
                  <td data-label="Draft">
                    <DraftStatus status={d.status} />
                    <div className="muted small">{d.status === "converted" ? "schedule created disabled" : "never runs"}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Review: quarantined (admins) and pending items, and open conflicts.

const REVIEW_LIMIT = 100;
const ADMIN_CONFLICTS = new Set<ConflictKind>(["instruction", "source_changed", "workflow"]);

const CONFLICT_KIND: Record<ConflictKind, [string, string]> = {
  memory_item: ["Same title, different text", "An imported item has the same kind, agent and title as an existing one, with different text."],
  source_changed: ["Changed source file", "A JARVIS file imported before has changed. The existing item is untouched until you decide."],
  instruction: ["JARVIS rule that bypasses approval", "A JARVIS instruction tells it to act without asking. In the Command Center it can only ever be reference text."],
  schedule: ["Overlaps an automation", "A JARVIS job does the same work as an existing Command Center automation."],
  site: ["Website JARVIS watched", "JARVIS watched a website that is not registered in the Command Center."],
  workflow: ["Changed JARVIS job", "A JARVIS job imported before has changed. The draft is untouched until you decide."],
};

const RESOLUTION_LABEL: Record<ConflictResolution, string> = {
  keep_existing: "Keep existing",
  use_incoming: "Use incoming",
  keep_both: "Keep both",
  dismissed: "Dismiss",
};

/** What each resolution does, in plain English (spec section 3, conflict resolutions). */
function resolutionText(kind: ConflictKind, r: ConflictResolution): string {
  switch (r) {
    case "keep_existing":
      if (kind === "source_changed") return "Keep the current item; the changed file is rejected and is not offered again.";
      if (kind === "site") return "Do not register the site.";
      if (kind === "workflow") return "Keep the current draft; the changed job is ignored.";
      if (kind === "schedule") return "Keep the existing automation; the JARVIS draft is rejected (it can be reopened on Workflows).";
      return "Keep what the Command Center has; the incoming item is rejected.";
    case "use_incoming":
      if (kind === "memory_item") return "Archive the existing item; the incoming item stays pending for review.";
      if (kind === "source_changed") return "The existing item takes the changed file's title and text and goes back to review; the separate incoming copy is deleted.";
      if (kind === "instruction") return "Keep the JARVIS rule as a pending reference item. It never becomes a rule: approvals stay with the Command Center.";
      if (kind === "site") return "Register the site paused (not monitored until someone enables it). It counts toward your edition's site limit.";
      if (kind === "workflow") return "Replace the draft with the changed job (only while the draft is not converted).";
      return "Use the incoming version.";
    case "keep_both":
      return "Keep both; nothing changes.";
    case "dismissed":
      return "Dismiss: nothing changes, and the question is not asked again.";
  }
}

function resolutionsFor(kind: ConflictKind): ConflictResolution[] {
  return kind === "schedule" ? ["keep_existing", "keep_both", "dismissed"] : ["keep_existing", "use_incoming", "keep_both", "dismissed"];
}

function ConflictCard({ c, onOpenItem, onResolved }: { c: MemoryConflict; onOpenItem: (id: number) => void; onResolved: () => void }) {
  const { user } = useAuth();
  const locked = ADMIN_CONFLICTS.has(c.kind) && user.role !== "admin";
  // Keeping one side changes an item whose approval needs an admin: keep both and dismiss stay open to operators.
  const adminOnly = !locked && !!c.needs_admin && user.role !== "admin";
  const [choice, setChoice] = useState<ConflictResolution | "">("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [label, help] = CONFLICT_KIND[c.kind] ?? [words(c.kind), ""];

  async function resolve(e: FormEvent) {
    e.preventDefault();
    if (!choice) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`${BASE}/conflicts/${c.id}/resolve`, { resolution: choice, note });
      onResolved();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(false);
    }
  }

  const existingItem = c.existing_table === "memory_items" && c.existing_id ? c.existing_id : null;
  return (
    <li className="br-conflict">
      <div className="br-conflict-head">
        <span className="br-chip br-chip-risk">{label}</span>
        <strong>{c.title}</strong>
        <span className="muted small">
          · found <When iso={c.created_at} />
          {c.run_id ? ` by run #${c.run_id}` : ""}
        </span>
      </div>
      {help ? <p className="muted small">{help}</p> : null}
      <div className="row-actions">
        {existingItem ? (
          <button type="button" className="btn small" onClick={() => onOpenItem(existingItem)}>
            Existing item #{existingItem}
          </button>
        ) : c.existing_table && c.existing_id ? (
          <span className="small muted">
            Existing: {words(c.existing_table)} #{c.existing_id}
          </span>
        ) : null}
        {c.incoming_item_id ? (
          <button type="button" className="btn small" onClick={() => onOpenItem(c.incoming_item_id as number)}>
            Incoming item #{c.incoming_item_id}
          </button>
        ) : null}
      </div>
      {Object.keys(c.existing ?? {}).length || Object.keys(c.incoming ?? {}).length ? (
        <details className="br-details">
          <summary>What each side holds</summary>
          <dl className="kv br-kv small">
            <dt>Existing</dt>
            <dd>{brief(c.existing)}</dd>
            <dt>Incoming</dt>
            <dd>{brief(c.incoming)}</dd>
          </dl>
        </details>
      ) : null}
      <form onSubmit={resolve}>
        {locked ? (
          <p className="br-locked small">
            <Icon name="lock" size={14} /> Only an admin can resolve this kind of conflict (JARVIS rules, changed source files and changed jobs).
          </p>
        ) : null}
        {adminOnly ? (
          <p className="br-locked small">
            <Icon name="lock" size={14} /> Keep existing and Use incoming change an item only an admin may approve: an admin decides those. Keep both and Dismiss
            are open to you.
          </p>
        ) : null}
        <fieldset className="br-choices" disabled={locked || busy}>
          <legend className="sr-only">Resolution for conflict {c.id}</legend>
          {resolutionsFor(c.kind).map((r) => (
            <label key={r} className="br-choice">
              <input
                type="radio"
                name={`conflict-${c.id}`}
                value={r}
                checked={choice === r}
                onChange={() => setChoice(r)}
                disabled={adminOnly && (r === "keep_existing" || r === "use_incoming")}
              />
              <span>
                <strong>{RESOLUTION_LABEL[r]}</strong> <span className="muted small">{resolutionText(c.kind, r)}</span>
              </span>
            </label>
          ))}
        </fieldset>
        <div className="row-actions">
          <input aria-label={`Note for conflict ${c.id}`} placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000} disabled={locked} />
          <button className="btn small primary" type="submit" disabled={locked || busy || !choice}>
            Resolve
          </button>
        </div>
      </form>
      <ErrorNote error={error} />
    </li>
  );
}

function ReviewList({
  title,
  icon,
  status,
  rev,
  intro,
  empty,
  onOpenItem,
  onChanged,
}: {
  title: string;
  icon: IconName;
  status: "quarantined" | "pending_review";
  rev: number;
  intro: ReactNode;
  empty: string;
  onOpenItem: (id: number) => void;
  onChanged: () => void;
}) {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const list = usePoll(() => api.get<{ items: MemoryItem[]; total: number }>(`${BASE}/items${qs({ status, limit: REVIEW_LIMIT })}`), 0, [rev]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const items = list.data?.items ?? null;

  async function decide(item: MemoryItem, decision: "approve" | "reject") {
    setBusy(item.id);
    setError(null);
    try {
      await api.post(`${BASE}/items/${item.id}/review`, { decision, note: "" });
      onChanged();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title={
        <>
          <Icon name={icon} size={18} className="card-icon" /> {title}
        </>
      }
      actions={list.data ? <span className="small">{resultCount(list.data.items.length, list.data.total, "item")}</span> : null}
    >
      {intro}
      <ErrorNote error={list.error ? `Could not load the items: ${list.error}` : error} />
      {!items ? (
        list.error ? null : <Empty>Loading…</Empty>
      ) : !items.length ? (
        <Empty>{empty}</Empty>
      ) : (
        <ul className="br-hits">
          {items.map((it) => {
            const locked = it.needs_admin && !isAdmin;
            return (
              <li key={it.id} className="br-hit br-review-row">
                <div className="br-review-main">
                  <button type="button" className="br-hit-title link-button" onClick={() => onOpenItem(it.id)}>
                    {it.title}
                  </button>
                  <div className="br-hit-meta small">
                    <ItemStatus item={it} /> <span>{kindLabel(it.kind, it.kind_label)}</span>
                    <span className="muted">
                      · {agentTitle(it.agent)} · {ORIGIN[it.origin] ?? it.origin}
                      {it.source_ref ? ` · ${it.source_ref}` : ""}
                    </span>
                    <FlagChips flags={it.flags} sensitivity={it.sensitivity} />
                    {it.needs_admin ? <span className="br-chip br-chip-admin">needs an admin</span> : null}
                  </div>
                  {it.excerpt ? <p className="br-excerpt">{it.excerpt}</p> : null}
                </div>
                <div className="row-actions br-review-actions">
                  <button
                    type="button"
                    className="btn small primary"
                    disabled={locked || busy === it.id || !!it.flags?.source_withheld}
                    title={
                      it.flags?.source_withheld
                        ? emptiedCopy(it)
                          ? SOURCE_WITHHELD_TEXT
                          : SOURCE_WITHHELD_PENDING_TEXT
                        : locked
                          ? `Needs an admin: ${adminReason(it)}`
                          : undefined
                    }
                    aria-label={`Approve ${it.title}`}
                    onClick={() => void decide(it, "approve")}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="btn small"
                    disabled={locked || busy === it.id}
                    title={locked ? `Needs an admin: ${adminReason(it)}` : undefined}
                    aria-label={`Reject ${it.title}`}
                    onClick={() => void decide(it, "reject")}
                  >
                    Reject
                  </button>
                  <button type="button" className="btn small ghost" onClick={() => onOpenItem(it.id)} aria-label={`Open ${it.title}`}>
                    Open
                  </button>
                </div>
                {locked ? <p className="br-locked small">Needs an admin: {adminReason(it)}.</p> : null}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

const DELETED_WINDOW = 200;

/** Admins: deleted items, to restore or purge. GET /items has no "deleted only" filter, so this asks for the latest
 * DELETED_WINDOW items with include_deleted and keeps the deleted ones. Without search words the store lists items newest
 * change first (store.search_memory), and deleting is a change, so the recently deleted ones are there; when there are more
 * items than that, the card says it looked at the most recent DELETED_WINDOW only. */
function DeletedList({ rev, onOpenItem }: { rev: number; onOpenItem: (id: number) => void }) {
  const list = usePoll(() => api.get<{ items: MemoryItem[]; total: number }>(`${BASE}/items${qs({ include_deleted: true, limit: DELETED_WINDOW })}`), 0, [rev]);
  const deleted = list.data ? list.data.items.filter((i) => i.deleted_at) : null;
  const partial = !!list.data && list.data.total > list.data.items.length;
  return (
    <Card
      title={
        <>
          <Icon name="history" size={18} className="card-icon" /> Deleted items
        </>
      }
      actions={deleted ? <span className="small">{plural(deleted.length, "item")}</span> : null}
    >
      <p className="muted small">
        Deleted items are hidden everywhere and kept with their versions until an admin purges them. Open one to restore or purge it.
        {partial ? ` Only the ${DELETED_WINDOW} most recently changed items are looked at here.` : ""}
      </p>
      <ErrorNote error={list.error ? `Could not load the deleted items: ${list.error}` : null} />
      {!deleted ? (
        list.error ? null : <Empty>Loading…</Empty>
      ) : !deleted.length ? (
        <Empty>{partial ? `No deleted items among the ${DELETED_WINDOW} most recently changed.` : "No deleted items."}</Empty>
      ) : (
        <ul className="br-hits">
          {deleted.map((it) => (
            <li key={it.id} className="br-hit">
              <button type="button" className="br-hit-title link-button" onClick={() => onOpenItem(it.id)}>
                {it.title}
              </button>
              <div className="br-hit-meta small">
                <ItemStatus item={it} /> <span>{kindLabel(it.kind, it.kind_label)}</span>
                <span className="muted">
                  · {agentTitle(it.agent)} · deleted <When iso={it.deleted_at} />
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

/** Admins (0.6.2): rows withheld because they may be a message an admin withheld, with the evidence (never their text):
 * keep one withheld for good, or release it (its text comes back at the next import). */
function HeldForReview({ rev, onChanged, onOpenMessage }: { rev: number; onChanged: () => void; onOpenMessage: (session: number, message?: number) => void }) {
  const list = usePoll(() => api.get<{ holds: BrainHold[]; total: number }>(`${BASE}/holds`), 0, [rev]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [release, setRelease] = useState<BrainHold | null>(null);
  const holds = list.data?.holds ?? null;

  async function act(h: BrainHold, what: "keep" | "release") {
    setBusy(h.message);
    setError(null);
    try {
      await api.post(`${BASE}/holds/${h.message}/${what}`);
      setRelease(null);
      onChanged();
      await list.refresh();
    } catch (err) {
      setError(errText(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card
      title={
        <>
          <Icon name="lock" size={18} className="card-icon" /> Held for review
        </>
      }
      actions={holds ? <span className="small">{plural(holds.length, "message")}</span> : null}
    >
      <p className="muted small">{HOLDS_INTRO}</p>
      <ErrorNote error={list.error ? `Could not load the held messages: ${list.error}` : release ? null : error} />
      {!holds ? (
        list.error ? null : <Empty>Loading…</Empty>
      ) : !holds.length ? (
        <Empty>No messages are held for review.</Empty>
      ) : (
        <ul className="br-hits">
          {holds.map((h) => {
            const w = h.withheld_message;
            const label = `message #${h.position} (${ROLE_LABEL[h.role] ?? cap(h.role)})`;
            return (
              <li key={h.message} className="br-hit br-review-row br-hold">
                <div className="br-review-main">
                  <button type="button" className="br-hit-title link-button" onClick={() => onOpenMessage(h.session, h.message)}>
                    {cap(label)} in {h.title === null ? "a conversation whose title is hidden" : h.title || "an untitled conversation"}
                  </button>
                  <div className="br-hit-meta small">
                    <span className="br-chip br-chip-risk">held for review</span>
                    <span className="muted">
                      <When iso={h.at} /> · {h.content_chars.toLocaleString()} characters · {h.state} · {h.compare}
                    </span>
                  </div>
                  <p className="small">{holdRuleText(h.rule)}</p>
                  <p className="small muted">
                    The withheld message it may be: {w.position != null ? `#${w.position}` : "no longer stored"} ({ROLE_LABEL[w.role] ?? (w.role ? cap(w.role) : "role unknown")}
                    {w.content_chars != null ? `, ${w.content_chars.toLocaleString()} characters` : ""}
                    {w.at ? ", " : ""}
                    {w.at ? <When iso={w.at} /> : null}).
                  </p>
                </div>
                <div className="row-actions br-review-actions">
                  <button type="button" className="btn small" disabled={busy === h.message} aria-label={`Keep ${label} withheld`} onClick={() => void act(h, "keep")}>
                    Keep withheld
                  </button>
                  <button type="button" className="btn small ghost" disabled={busy === h.message} aria-label={`Release ${label}`} onClick={() => setRelease(h)}>
                    Release
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
      {release ? (
        <ConfirmModal
          title={`Release message #${release.position}`}
          action="Release"
          busy={busy === release.message}
          error={error}
          onConfirm={() => void act(release, "release")}
          onCancel={() => {
            setRelease(null);
            setError(null);
          }}
        >
          <p>{RELEASE_CONFIRM}</p>
          <p>{holdRuleText(release.rule)}</p>
        </ConfirmModal>
      ) : null}
    </Card>
  );
}

function Review({
  rev,
  bump,
  onOpenItem,
  onOpenMessage,
}: {
  rev: number;
  bump: () => void;
  onOpenItem: (id: number) => void;
  onOpenMessage: (session: number, message?: number) => void;
}) {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const conflicts = usePoll(() => api.get<{ conflicts: MemoryConflict[]; total: number }>(`${BASE}/conflicts${qs({ status: "open" })}`), 0, [rev]);
  const open = conflicts.data?.conflicts ?? null;
  return (
    <>
      <p className="muted br-lead">
        Imported items wait here until a person approves them; agents read approved items only. Approving makes an item reference material: it never approves
        an action or changes a setting. Operators approve plain items; operating instructions, skills, workflow references, Credit Repair items, quarantined
        and flagged items need an admin.
      </p>
      {isAdmin ? (
        <ReviewList
          title="Quarantined"
          icon="lock"
          status="quarantined"
          rev={rev}
          intro={<p className="alert alert-warn small">{QUARANTINED_TEXT} Quarantined items never reach an AI prompt.</p>}
          empty="No quarantined items."
          onOpenItem={onOpenItem}
          onChanged={bump}
        />
      ) : (
        <p className="muted small br-note">Quarantined items (they held a secret, now removed, or an instruction-like rule) are reviewed by admins and are not listed for you.</p>
      )}
      <ReviewList
        title="Waiting for review"
        icon="approvals"
        status="pending_review"
        rev={rev}
        intro={null}
        empty="Nothing is waiting for review."
        onOpenItem={onOpenItem}
        onChanged={bump}
      />
      <Card
        title={
          <>
            <Icon name="alert" size={18} className="card-icon" /> Open conflicts
          </>
        }
        actions={conflicts.data ? <span className="small">{resultCount(conflicts.data.conflicts.length, conflicts.data.total, "conflict")}</span> : null}
      >
        <p className="muted small">An import never overwrites anything: when it finds a clash it asks here. Each choice is recorded and a rollback of the import puts it back.</p>
        <ErrorNote error={conflicts.error ? `Could not load the conflicts: ${conflicts.error}` : null} />
        {!open ? (
          conflicts.error ? null : <Empty>Loading…</Empty>
        ) : !open.length ? (
          <Empty>No open conflicts.</Empty>
        ) : (
          <ul className="br-conflicts">
            {open.map((c) => (
              <ConflictCard key={c.id} c={c} onOpenItem={onOpenItem} onResolved={bump} />
            ))}
          </ul>
        )}
      </Card>
      {isAdmin ? <HeldForReview rev={rev} onChanged={bump} onOpenMessage={onOpenMessage} /> : null}
      {isAdmin ? <DeletedList rev={rev} onOpenItem={onOpenItem} /> : null}
    </>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Import history.

function countsSummary(run: ImportRun): string {
  const c = run.counts ?? {};
  const byAction = (c.by_action ?? c) as Record<string, unknown>;
  return breakdown(byAction) || "—";
}

function Verification({ v }: { v: Record<string, unknown> | undefined }) {
  if (!v || !Object.keys(v).length) return <span className="muted">Not verified yet (run <code>brain-import verify</code>).</span>;
  const ok = typeof v.ok === "boolean" ? v.ok : typeof v.passed === "boolean" ? v.passed : null;
  const rest = Object.entries(v).filter(([k]) => k !== "ok" && k !== "passed");
  return (
    <>
      {ok === null ? <Pill value="unknown" label="Result not stated" /> : <Pill value={ok ? "succeeded" : "failed"} label={ok ? "Passed" : "Failed"} />}
      {rest.length ? (
        <ul className="br-list small">
          {rest.map(([k, x]) => (
            <li key={k}>
              {cap(words(k))}: {brief(x)}
            </li>
          ))}
        </ul>
      ) : null}
    </>
  );
}

function CountList({ rec, label = words }: { rec: Record<string, unknown> | undefined; label?: (k: string) => string }) {
  const list = entries(rec).sort((a, b) => b[1] - a[1]);
  if (!list.length) return <span className="muted">none</span>;
  return (
    <ul className="br-counts">
      {list.map(([k, n]) => (
        <li key={k}>
          <span className="tabular">{n.toLocaleString()}</span> {label(k)}
        </li>
      ))}
    </ul>
  );
}

/** `onRefused`: the API refused (for example, items were reviewed since the count shown): the run is loaded again, so the
 * reviewed-item count and the discard option are current. */
function RollbackModal({ detail, onDone, onCancel, onRefused }: { detail: ImportRunDetail; onDone: () => void; onCancel: () => void; onRefused?: () => void }) {
  const run = detail.run;
  const name = String(run.id);
  const [typed, setTyped] = useState("");
  const [discard, setDiscard] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const matches = typed.trim() === name;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!matches) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`${BASE}/imports/${run.id}/rollback`, { confirm: typed.trim(), discard_reviewed: discard });
      onDone();
    } catch (err) {
      const reason = confirmReason(err);
      setError(reason ? `The API did not accept the confirmation: ${reason}.` : `Not rolled back: ${errText(err)}`);
      if (!reason) onRefused?.();
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Roll back import run #${run.id}`} onClose={onCancel}>
      <form className="typed-confirm" onSubmit={submit}>
        <div className="alert alert-warn typed-confirm-text">
          This removes everything run #{run.id} imported: its memory items, conversations and messages, conflicts and drafts, and puts back what it changed in
          existing rows. A draft already converted stays, and so does its disabled schedule; items promoted from its conversations stay too. The JARVIS backup is
          not touched. This cannot be undone.
        </div>
        {detail.reviewed_items > 0 ? (
          <>
            <p className="small">
              <strong>{plural(detail.reviewed_items, "item")}</strong> from this run {detail.reviewed_items === 1 ? "was" : "were"} reviewed or edited since. The
              rollback is refused unless you discard {detail.reviewed_items === 1 ? "it" : "them"} too.
            </p>
            <label className="check">
              <input type="checkbox" checked={discard} onChange={(e) => setDiscard(e.target.checked)} />
              Discard reviewed items
            </label>
          </>
        ) : (
          <p className="muted small">No item from this run has been reviewed or edited since.</p>
        )}
        <label className="typed-confirm-label">
          <span>
            Type the run number <strong className="typed-confirm-name">{name}</strong> to confirm
          </span>
          <input value={typed} onChange={(e) => setTyped(e.target.value)} autoFocus autoComplete="off" spellCheck={false} />
        </label>
        {error ? (
          <div className="alert alert-bad" role="alert">
            {error}
          </div>
        ) : null}
        <div className="row-actions">
          <button className="btn danger" type="submit" disabled={!matches || busy}>
            Roll back run #{run.id}
          </button>
          <button className="btn" type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}

function RunModal({ id, onClose, onChanged }: { id: number; onClose: () => void; onChanged: () => void }) {
  const { user } = useAuth();
  const isAdmin = user.role === "admin";
  const detail = usePoll(() => api.get<ImportRunDetail>(`${BASE}/imports/${id}`), 0, [id]);
  const [rolling, setRolling] = useState(false);
  const [done, setDone] = useState(false);
  const d = detail.data;
  const run = d?.run;
  const report: ImportReport = run?.report ?? {};
  const secrets = entries(report.secrets?.types).sort((a, b) => b[1] - a[1]);
  // A run that stopped keeps its counts so far in the checkpoint (the report is written at the end).
  const tally = (run?.checkpoint as { tally?: { secrets?: { types?: Record<string, number> } } } | undefined)?.tally;
  const partialSecrets = entries(tally?.secrets?.types).sort((a, b) => b[1] - a[1]);
  const restrictedTypes = report.restricted?.types;
  const source = (run?.source ?? {}) as { db?: { name?: string; bytes?: number } };

  return (
    <Modal title={`Import run #${id}`} onClose={onClose} wide>
      <ErrorNote error={detail.error} />
      {!run || !d ? (
        detail.error ? null : <p className="muted">Loading…</p>
      ) : (
        <div className="br-run">
          <p className="br-item-meta">
            <RunStatus status={run.status} /> {run.label ? <strong>{run.label}</strong> : null}
            <span className="muted">
              {" "}
              · started <When iso={run.started_at} />
              {run.finished_at ? (
                <>
                  , finished <When iso={run.finished_at} />
                </>
              ) : null}
              {run.created_by ? ` by ${run.created_by}` : ""}
            </span>
          </p>
          {run.error ? <p className="alert alert-bad small">{run.error}</p> : null}
          {done ? <div className="alert alert-good">Run #{run.id} was rolled back.</div> : null}
          <dl className="kv br-kv">
            <dt>Records by action</dt>
            <dd>
              <CountList rec={d.records_by_action} />
            </dd>
            <dt>Records by type</dt>
            <dd>
              <CountList rec={d.records_by_type} />
            </dd>
            {report.totals ? (
              <>
                <dt>Selected in JARVIS</dt>
                <dd>{breakdown(report.totals) || "nothing"}</dd>
              </>
            ) : null}
            {report.time_range?.first ? (
              <>
                <dt>Conversations from</dt>
                <dd>
                  <When iso={report.time_range.first} dateOnly /> to <When iso={report.time_range.last} dateOnly />
                </dd>
              </>
            ) : null}
            {entries(report.by_reason).length ? (
              <>
                <dt>Excluded or skipped, by reason</dt>
                <dd>
                  <CountList rec={report.by_reason} />
                </dd>
              </>
            ) : null}
            {Object.keys(report.by_sensitivity ?? {}).length ? (
              <>
                <dt>By sensitivity</dt>
                <dd>
                  {Object.entries(report.by_sensitivity ?? {})
                    .map(([k, v]) => (typeof v === "number" ? `${v.toLocaleString()} ${words(k)}` : `${words(k)}: ${breakdown(v) || "none"}`))
                    .join("; ")}
                </dd>
              </>
            ) : null}
            {report.masked ? (
              <>
                <dt>Contact details masked</dt>
                <dd>
                  {plural(report.masked.email_address ?? 0, "e-mail address", "e-mail addresses")} · {plural(report.masked.phone_number ?? 0, "phone number")}
                </dd>
              </>
            ) : null}
            {report.restricted ? (
              <>
                <dt>Restricted data</dt>
                <dd>
                  {plural(report.restricted.sessions_withheld ?? 0, "conversation")} withheld · {plural(report.restricted.files_excluded ?? 0, "file")} excluded
                  {restrictedTypes && (Array.isArray(restrictedTypes) ? restrictedTypes.length : Object.keys(restrictedTypes).length) ? (
                    <span className="muted"> ({Array.isArray(restrictedTypes) ? restrictedTypes.map(words).join(", ") : breakdown(restrictedTypes)})</span>
                  ) : null}
                  <div className="muted small">Kept only in the JARVIS backup; never stored here.</div>
                </dd>
              </>
            ) : null}
            {entries(report.conflicts).length ? (
              <>
                <dt>Conflicts found</dt>
                <dd>{breakdown(report.conflicts, (k) => lowerFirst(CONFLICT_KIND[k as ConflictKind]?.[0] ?? words(k)))}</dd>
              </>
            ) : null}
            {report.drafts?.length ? (
              <>
                <dt>Workflow drafts</dt>
                <dd>
                  {report.drafts.map((x, i) => (
                    <span key={i}>
                      {i ? ", " : ""}
                      {x.name} ({words(x.compatibility)}
                      {x.enabled_in_source ? ", on in JARVIS" : ""})
                    </span>
                  ))}
                </dd>
              </>
            ) : null}
            {typeof report.missing_from_source === "number" && report.missing_from_source > 0 ? (
              <>
                <dt>Missing from this backup</dt>
                <dd>{plural(report.missing_from_source, "conversation")} imported before are not in this backup (they are kept).</dd>
              </>
            ) : null}
            <dt>Verification</dt>
            <dd>
              <Verification v={report.verification} />
            </dd>
            {source.db?.name ? (
              <>
                <dt>Source</dt>
                <dd>
                  {source.db.name}
                  {typeof source.db.bytes === "number" ? <span className="muted"> ({(source.db.bytes / 1_048_576).toFixed(1)} MB)</span> : null}
                  {run.importer_version ? <span className="muted"> · importer {run.importer_version}</span> : null}
                </dd>
              </>
            ) : null}
            {run.rolled_back_at ? (
              <>
                <dt>Rolled back</dt>
                <dd>
                  <When iso={run.rolled_back_at} /> {run.rolled_back_by ? `by ${run.rolled_back_by}` : ""}
                  {report.rollback ? <div className="muted small">{brief(report.rollback)}</div> : null}
                </dd>
              </>
            ) : null}
          </dl>

          <section className="br-rotation" aria-label="Credential rotation">
            <h3>Rotate these kinds of credential</h3>
            {!report.secrets ? (
              partialSecrets.length ? (
                <>
                  <p className="small">
                    The run did not finish, so this list is partial: up to where it stopped, JARVIS's files or conversations held these secrets. Change them
                    where they are used, and check the rest after a complete run.
                  </p>
                  <ul className="br-counts">
                    {partialSecrets.map(([k, n]) => (
                      <li key={k}>
                        <strong>{words(k)}</strong> <span className="muted">found {n === 1 ? "once" : `${n.toLocaleString()} times`} so far</span>
                      </li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="muted small">No list yet: the run did not finish. Secrets found by a complete run are listed here.</p>
              )
            ) : secrets.length ? (
              <>
                <p className="small">
                  JARVIS's files or conversations held these secrets. They were removed and never stored; change them where they are used, since JARVIS (and its
                  backup) had them.
                </p>
                <ul className="br-counts">
                  {secrets.map(([k, n]) => (
                    <li key={k}>
                      <strong>{words(k)}</strong> <span className="muted">found {n === 1 ? "once" : `${n.toLocaleString()} times`}</span>
                    </li>
                  ))}
                </ul>
                {report.secrets?.where ? <p className="muted small">Where: {breakdown(report.secrets.where) || "not stated"}.</p> : null}
              </>
            ) : (
              <p className="muted small">No secrets were found in what this run read.</p>
            )}
          </section>

          {isAdmin ? (
            <section className="br-actions" aria-label="Roll back">
              <h3>Roll back</h3>
              {d.can_roll_back ? (
                <>
                  <p className="small">
                    Removes what this run imported and puts back what it changed.
                    {d.reviewed_items ? ` ${plural(d.reviewed_items, "item")} from it ${d.reviewed_items === 1 ? "was" : "were"} reviewed or edited since.` : ""}
                  </p>
                  <button className="btn small danger" type="button" onClick={() => setRolling(true)}>
                    Roll back run #{run.id}…
                  </button>
                </>
              ) : (
                <p className="muted small">
                  {run.status === "rolled_back"
                    ? "Already rolled back."
                    : run.status === "running"
                      ? "Still running: it cannot be rolled back now."
                      : "The API says this run cannot be rolled back now (for example, a later run depends on it: roll back in reverse order)."}
                </p>
              )}
            </section>
          ) : null}
        </div>
      )}
      {rolling && d ? (
        <RollbackModal
          detail={d}
          onRefused={() => void detail.refresh()}
          onCancel={() => setRolling(false)}
          onDone={() => {
            setRolling(false);
            setDone(true);
            void detail.refresh();
            onChanged();
          }}
        />
      ) : null}
    </Modal>
  );
}

function Imports({ rev, openRun }: { rev: number; openRun: (id: number) => void }) {
  const list = usePoll(() => api.get<{ runs: ImportRun[] }>(`${BASE}/imports`), 15_000, [rev]);
  const runs = list.data?.runs ?? null;
  return (
    <Card
      title={
        <>
          <Icon name="history" size={18} className="card-icon" /> Import runs
        </>
      }
      actions={runs ? <span className="small">{plural(runs.length, "run")}</span> : null}
    >
      <p className="muted small">
        Every import of JARVIS's backup, with what it did to each record. Imports run from the command line (<code>scripts\jarvis-import.ps1</code>); this page
        shows them and lets an admin roll one back.
      </p>
      <ErrorNote error={list.error ? `Could not load the import runs: ${list.error}` : null} />
      {!runs ? (
        list.error ? null : <Empty>Loading import runs…</Empty>
      ) : !runs.length ? (
        <Empty>{NO_IMPORT}.</Empty>
      ) : (
        <div className="table-wrap">
          <table className="table br-table br-runs">
            <thead>
              <tr>
                <th scope="col">Run</th>
                <th scope="col">Status</th>
                <th scope="col">Started</th>
                <th scope="col">Finished</th>
                <th scope="col">Records</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr key={r.id}>
                  <td className="br-c-main">
                    <button type="button" className="link-button br-row-title" onClick={() => openRun(r.id)}>
                      Run #{r.id}
                    </button>
                    {r.label ? <div className="muted small">{r.label}</div> : null}
                  </td>
                  <td data-label="Status">
                    <RunStatus status={r.status} />
                    {r.error ? <div className="text-bad small">{r.error}</div> : null}
                  </td>
                  <td data-label="Started">
                    <When iso={r.started_at} />
                    {r.created_by ? <div className="muted small">by {r.created_by}</div> : null}
                  </td>
                  <td data-label="Finished">{r.finished_at ? <When iso={r.finished_at} /> : <span className="muted">—</span>}</td>
                  <td data-label="Records" className="small">
                    {countsSummary(r)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// The page.

/** Keeps one part of this page from taking the whole dashboard down when the API answers in a shape this build does not
 * expect (the backend is versioned separately): that part says so instead. A dialog (`onClose` given) says so in a dialog
 * that can be closed. */
class Contained extends Component<{ children: ReactNode; what: string; onClose?: () => void }, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(err: unknown) {
    return { error: err instanceof Error ? err.message : String(err) };
  }

  componentDidCatch(err: unknown) {
    console.error(err);
  }

  render() {
    if (!this.state.error) return this.props.children;
    const alert = (
      <div className="alert alert-bad" role="alert">
        {this.props.what} could not be shown: the API answered in a form this dashboard does not understand ({this.state.error}). The data itself is unchanged.
      </div>
    );
    return this.props.onClose ? (
      <Modal title="Could not be shown" onClose={this.props.onClose}>
        {alert}
      </Modal>
    ) : (
      alert
    );
  }
}

type TabKey = "overview" | "search" | "conversations" | "agents" | "skills" | "workflows" | "review" | "imports";
const TABS: { key: TabKey; label: string; icon: IconName; operators?: boolean }[] = [
  { key: "overview", label: "Overview", icon: "dashboard" },
  { key: "search", label: "Search", icon: "search" },
  { key: "conversations", label: "Conversations", icon: "chat", operators: true },
  { key: "agents", label: "Agent knowledge", icon: "agents" },
  { key: "skills", label: "Skills", icon: "book" },
  { key: "workflows", label: "Workflows", icon: "workflows", operators: true },
  { key: "review", label: "Review", icon: "approvals", operators: true },
  { key: "imports", label: "Import history", icon: "history", operators: true },
];
/** URL parameters of the dialogs: an item, a conversation (and a message in it), a workflow draft, an import run. */
const DIALOG_PARAMS = ["item", "session", "message", "draft", "run"];

export default function Brain() {
  const { user, canOperate } = useAuth();
  const [params, setParams] = useSearchParams();
  const tabs = TABS.filter((t) => !t.operators || canOperate);
  const requested = params.get("tab");
  const tab: TabKey = tabs.find((t) => t.key === requested)?.key ?? "overview";
  const tabRefs = useRef<Partial<Record<TabKey, HTMLButtonElement | null>>>({});
  const [rev, setRev] = useState(0);
  const bump = () => setRev((r) => r + 1);

  const id = (k: string) => Number(params.get(k)) || null;
  const itemId = id("item");
  const sessionId = canOperate ? id("session") : null;
  const messageId = id("message");
  const draftId = canOperate ? id("draft") : null;
  const runId = canOperate ? id("run") : null;

  function update(changes: Record<string, string | null>) {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const [k, v] of Object.entries(changes)) {
          if (v) next.set(k, v);
          else next.delete(k);
        }
        return next;
      },
      { replace: true },
    );
  }

  function selectTab(key: TabKey) {
    const next = new URLSearchParams(params);
    for (const k of DIALOG_PARAMS) next.delete(k);
    if (key === "overview") next.delete("tab");
    else next.set("tab", key);
    setParams(next, { replace: true });
  }

  function onTabKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = tabs.findIndex((t) => t.key === tab);
    const n = tabs.length;
    const next = e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (next < 0) return;
    e.preventDefault();
    selectTab(tabs[next].key);
    tabRefs.current[tabs[next].key]?.focus();
  }

  const openItem = (n: number) => update({ item: String(n) });
  const openSession = (s: number, m?: number) => update({ session: String(s), message: m ? String(m) : null });
  const closeSession = () => update({ session: null, message: null });
  const closeDraft = () => update({ draft: null });
  const closeRun = () => update({ run: null });
  const closeItem = () => update({ item: null });

  return (
    <div className="page br-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Memory</div>
          <h1>JARVIS Brain</h1>
          <p className="muted">
            What the Command Center keeps from JARVIS, your previous assistant: its knowledge, skills, scheduled jobs and conversations. Imported text is reference
            data. It never becomes an instruction, an approval, a setting or a running job, and agents read only what a person approved.
          </p>
          {user.role === "viewer" ? <p className="muted small">As a viewer you see approved business knowledge and the agents' reading rules. Conversations, review and imports are for operators and admins.</p> : null}
        </div>
      </header>

      <div className="tabs br-tabs" role="tablist" aria-label="JARVIS Brain" onKeyDown={onTabKey}>
        {tabs.map((t) => (
          <button
            key={t.key}
            ref={(el) => {
              tabRefs.current[t.key] = el;
            }}
            type="button"
            role="tab"
            id={`br-tab-${t.key}`}
            aria-selected={tab === t.key}
            aria-controls={`br-panel-${t.key}`}
            tabIndex={tab === t.key ? 0 : -1}
            className={tab === t.key ? "active" : ""}
            onClick={() => selectTab(t.key)}
          >
            <Icon name={t.icon} size={15} className="br-tab-icon" />
            <span>{t.label}</span>
          </button>
        ))}
      </div>

      <div className="br-panel" role="tabpanel" id={`br-panel-${tab}`} aria-labelledby={`br-tab-${tab}`}>
        <Contained key={tab} what="This tab">
          {tab === "overview" ? <Overview rev={rev} /> : null}
          {tab === "search" ? <SearchTab rev={rev} onOpenItem={openItem} onOpenMessage={(s, m) => openSession(s, m)} /> : null}
          {tab === "conversations" ? <Conversations rev={rev} onOpen={(s) => openSession(s)} /> : null}
          {tab === "agents" ? <AgentKnowledge rev={rev} /> : null}
          {tab === "skills" ? <Skills rev={rev} onOpenItem={openItem} /> : null}
          {tab === "workflows" ? <Workflows rev={rev} openDraft={(n) => update({ draft: String(n) })} /> : null}
          {tab === "review" ? <Review rev={rev} bump={bump} onOpenItem={openItem} onOpenMessage={(s, m) => openSession(s, m)} /> : null}
          {tab === "imports" ? <Imports rev={rev} openRun={(n) => update({ run: String(n) })} /> : null}
        </Contained>
      </div>

      {/* Each dialog is contained on its own, keyed by what it shows: opening an item from a conversation must not reset the
          conversation (its page, an open promote form). */}
      {sessionId ? (
        <Contained key={`session:${sessionId}:${messageId ?? ""}`} what="This conversation" onClose={closeSession}>
          <SessionModal id={sessionId} focus={messageId} onClose={closeSession} onOpenItem={openItem} onChanged={bump} />
        </Contained>
      ) : null}
      {draftId ? (
        <Contained key={`draft:${draftId}`} what="This workflow draft" onClose={closeDraft}>
          <DraftModal id={draftId} onClose={closeDraft} onChanged={bump} />
        </Contained>
      ) : null}
      {runId ? (
        <Contained key={`run:${runId}`} what="This import run" onClose={closeRun}>
          <RunModal id={runId} onClose={closeRun} onChanged={bump} />
        </Contained>
      ) : null}
      {itemId ? (
        <Contained key={`item:${itemId}`} what="This memory item" onClose={closeItem}>
          <ItemModal id={itemId} onClose={closeItem} onChanged={bump} />
        </Contained>
      ) : null}
    </div>
  );
}
