// Thin client for the Command Center API. Every mutating request carries the
// X-Requested-With header the API requires alongside the session cookie.

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

type Json = Record<string, unknown>;

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (method !== "GET") headers["X-Requested-With"] = "command-center";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { detail: text };
    }
  }
  if (!response.ok) {
    const detail = (data as Json | null)?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d: Json) => `${(d.loc as string[] | undefined)?.slice(1).join(".") ?? ""} ${d.msg}`).join("; ")
          : `HTTP ${response.status}`;
    if (response.status === 401) window.dispatchEvent(new Event("cc:unauthorized"));
    throw new ApiError(response.status, message);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export type Severity = "info" | "warning" | "critical";
export type Risk = "read" | "draft" | "external" | "change" | "financial" | "destructive";

export interface User {
  id: number;
  email: string;
  display_name: string;
  role: "admin" | "operator" | "viewer";
  can_access_sensitive?: boolean;
}

export interface Finding {
  code: string;
  severity: Severity;
  title: string;
  detail?: string;
}

export interface Capability {
  name: string;
  description: string;
  risk: Risk;
  requires_approval: boolean;
  params_schema: { properties?: Record<string, { type?: string; default?: unknown; description?: string; enum?: unknown[] }>; required?: string[] };
  examples: string[];
}

export interface Agent {
  name: string;
  role: "supervisor" | "agent";
  title: string;
  description: string;
  enabled: boolean;
  entitled: boolean;
  sensitive: boolean;
  integrations: string[];
  capabilities: Capability[];
  stats_24h: Record<string, number>;
  last_run_at: string | null;
  /** Work in progress now, however old the task (0.5.0 API; absent from older APIs). */
  active?: AgentActive;
}

export interface AgentActive {
  queued: number;
  running: number;
  awaiting_approval: number;
}

export interface Task {
  id: number;
  created_at: string;
  updated_at: string;
  request_id: number | null;
  schedule_id: number | null;
  agent: string;
  capability: string;
  params: Json;
  risk: Risk;
  status: "queued" | "running" | "succeeded" | "failed" | "awaiting_approval" | "rejected" | "cancelled";
  attempts: number;
  started_at: string | null;
  finished_at: string | null;
  source: string;
  created_by: string;
  summary: string;
  result: { summary?: string; data?: Json; findings?: Finding[] } | null;
  error: string;
  /** Last sign of life from the worker running it. */
  heartbeat_at?: string | null;
}

export interface TaskLog {
  id: number;
  at: string;
  level: string;
  message: string;
}

export interface Approval {
  id: number;
  created_at: string;
  task_id: number;
  status: "pending" | "approved" | "rejected";
  agent: string;
  capability: string;
  risk: Risk;
  summary: string;
  details: { params?: Json; capability_description?: string; target?: string; environment?: string; production?: boolean; confirm?: string };
  decided_by: string;
  decided_at: string | null;
  decision_note: string;
}

export interface SupervisorRequest {
  id: number;
  created_at: string;
  source: string;
  requested_by: string;
  message: string;
  status: "planning" | "running" | "awaiting_approval" | "completed" | "failed";
  planner: string;
  plan: { steps?: { agent: string; capability: string; params: Json; rationale: string }[]; notes?: string[]; reply?: string };
  reply: string;
  summary: string;
  summarizer: string;
  error: string;
  completed_at: string | null;
}

export interface SiteCheck {
  id: number;
  checked_at: string;
  status: "up" | "degraded" | "down";
  http_status: number | null;
  response_ms: number | null;
  final_url: string;
  tls_days_left: number | null;
  tls_issuer: string;
  is_wordpress: boolean;
  wp_version: string;
  findings: Finding[];
  details: Json & {
    deep?: boolean;
    plugins_detected?: string[];
    themes_detected?: string[];
    latest_wp_version?: string;
    external_scripts?: string[];
    tls?: { expires_at?: string; protocol?: string; issuer?: string };
    redirect_check?: { chain?: string[]; offsite?: boolean; variants?: Record<string, { offsite_target?: string; final_url?: string }> };
  };
}

