// Honest labels and small pieces several pages share (0.5.0 audit fixes): what was really delivered (C2-01, C2-02, C2-11),
// how much data an uptime figure rests on (C2-07), how old a connection check is (C2-04), schedule cadences built from the
// real schedules (C2-15), sample-data marks (C2-12) and typed confirmations the API asks for (C3-05, C3-06).
import { FormEvent, ReactNode, useState } from "react";
import { ApiError, type IntegrationState, type LeadRecord, type Schedule, type Ticket, type Uptime } from "../api";
import { Modal } from "../components";
import "./shared.css";

// ---------------------------------------------------------------------------------------------------------------------
// Delivery: "sent" only when a live integration carried it.

/** How the email a lead's or ticket's status refers to was carried (0.5.0 API): "live" really sent, "mock" recorded in the
 * Outbox only, null no email recorded. Absent from older APIs. */
export type DeliveryMode = "mock" | "live" | null;

export type LeadWithDelivery = LeadRecord & { delivery_mode?: DeliveryMode; delivery_outbox_id?: number | null };
export type TicketWithDelivery = Ticket & { delivery_mode?: DeliveryMode; delivery_outbox_id?: number | null };

/** What a mock integration did not do, by integration name (backend app/integrations). */
const NOT_DONE: Record<string, string> = {
  email: "not sent",
  postonomous: "not posted",
  wordpress_publish: "not published",
  postal_mail: "not mailed",
  ads: "not applied",
};

/** "Recorded (mock), not sent" for email, "… not mailed" for letters, and so on. */
export function mockLabel(integration: string): string {
  return `Recorded (mock), ${NOT_DONE[integration] ?? "not carried out"}`;
}

/** [Pill value, label] for an approved email: the sent label only when it went out live (C2-01). */
export function emailDelivery(mode: DeliveryMode | undefined, sentLabel: string): [string, string] {
  if (mode === "live") return ["sent", sentLabel];
  if (mode === "mock") return ["mock", mockLabel("email")];
  return ["unknown", "Approved · delivery not recorded"];
}

// Lead statuses (backend agents/sales.py) as [Pill value, label]; Pill values pick the tone (components.tsx).
const LEAD_STATUS: Record<string, [string, string]> = {
  new: ["queued", "New"],
  qualified: ["up", "Qualified"],
  proposal_drafted: ["draft", "Proposal drafted"],
  spam: ["failed", "Spam"],
};

// Ticket statuses (backend agents/support.py): open -> triaged -> drafted -> replied, or closed.
const TICKET_STATUS: Record<string, [string, string]> = {
  open: ["pending", "Open"],
  triaged: ["queued", "Triaged"],
  drafted: ["draft", "Reply drafted"],
  closed: ["archived", "Closed"],
};

function fallback(status: string): [string, string] {
  const text = status.replace(/_/g, " ");
  return ["unknown", text.charAt(0).toUpperCase() + text.slice(1)];
}

export function leadStatus(lead: LeadWithDelivery): [string, string] {
  if (lead.status === "proposal_sent") return emailDelivery(lead.delivery_mode, "Proposal sent");
  return LEAD_STATUS[lead.status] ?? fallback(lead.status);
}

export function ticketStatus(ticket: TicketWithDelivery): [string, string] {
  if (ticket.status === "replied") return emailDelivery(ticket.delivery_mode, "Replied");
  return TICKET_STATUS[ticket.status] ?? fallback(ticket.status);
}

/** Records in `sentStatus` split by how their email was carried. */
export function deliveryCounts<T extends { status: string; delivery_mode?: DeliveryMode }>(rows: T[], sentStatus: string) {
  const sent = rows.filter((r) => r.status === sentStatus);
  const live = sent.filter((r) => r.delivery_mode === "live").length;
  const mock = sent.filter((r) => r.delivery_mode === "mock").length;
  return { live, mock, unknown: sent.length - live - mock };
}

/** "2 sent · 1 recorded (mock), not sent": only the parts that are not zero; `none` when all are. */
export function deliverySummary(c: { live: number; mock: number; unknown: number }, sentWord: string, none: string): string {
  const parts = [
    c.live ? `${c.live} ${sentWord}` : "",
    c.mock ? `${c.mock} recorded (mock), not sent` : "",
    c.unknown ? `${c.unknown} approved, delivery not recorded` : "",
  ].filter(Boolean);
  return parts.join(" · ") || none;
}

// ---------------------------------------------------------------------------------------------------------------------
// Uptime with its coverage (C2-07).

/** Below this many checks a window's percentage is not shown: it would say more than the data does. */
export const MIN_CHECKS = 4;
export type UptimeWindow = "24h" | "7d" | "30d";
const WINDOW_MS: Record<UptimeWindow, number> = { "24h": 86_400_000, "7d": 7 * 86_400_000, "30d": 30 * 86_400_000 };

