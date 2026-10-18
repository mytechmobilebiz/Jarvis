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
  del: <T>(path: string) => request<T>("DELETE", path),
};

export type Severity = "info" | "warning" | "critical";
export type Risk = "read" | "draft" | "external" | "financial" | "destructive";

export interface User {
  id: number;
  email: string;
  display_name: string;
  role: "admin" | "operator" | "viewer";
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
  title: string;
  description: string;
  enabled: boolean;
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
  details: { params?: Json; capability_description?: string };
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
  details: Json & { deep?: boolean; plugins_detected?: string[]; themes_detected?: string[]; latest_wp_version?: string };
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
  llm: { configured: boolean; provider: string; model: string | null; requests_today: number; daily_cap: number };
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
