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
  llm: { configured: boolean; provider: string; model: string | null; requests_today: number; daily_cap: number; reason?: string };
  workflow_engine?: string;
  edition?: { code: string; title: string; source: string };
  n8n: IntegrationState | null;
  counts: {
    sites: Record<string, number>;
    tasks_24h: Record<string, number>;
    tasks_queued: number;
    tasks_running: number;
    approvals_pending: number;
    events_24h: Record<string, number>;
    drafts: number;
  };
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