export interface Coverage {
  /** The percentage, or null when there is none or too little data behind it. */
  pct: number | null;
  /** Checks behind it; null when the API does not report it (before 0.5.0). */
  checks: number | null;
  /** The first check, when it falls inside the window (a young site: the figure covers only the time since). */
  since: string | null;
  /** "14 checks since 05:27", "Not enough data yet (2 checks since 05:27)", "No checks in this window". */
  text: string;
}

/** "05:27" for today, "12 Oct" otherwise, in the viewer's locale. */
export function shortWhen(iso: string, now: number = Date.now()): string {
  const d = new Date(iso);
  return d.toDateString() === new Date(now).toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString([], { day: "numeric", month: "short" });
}

export function coverage(u: Uptime | null | undefined, w: UptimeWindow, now: number = Date.now()): Coverage {
  const n = !u ? 0 : w === "30d" ? (u.n_30d ?? u.checks_30d) : (u[`n_${w}`] ?? null);
  const first = u?.first_check_at && Date.parse(u.first_check_at) > now - WINDOW_MS[w] ? u.first_check_at : null;
  const since = first ? ` since ${shortWhen(first, now)}` : "";
  const raw = u?.[w] ?? null;
  if (n === null) return { pct: raw, checks: null, since: first, text: raw == null ? "No checks in this window" : "Number of checks not reported" };
  if (!n || raw == null) return { pct: null, checks: n, since: first, text: "No checks in this window" };
  const checks = `${n} check${n === 1 ? "" : "s"}${since}`;
  if (n < MIN_CHECKS) return { pct: null, checks: n, since: first, text: `Not enough data yet (${checks})` };
  return { pct: raw, checks: n, since: first, text: checks };
}

/** "99.87%", or "100%" when it rounds to it. */
export function pctText(v: number): string {
  return `${v.toFixed(v >= 99.995 ? 0 : 2)}%`;
}

// ---------------------------------------------------------------------------------------------------------------------
// Connection states with their age (C2-04).

/** n8n is probed every 60 s while a worker runs; an older success is no longer a current "connected". */
export const FRESH_MS = 3 * 60_000;