export type Environment = "production" | "staging" | "test";

export interface Uptime {
  "24h": number | null;
  "7d": number | null;
  "30d": number | null;
  checks_30d: number;
  avg_response_ms_24h: number | null;
  /** Oldest check in the 30-day window: on a younger site the 30-day figure covers only the time since then (0.5.0 API). */
  first_check_at?: string | null;
  /** Checks behind each percentage (0.5.0 API; n_30d equals checks_30d). */
  n_24h?: number;
  n_7d?: number;
  n_30d?: number;
  /** Clock hours of each window with at least one check (0.5.0 API). */
  hours_checked_24h?: number;
  hours_checked_7d?: number;
  hours_checked_30d?: number;
}

export interface Incident {
  start: string;
  end: string | null;
  minutes: number;
  reason: string;
}

export interface Diagnosis {
  at?: string;
  server?: string;
  core_version?: string;
  site_urls?: {
    home: string;
    siteurl: string;
    expected: string;
    expected_siteurl?: string;
    matches: boolean;
    /** WordPress seems to live in a sub-folder and the cc-gate configuration has no "siteurl" for the site. */
    subdirectory_install_suspected?: boolean;
    suspected_siteurl?: string;
  };
  plugins?: { name: string; status: string; version: string; update: string; update_version: string }[];
  themes?: { name: string; status: string; version: string; update: string }[];
  admins?: { ID: number; user_login: string; user_registered: string }[];
  super_admins?: string[];
  maintenance?: { active: boolean };
  debug_log?: { exists: boolean; bytes?: number; type?: string };
  core_verify?: { verified: boolean | null; detail?: string; files?: string[]; extra_files?: string[] };
  /** Parts the server could not answer, with the reason; the rest of the diagnosis still counts. */
  errors?: Record<string, string>;
  findings?: Finding[];
  /** A later check that could not run at all; the results above (if any) are older. */
  last_failed_attempt?: { at: string; error: string };
}

export interface Site {
  id: number;
  name: string;
  url: string;
  client: string;
  notes: string;
  enabled: boolean;
  last_status: "unknown" | "up" | "degraded" | "down";
  last_checked_at: string | null;
  last_check: SiteCheck | null;
  environment: Environment;
  server_id: number | null;
  server_site_key: string;
  uptime: Uptime | null;
  incidents_30d?: Incident[];
  last_diagnosis: Diagnosis;
  last_diagnosis_at: string | null;
}

export interface Server {
  id: number;
  name: string;
  host: string;
  port: number;
  username: string;
  environment: Environment;
  host_key_pinned: boolean;
  host_key_fingerprint: string;
  host_key_trusted_by: string;
  host_key_trusted_at: string | null;
  enabled: boolean;
  notes: string;
  last_hello: {
    gate_version?: string;
    user?: string;
    uid?: number;
    hostname?: string;
    allow_changes?: boolean;
    sites?: Record<string, { url: string }>;
    services?: string[];
    restartable_services?: string[];
    at?: string;
  };
  last_seen_at: string | null;
  created_at: string;
  sites: { id: number; name: string; server_site_key: string; environment: Environment }[];
}

export interface ServerCommand {
  id: number;
  server_id: number;
  task_id: number | null;
  op: string;
  args: Record<string, string>;
  kind: "read" | "change";
  requested_by: string;
  approved_by: string;
  started_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  exit_status: number | null;
  ok: boolean | null;
  refused: boolean;
  error: string;
}

export interface SshKey {
  openssh: string;
  fingerprint: string;
  created_at: string;
  created_by: string;
  authorized_keys_line: string;
}

export interface CCEvent {
  id: number;
  at: string;
  kind: string;
  severity: Severity;
  source: string;
  title: string;
  detail: Json;
  task_id: number | null;
  forward_status: "pending" | "skipped" | "sent" | "failed";
  forward_attempts: number;
  forward_error: string;
  forwarded_at: string | null;
  n8n_ack_at: string | null;
  n8n_execution_id: string;
}

export interface Artifact {
  id: number;
  created_at: string;
  task_id: number | null;
  agent: string;
  kind: string;
  title: string;
  content?: string;
  content_type: string;
  data: Json;
  status: "draft" | "approved" | "rejected" | "archived";
}

export interface Schedule {
  id: number;
  name: string;
  description: string;
  agent: string;
  capability: string;
  params: Json;
  cron: string;
  enabled: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface IntegrationState {
  name: string;
  last_ok_at: string | null;
  last_error_at: string | null;
  last_error: string;
  detail: Json;
}

export interface Integration {
  name: string;
  title: string;
  mode: "live" | "mock" | "disabled";
  description: string;
  live_requirements: string;
  status: IntegrationState | null;
  workflows?: { id: string; name: string; active: boolean }[] | null;
  workflow_error?: string;
  webhook_url?: string;
  model?: string;
}

export interface SystemStatus {
  app: string;
  version: string;
  environment: string;
  time: string;
  database: { ok: boolean };
  workers: { worker_id: string; last_seen_at: string; alive: boolean; info: Json }[];
  llm: {
    configured: boolean;
    provider: string;
    model: string | null;
    requests_today: number;
    daily_cap: number;
    reason?: string;
    /** When the AI model last answered, or last failed, and that error (0.5.0 API). */
    last_ok_at?: string | null;
    last_error_at?: string | null;
    last_error?: string;
  };
  workflow_engine?: string;
  /** The n8n editor's address on this PC, only with the n8n engine and only http(s) (0.5.0 API). */
  n8n_editor_url?: string | null;
  edition?: { code: string; title: string; source: string };
  n8n: IntegrationState | null;
  /** Whether this PC reaches the internet, from the monitor's control check when a site gives no answer (0.5.0 API). While
   * "offline", sites are not checked and keep their last real result. */
  connectivity?: {
    state: "online" | "offline" | "unknown";
    since: string | null;
    last_ok_at: string | null;
    last_error_at: string | null;
    last_error: string;
  };
  counts: {
    sites: Record<string, number>;
    tasks_24h: Record<string, number>;
    tasks_queued: number;
    tasks_running: number;
    approvals_pending: number;
    events_24h: Record<string, number>;
    drafts: number;
    /** The same as `sites`, for monitored (enabled) sites only, and the number of paused sites (0.5.0 API). */
    sites_enabled?: Record<string, number>;
    sites_paused?: number;
  };
  /** CC_INSTANCE_LABEL: set on a test copy (the JARVIS Brain test instance), empty or absent on a live install (0.6.0 API;
   * absent from older APIs). The shell shows a "TEST COPY" banner while it is set. */
  instance_label?: string | null;
}

/** GET /api/stats/task-activity: finished tasks (succeeded or failed) per category and local time bucket. The AI
 * Supervisor's own planning and summary steps are not counted. */
export type ActivityRange = "24h" | "7d" | "30d";

export interface TaskActivity {
  range: ActivityRange;
  bucket: "hour" | "6 hours" | "day";
  tz: string;
  since: string;
  /** Display categories in the API's order. */
  categories: { key: string; label: string }[];
  /** Every bucket from the start of the range to now; `start` is local time with its UTC offset. */
  buckets: { start: string; counts: Record<string, number>; failed: number }[];
  totals: Record<string, number>;
  failed: number;
  /** Since local midnight, and yesterday from midnight to the same clock time. */
  today: { succeeded: number; failed: number };
  yesterday_same_time: { succeeded: number; failed: number };
}

export interface AuditEntry {
  id: number;
  at: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  detail: Json;
  ip: string;
}

export interface EditionInfo {
  edition: string;
  title: string;
  source: "license" | "configuration";
  license: { license_id: string; customer: string; issued_at: string; expires_at: string | null; expired: boolean } | null;
  limits: { max_sites: number | null; max_users: number | null };
  agents: string[] | null;
  features: Record<string, { enabled: boolean; description: string }>;
  notes: string[];
  usage: { sites: number; users: number };
}

export interface SettingField {
  value: string | number;
  label: string;
  description: string;
  kind: "text" | "choice" | "integer" | "timezone";
  choices: string[];
}

export interface SettingsPage {
  settings: Record<string, SettingField>;
  secrets: Record<string, { label: string; description: string; set: boolean; hint: string; updated_at: string | null; updated_by: string }>;
  secrets_available: boolean;
  ai: { provider: string; model: string; daily_cap: number; key_source: string; reason: string };
}

export interface ManagedUser {
  id: number;
  email: string;
  display_name: string;
  role: "admin" | "operator" | "viewer";
  is_active: boolean;
  can_access_sensitive: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

export interface LeadRecord {
  id: number;
  created_at: string;
  source: string;
  name: string;
  company: string;
  email: string;
  phone: string;
  website: string;
  service_interest: string;
  message: string;
  score: number | null;
  score_reasons: string[];
  status: string;
  proposal_artifact_id: number | null;
}

export interface Ticket {
  id: number;
  created_at: string;
  source: string;
  customer_name: string;
  customer_email: string;
  subject: string;
  body: string;
  category: string;
  priority: "low" | "normal" | "high" | "urgent";
  sentiment: string;
  status: string;
  reply_artifact_id: number | null;
}

export interface OutboxRecord {
  id: number;
  created_at: string;
  task_id: number | null;
  agent: string;
  integration: string;
  mode: "mock" | "live";
  action: string;
  recipient: string;
  subject: string;
  body: string;
  payload: Record<string, unknown>;
  approved_by: string;
  result: string;
}

export interface CRStatus {
  configured: boolean;
  entitled: boolean;
  enabled: { acknowledged_by: string; acknowledged_at: string } | null;
  can_access: boolean;
  notice: string;
}

export interface CRClient {
  id: number;
  reference: string;
  full_name: string;
  status: string;
  contract_signed_on: string | null;
  services_completed_on: string | null;
  fees_collected_cents: number;
  created_at: string;
  address?: string;
  email?: string;
  phone?: string;
  dob?: string;
  ssn_last4?: string;
  notes?: string;
}

// ---------------------------------------------------------------------------------------------------------------------
// JARVIS Brain (0.6.0 API, /api/brain; docs/JARVIS_BRAIN_SPEC.md section 3). Imported text is reference data: nothing here
// approves, runs or schedules anything. The API applies each person's scope (viewer, operator, admin, sensitive-data
// permission); these shapes are what it returns within that scope.

export type MemoryKind =
  | "business"
  | "knowledge"
  | "agent_note"
  | "instruction"
  | "skill"
  | "workflow"
  | "project"
  | "decision"
  | "accomplishment"
  | "problem";
export type MemoryStatus = "pending_review" | "quarantined" | "approved" | "rejected" | "archived";
export type Sensitivity = "general" | "customer" | "restricted";

/** MemoryItemOut. `flags` holds review hints (secrets_redacted, masked, instruction_like, approval_conflict, script,
 * plugin_or_hook, custom, owner_profile, template, truncated, conflict, ...) and, for skills, the mapping (skill, staff,
 * capabilities, compatibility, note, frontmatter). `needs_admin`: approving or editing it needs an admin. */
export interface MemoryItem {
  id: number;
  kind: MemoryKind;
  kind_label: string;
  /** "" = shared by every agent; otherwise the one agent it belongs to. */
  agent: string;
  title: string;
  body: string;
  tags: string[];
  sensitivity: "general" | "customer";
  status: MemoryStatus;
  origin: "import" | "manual" | "promoted";
  flags: Record<string, unknown>;
  source_system: string;
  source_ref: string;
  source_at: string | null;
  import_run_id: number | null;
  version: number;
  created_at: string;
  created_by: string;
  updated_at: string;
  updated_by: string;
  reviewed_by: string;
  reviewed_at: string | null;
  review_note: string;
  deleted_at: string | null;
  needs_admin: boolean;
  /** In list and search results only. */
  excerpt?: string;
  rank?: number;
}

/** One row of memory_item_versions: every change to an item, in full. A redacted version has its title and body blanked. */
export interface MemoryItemVersion {
  id?: number;
  item_id?: number;
  version: number;
  change: string;
  kind: string;
  agent: string;
  title: string;
  body: string;
  tags?: string[];
  sensitivity: string;
  status: string;
  changed_by: string;
  changed_at: string;
  note: string;
}

/** ArchiveSessionOut: the archive_sessions columns except `search`. A "restricted" session is withheld entirely: its title,
 * summary and every message's text are empty (the text stays only in the JARVIS backup). */
export interface ArchiveSession {
  id: number;
  source_system: string;
  source_key: string;
  source_session_id: string;
  title: string;
  staff: string;
  agent: string;
  origin: "chat" | "scheduled";
  channel: string;
  lineage: "" | "continuation" | "branch" | "delegated";
  parent_source_session_id: string;
  job_id: string;
  job_name: string;
  started_at: string | null;
  ended_at: string | null;
  end_reason: string;
  hermes_archived: boolean;
  source_message_count: number;
  imported_messages: number;
  withheld_messages: number;
  tool_calls: number;
  sensitivity: Sensitivity;
  /** Messages per keyword-matched label (accomplishment, problem, decision). */
  labels: Record<string, number>;
  summary: string;
  /** flags.attribution: "job id" or "default" (how the staff member was inferred); flags.time_assumed_local. */
  flags: Record<string, unknown>;
  import_run_id: number | null;
  created_at: string;
  updated_at: string;
}

export interface ArchiveToolCall {
  name: string;
  arguments: string;
}

/** ArchiveMessageOut. A withheld message has no text. */
export interface ArchiveMessage {
  id: number;
  session_id: number;
  position: number;
  role: string;
  content: string;
  truncated: boolean;
  tool_name: string;
  tool_calls: ArchiveToolCall[];
  at: string | null;
  state: "live" | "compacted";
  sensitivity: Sensitivity;
  withheld: boolean;
  /** Labels matched by keywords: accomplishment, problem, decision. */
  labels: string[];
  /** Secret types (and counts) removed from this message; never the values. */
  redactions: Record<string, unknown>;
  flags: Record<string, unknown>;
}

/** A stored row the withhold preview names: where it is, never its text. */
export interface WithholdRow {
  id: number;
  session: number;
  position: number;
  role: string;
  at: string | null;
  state: "live" | "compacted";
  withheld: boolean;
}

/** GET /api/brain/messages/{id}/withhold-preview (admin, read-only, 0.6.2): what a withhold would also take. Rows outside
 * the admin's scope are counted in one total (rows_outside_your_scope), never named and never per list. */
export interface WithholdPreview {
  message: number;
  /** Other stored rows of the same JARVIS message: withheld too. */
  same_message_rows: WithholdRow[];
  /** Rows sharing its JARVIS id with other text: withheld and held for an admin's review. */
  held_rows: WithholdRow[];
  /** Readable rows with exactly this text in the conversation and its continuations or branches: only when asked for. */
  identical_text_rows: WithholdRow[];
  /** Rows of the three lists above that are outside the admin's scope: one total, so it never tells which list. */
  rows_outside_your_scope?: number;
  /** Copies the importer stored only as duplicates (no row of their own). */
  rowless_copies: number;
  /** Imported memory items whose text contains the message's text (to purge separately; nothing is automatic). */
  items_with_this_text: { id: number; title: string }[];
  first_user_message: boolean;
}

/** POST /api/brain/messages/{id}/withhold: what was withheld (0.6.2 adds the same-message and held rows). */
export interface WithholdResult {
  copies?: { items?: number[]; versions_blanked?: number };
  also_withheld?: number[];
  held_for_review?: number[];
  identical_withheld?: number;
  keys_tombstoned?: number;
  title_hidden?: boolean;
}

/** GET /api/brain/holds (admin, 0.6.2): a row withheld because it may be a withheld message. Evidence only, never text. */
export interface BrainHold {
  message: number;
  session: number;
  /** null when an admin hid the conversation's title. */
  title: string | null;
  position: number;
  role: string;
  at: string | null;
  content_chars: number;
  state: "live" | "compacted";
  /** The rule that held it: "same message id, different text", "same text", "upgrade: same message id", ... */
  rule: string;
  withheld_message: { id: number | null; position: number | null; role: string; at: string | null; content_chars: number | null };
  compare: "identical text" | "different text" | "contains the withheld text" | "nothing to compare";
  /** Ids of the readable messages before and after it. */
  neighbours: number[];
}

export interface BrainSearchHit {
  message: ArchiveMessage;
  session: { id: number; title: string; staff: string; agent: string; started_at: string | null };
  excerpt: string;
}

/** GET /api/brain/search. `filtered` is false when the words filter nothing (empty or only common words): the newest are shown. */
export interface BrainSearch {
  memory: { hits: MemoryItem[]; total: number };
  archive: { hits: BrainSearchHit[]; total: number };
  filtered: boolean;
}

export type ConflictKind = "memory_item" | "source_changed" | "instruction" | "schedule" | "site" | "workflow";
export type ConflictResolution = "keep_existing" | "use_incoming" | "keep_both" | "dismissed";

/** A row of memory_conflicts: something an import found that a person must decide. Never resolved by overwriting. */
export interface MemoryConflict {
  id: number;
  run_id: number | null;
  kind: ConflictKind;
  title: string;
  source_key?: string;
  existing_table?: string;
  existing_id?: number | null;
  incoming_item_id?: number | null;
  existing?: Record<string, unknown>;
  incoming?: Record<string, unknown>;
  status: "open" | ConflictResolution;
  effects?: Record<string, unknown>;
  decided_by?: string;
  decided_at?: string | null;
  note?: string;
  created_at: string;
  /** Keep existing / use incoming change an item whose approval needs an admin (or the kind needs one). */
  needs_admin?: boolean;
}

/** A row of workflow_drafts: one of JARVIS's scheduled jobs. A draft never runs; converting it (admin) creates a disabled
 * schedule. */
export interface WorkflowDraft {
  id: number;
  source_system: string;
  source_key: string;
  name: string;
  staff: string;
  agent: string;
  capability: string;
  params: Record<string, unknown>;
  compatibility: "mapped" | "partial" | "reference_only";
  trigger: "schedule" | "manual" | "event";
  /** "" when JARVIS's expression was not a valid cron. In JARVIS it ran in the PC's local time. */
  cron: string;
  schedule_display: string;
  timezone: string;
  enabled_in_source: boolean;
  deliver: string;
  approval: { jarvis?: string; jarvis_rules?: string[]; command_center?: string } & Record<string, unknown>;
  error_handling: { jarvis?: string[]; command_center?: string } & Record<string, unknown>;
  duplicate_guard: { jarvis_job_enabled?: boolean; overlaps?: string | string[] | null; rule?: string } & Record<string, unknown>;
  spec: string;
  history: { runs?: number; first?: string | null; last?: string | null; with_problems?: number; last_runs?: string[]; from?: string } & Record<string, unknown>;
  notes: string[];
  status: "draft" | "reviewed" | "converted" | "rejected";
  schedule_id: number | null;
  import_run_id: number | null;
  created_at: string;
  updated_at: string;
  reviewed_by: string;
  reviewed_at: string | null;
}

/** The import report (spec 2.8): counts, ids, kinds and safe labels only, never message text or titles. */
export interface ImportReport {
  selection?: Record<string, unknown>;
  totals?: Partial<Record<"sessions" | "messages" | "files" | "jobs" | "urls", number>>;
  by_action?: Record<string, number>;
  by_reason?: Record<string, number>;
  by_type?: Record<string, number>;
  by_agent?: Record<string, number>;
  by_staff?: Record<string, number>;
  by_kind?: Record<string, number>;
  /** Per kind of record: { messages: { general: 40, customer: 10 }, items: {...}, sessions: {...} }. */
  by_sensitivity?: Record<string, number | Record<string, number>>;
  /** The credential rotation list: secret types and how often each was found. Never the values. */
  secrets?: { types?: Record<string, number>; where?: Partial<Record<"messages" | "files" | "drafts", number>> };
  restricted?: { types?: Record<string, number> | string[]; sessions_withheld?: number; files_excluded?: number };
  masked?: { email_address?: number; phone_number?: number };
  conflicts?: Record<string, number>;
  drafts?: { name: string; compatibility: string; enabled_in_source: boolean }[];
  time_range?: { first?: string | null; last?: string | null };
  missing_from_source?: number;
  discovery_check?: unknown;
  /** Written by `brain-import verify`. */
  verification?: Record<string, unknown>;
  /** Written by a rollback: what it deleted and reverted. */
  rollback?: Record<string, unknown>;
}

/** ImportRunOut: every import_runs column; `report` comes only with the single run. */
export interface ImportRun {
  id: number;
  source_system: string;
  label: string;
  status: "running" | "completed" | "failed" | "interrupted" | "rolled_back";
  scope: Record<string, unknown>;
  source: Record<string, unknown>;
  importer_version: string;
  created_by: string;
  started_at: string;
  heartbeat_at: string;
  finished_at: string | null;
  checkpoint: Record<string, unknown>;
  counts: Record<string, unknown>;
  report?: ImportReport;
  error: string;
  rolled_back_at: string | null;
  rolled_back_by: string;
}

/** GET /api/brain/imports/{id}. `reviewed_items`: items of this run reviewed or edited since (a rollback refuses them unless
 * they are discarded). */
export interface ImportRunDetail {
  run: ImportRun;
  records_by_action: Record<string, number>;
  records_by_type: Record<string, number>;
  can_roll_back: boolean;
  reviewed_items: number;
}

/** GET /api/brain/overview. Counts follow the person's scope; viewers get archive totals only. */
export interface BrainOverview {
  memory: { total: number; by_status?: Record<string, number>; by_kind?: Record<string, number>; by_agent?: Record<string, number> };
  archive: { sessions: number; messages: number; withheld_messages?: number; first_at?: string | null; last_at?: string | null; by_staff?: Record<string, number> };
  review?: { pending_items: number; quarantined_items: number; open_conflicts: number };
  workflows?: { drafts: number; by_status?: Record<string, number> };
  imports: { runs: number; latest: ImportRun | null };
  /** The owner's setting brain_ai_context: "off" (default: memory never goes into AI prompts) or "approved_general". */
  ai_context: string;
  instance_label?: string | null;
}

/** GET /api/brain/agents: what each agent reads (policy.describe()), where it reads memory, and counts. Counts may be absent
 * (Credit Repair's need the sensitive-data permission). */
export interface BrainAgent {
  agent: string;
  title: string;
  reads_shared: boolean;
  reads_own: boolean;
  reads_other_agents: boolean;
  reads_customer_items: boolean;
  archive: "all" | "own" | "none";
  statuses: string[];
  used_by: string[];
  items?: { approved: number; by_kind?: Record<string, number> };
  sessions?: number;
}