/** "40 s ago", "12 min ago", "3 h ago", "2 d ago". */
export function ageText(iso: string, now: number = Date.now()): string {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86_400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86_400)} d ago`;
}

/** "10:21" today, "Mon 12 Oct, 10:21" before. */
function sinceText(iso: string, now: number): string {
  const d = new Date(iso);
  return d.toDateString() === new Date(now).toDateString()
    ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export type ConnTone = "good" | "warn" | "bad" | "muted";
export interface Connection {
  /** The whole state with its age: "Connected · checked 40 s ago". */
  label: string;
  tone: ConnTone;
  /** The state alone ("Connected") and its age ("checked 40 s ago", "" when there is none), for places that show them apart. */
  state: string;
  age: string;
}

function conn(state: string, age: string, tone: ConnTone): Connection {
  return { label: age ? `${state} · ${age}` : state, state, age, tone };
}

function newer(a: string | null | undefined, b: string | null | undefined): boolean {
  return !!a && (!b || Date.parse(a) > Date.parse(b));
}

/** The state of an integration that is checked on a timer (n8n): "Connected · checked 40 s ago" only within FRESH_MS. */
export function probedConnection(mode: string, st: IntegrationState | null | undefined, now: number = Date.now()): Connection {
  if (mode === "disabled") return conn("Not connected", "", "muted");
  if (!st || (!st.last_ok_at && !st.last_error_at)) return conn("Not checked yet", "", "muted");
  if (newer(st.last_error_at, st.last_ok_at)) {
    const unreachable = (st.detail as { reachable?: boolean } | undefined)?.reachable === false;
    return conn(unreachable ? "Unreachable" : "Last check failed", ageText(st.last_error_at!, now), unreachable ? "bad" : "warn");
  }
  const ok = st.last_ok_at!;
  if (now - Date.parse(ok) <= FRESH_MS) return conn("Connected", `checked ${ageText(ok, now)}`, "good");
  return conn(`Not checked since ${sinceText(ok, now)}`, "", "warn");
}

/** An integration that is never probed (the webhook engine): only its deliveries say anything, never "connected". */
export function deliveredConnection(mode: string, st: IntegrationState | null | undefined, now: number = Date.now()): Connection {
  if (mode === "disabled") return conn("Not connected", "", "muted");
  if (st && newer(st.last_error_at, st.last_ok_at)) return conn("Last delivery failed", ageText(st.last_error_at!, now), "warn");
  if (st?.last_ok_at) return conn(`Last successful delivery ${ageText(st.last_ok_at, now)}`, "", "muted");
  return conn("No delivery yet", "", "muted");
}

// ---------------------------------------------------------------------------------------------------------------------
// Schedules (C2-15): cadences from the real schedules. The worker evaluates cron in UTC.

const DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"];

/** Plain-English reading of the common cron forms ("Every 15 minutes", "Daily at 03:17 UTC"), or null. */
export function cronHint(cron: string): string | null {
  const f = cron.trim().split(/\s+/);
  if (f.length !== 5) return null;
  const [mi, h, dom, mon, dow] = f;
  const num = (v: string, min: number, max: number) => (/^\d{1,2}$/.test(v) && +v >= min && +v <= max ? +v : null);
  const step = (v: string) => /^\*\/(\d{1,2})$/.exec(v)?.[1];
  const pad = (n: number) => String(n).padStart(2, "0");
  if (mon !== "*") return null;
  if (h === "*" && dom === "*" && dow === "*") {
    if (mi === "*") return "Every minute";
    const n = Number(step(mi));
    // Only steps that divide the hour fire at an even interval.
    if (n >= 1 && 60 % n === 0) return n === 1 ? "Every minute" : `Every ${n} minutes`;
    const m = num(mi, 0, 59);
    if (m !== null) return m === 0 ? "Hourly, on the hour" : `Hourly at :${pad(m)}`;
    return null;
  }
  const m = num(mi, 0, 59);
  if (m === null) return null;
  const hs = Number(step(h));
  if (hs >= 1 && 24 % hs === 0 && dom === "*" && dow === "*") return hs === 1 ? `Hourly at :${pad(m)}` : `Every ${hs} hours at :${pad(m)} UTC`;
  const hr = num(h, 0, 23);
  if (hr === null) return null;
  const at = `${pad(hr)}:${pad(m)} UTC`;
  if (dom === "*" && dow === "*") return `Daily at ${at}`;
  if (dom === "*") {
    if (dow === "1-5") return `Weekdays at ${at}`;
    if (dow === "0,6" || dow === "6,0") return `Weekends at ${at}`;
    const d = num(dow, 0, 7);
    return d === null ? null : `${DAYS[d % 7]} at ${at}`;
  }
  const day = num(dom, 1, 31);
  if (dow === "*" && day !== null) return `Monthly on day ${day} at ${at}`;
  return null;
}

/** How often the schedules running `agent.capability` run: "every 15 minutes", "paused", "not scheduled"; lower case, for a sentence. */
export function cadence(schedules: Schedule[], agent: string, capability: string): string {
  const mine = schedules.filter((s) => s.agent === agent && s.capability === capability);
  if (!mine.length) return "not scheduled";
  const on = mine.filter((s) => s.enabled);
  if (!on.length) return "paused";
  const hints = on.map((s) => {
    const hint = cronHint(s.cron);
    return hint ? hint.charAt(0).toLowerCase() + hint.slice(1) : `on cron ${s.cron} (UTC)`;
  });
  return Array.from(new Set(hints)).join(" and ");
}

// ---------------------------------------------------------------------------------------------------------------------
// Sample data (C2-12).

/** Whether a task result or artifact holds synthetic ad figures (backend agents/ads.py sets data.sample_data). */
export function isSample(data: unknown): boolean {
  return !!data && typeof data === "object" && (data as { sample_data?: unknown }).sample_data === true;
}

export function SampleDataPill() {
  return (
    <span className="pill tone-warn" title="Synthetic figures: no ad account is connected">
      Sample data
    </span>
  );
}

// ---------------------------------------------------------------------------------------------------------------------
// Typed confirmations (C3-05, C3-06).

/** The API's reason when a change needs the target's name typed (HTTP 400 "confirm: …"), or null for any other error. */
export function confirmReason(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 400 || !err.message.startsWith("confirm:")) return null;
  return err.message.slice("confirm:".length).trim();
}

/** A dialog that asks for `name` to be typed before `onConfirm` runs. The button stays disabled until it matches exactly. */
export function TypedConfirm({
  title,
  name,
  action,
  children,
  error,
  onConfirm,
  onCancel,
}: {
  title: string;
  name: string;
  action: string;
  children: ReactNode;
  error?: string | null;
  onConfirm: (typed: string) => Promise<void> | void;
  onCancel: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const matches = typed.trim() === name;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!matches) return;
    setBusy(true);
    try {
      await onConfirm(typed.trim());
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={title} onClose={onCancel}>
      <form className="typed-confirm" onSubmit={submit}>
        <div className="alert alert-warn typed-confirm-text" role="alert">
          {children}
        </div>
        <label className="typed-confirm-label">
          <span>
            Type <strong className="typed-confirm-name">{name}</strong> to confirm
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
            {action}
          </button>
          <button className="btn" type="button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}
