// The five stat cards under the title: AI Agents, Websites, Tasks Today, Website Uptime, and the date card with the
// system line. Every figure comes from the API; loading shows "—" and a failed request says "Unavailable".
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Agent, Site, SystemStatus, TaskActivity } from "../api";
import { AgentIcon } from "../agentStyle";
import type { StatusMeta } from "../status";
import {
  agentStatus,
  clock,
  formatUptime,
  MIN_UPTIME_CHECKS,
  shortDate,
  specialists,
  systemLine,
  tasksTrend,
  uptimeTone,
  websiteUptime,
  type Tone,
} from "./model";

export interface Loaded<T> {
  data: T | null;
  error: string | null;
}

type BadgeTone = "blue" | "indigo" | "green" | "orange";

function Glyph({ name }: { name: "globe" | "check" | "clock" }) {
  return (
    <svg width={34} height={34} viewBox="0 0 24 24" aria-hidden focusable="false" className="stat-glyph">
      {name === "globe" ? (
        <>
          <circle cx="12" cy="12" r="9.5" fill="currentColor" fillOpacity={0.22} />
          <g fill="none" stroke="currentColor" strokeWidth={1.7}>
            <circle cx="12" cy="12" r="9.5" />
            <path d="M2.5 12h19M12 2.5c2.6 2.6 3.9 5.8 3.9 9.5s-1.3 6.9-3.9 9.5M12 2.5C9.4 5.1 8.1 8.3 8.1 12s1.3 6.9 3.9 9.5M4.2 7h15.6M4.2 17h15.6" />
          </g>
        </>
      ) : name === "check" ? (
        <>
          <circle cx="12" cy="12" r="9.5" fill="currentColor" fillOpacity={0.22} />
          <circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth={1.9} />
          <path d="m7.6 12.3 3 3 5.8-6.1" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round" strokeLinejoin="round" />
        </>
      ) : (
        <>
          <circle cx="12" cy="12" r="9.5" fill="currentColor" fillOpacity={0.22} />
          <circle cx="12" cy="12" r="9.5" fill="none" stroke="currentColor" strokeWidth={1.9} />
          <path d="M12 6.8V12l3.6 2.2" fill="none" stroke="currentColor" strokeWidth={2.1} strokeLinecap="round" strokeLinejoin="round" />
        </>
      )}
    </svg>
  );
}

function Calendar() {
  return (
    <svg width={36} height={36} viewBox="0 0 24 24" aria-hidden focusable="false" className="stat-calendar">
      <g fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round">
        <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" />
        <path d="M3.5 9.5h17M8 3v4M16 3v4" />
      </g>
      <g fill="currentColor">
        {[7.5, 10.5, 13.5, 16.5].map((x) => (
          <circle key={x} cx={x} cy={13} r={0.9} />
        ))}
        {[7.5, 10.5, 13.5].map((x) => (
          <circle key={x} cx={x} cy={16.4} r={0.9} />
        ))}
      </g>
    </svg>
  );
}

/** One linked stat card: a round badge, the value, the label and a sub-line whose tone carries the state. */
export function StatCard({
  to,
  badge,
  tone,
  value,
  label,
  sub,
  subTone,
  title,
  note,
}: {
  to: string;
  badge: ReactNode;
  tone: BadgeTone;
  value: string;
  /** A short addition beside the value, e.g. "2 failed". */
  note?: ReactNode;
  label: string;
  sub: ReactNode;
  /** neutral: plain secondary text, for a change that is neither good nor bad (more tasks is not better in itself). */
  subTone: Tone | "neutral";
  title?: string;
}) {
  return (
    <Link className="stat-card" to={to} title={title}>
      <span className={`stat-badge badge-${tone}`} aria-hidden>
        {badge}
      </span>
      <span className="stat-text">
        <span className="stat-value tabular">
          {value}
          {/* The comma keeps a screen reader from running the value and the note together ("39, 2 failed", not "392 failed"). */}
          {note ? (
            <>
              <span className="sr-only">,</span> {note}
            </>
          ) : null}
        </span>
        <span className="stat-label">{label}</span>
        <span className={`stat-sub tone-text-${subTone}`}>{sub}</span>
      </span>
    </Link>
  );
}

/** A card whose last request failed keeps its last figure, but does not present it as current. */
const NOT_UPDATED = "Not updated";

function agentsCard(agents: Loaded<Agent[]>) {
  if (!agents.data) return { value: "—", sub: agents.error ? "Unavailable" : "Loading…", tone: (agents.error ? "bad" : "muted") as Tone };
  const list = specialists(agents.data);
  const on = list.filter((a) => a.enabled && a.entitled);
  // The last good list is still on screen, but it says nothing about now.
  if (agents.error) return { value: String(on.length), sub: NOT_UPDATED, tone: "unknown" as Tone };
  if (!on.length) return { value: "0", sub: "None active", tone: "muted" as Tone };
  const tones = on.map((a) => agentStatus(a).tone);
  const attention = tones.filter((t) => t === "warn").length;
  const parts = [
    ...(on.length < list.length ? [`${on.length} of ${list.length} active`] : []),
    ...(attention ? [`${attention} need${attention === 1 ? "s" : ""} attention`] : []),
  ];
  if (parts.length) return { value: String(on.length), sub: parts.join(" · "), tone: "warn" as Tone };
  // Green only when every agent that is on has really run (C2-06); switched on alone stays neutral.
  return { value: String(on.length), sub: "Active", tone: (tones.every((t) => t === "good") ? "good" : "muted") as Tone };
}

export function sitesCard(sites: Loaded<Site[]>, status: SystemStatus | null = null) {
  if (!sites.data) return { value: "—", sub: sites.error ? "Unavailable" : "Loading…", tone: (sites.error ? "bad" : "muted") as Tone };
  const enabled = sites.data.filter((s) => s.enabled);
  const down = enabled.filter((s) => s.last_status === "down").length;
  const degraded = enabled.filter((s) => s.last_status === "degraded").length;
  const unchecked = enabled.filter((s) => !s.last_checked_at).length;
  const value = String(enabled.length);
  if (sites.error) return { value, sub: NOT_UPDATED, tone: "unknown" as Tone };
  if (!sites.data.length) return { value, sub: "Add a website", tone: "muted" as Tone };
  if (!enabled.length) return { value, sub: "All paused", tone: "muted" as Tone };
  // Offline, nothing is checked: the statuses on screen are from before (C2-22).
  if (status?.connectivity?.state === "offline") return { value, sub: "Not checked · no internet", tone: "unknown" as Tone };
  const notChecked = unchecked ? ` · ${unchecked} not checked yet` : "";
  if (down) return { value, sub: `${down} down${degraded ? ` · ${degraded} degraded` : ""}${notChecked}`, tone: "bad" as Tone };
  if (degraded) return { value, sub: `${degraded} degraded${notChecked}`, tone: "warn" as Tone };
  if (unchecked) return { value, sub: unchecked === enabled.length ? "Not checked yet" : `Monitored${notChecked}`, tone: "muted" as Tone };
  return { value, sub: "Monitored", tone: "muted" as Tone };
}

function TrendArrow({ dir }: { dir: "up" | "down" | "same" | null }) {
  if (dir !== "up" && dir !== "down") return null;
  return (
    <svg width={13} height={13} viewBox="0 0 24 24" aria-hidden className="stat-arrow">
      <path d={dir === "up" ? "M12 20V5M5.5 11 12 4.5 18.5 11" : "M12 4v15M5.5 13l6.5 6.5 6.5-6.5"} fill="none" stroke="currentColor" strokeWidth={2.6} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export default function StatCards({
  agents,
  sites,
  activity,
  status,
  statusMeta,
  now,
}: {
  agents: Loaded<Agent[]>;
  sites: Loaded<Site[]>;
  activity: Loaded<TaskActivity>;
  status: SystemStatus | null;
  statusMeta: StatusMeta;
  now: number;
}) {
  const a = agentsCard(agents);
  const s = sitesCard(sites, status);
  const up = sites.data ? websiteUptime(sites.data, now) : null;
  const trend = activity.data ? tasksTrend(activity.data) : null;
  const failedToday = activity.data?.today.failed ?? 0;
  const line = systemLine(status, statusMeta, now);
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const date = new Date(now);

  let upValue = "—";
  let upSub = sites.error ? "Unavailable" : "Loading…";
  let upTone: Tone = sites.error ? "bad" : "muted";
  if (up && sites.error) {
    if (up.value !== null && up.checks >= MIN_UPTIME_CHECKS) upValue = formatUptime(up.value);
    [upSub, upTone] = [NOT_UPDATED, "unknown"];
  } else if (up) {
    if (up.value === null) [upSub, upTone] = ["No checks yet", "muted"];
    else if (up.checks < MIN_UPTIME_CHECKS) [upSub, upTone] = ["Not enough data yet", "muted"];
    else {
      upValue = formatUptime(up.value);
      const period = up.since ? `since ${shortDate(up.since)}` : "last 30 days";
      const tone = uptimeTone(up.value);
      // Below target the words say so; the colour only repeats it.
      upSub = tone === "bad" ? `Below 98% · ${period}` : tone === "warn" ? `Below 99.5% · ${period}` : period.charAt(0).toUpperCase() + period.slice(1);
      upTone = tone === "good" ? "muted" : tone;
    }
  }

  return (
    <div className="stat-row">
      <StatCard to="/agents" badge={<AgentIcon name="robot" size={34} color="currentColor" />} tone="blue" value={a.value} label="AI Agents" sub={a.sub} subTone={a.tone} />
      <StatCard to="/websites" badge={<Glyph name="globe" />} tone="indigo" value={s.value} label="Websites" sub={s.sub} subTone={s.tone} />
      <StatCard
        to="/tasks"
        badge={<Glyph name="check" />}
        tone="green"
        value={activity.data ? activity.data.today.succeeded.toLocaleString() : "—"}
        label="Tasks Today"
        note={failedToday ? <span className="stat-note tone-text-bad">{failedToday.toLocaleString()} failed</span> : null}
        title="Tasks completed since midnight (your time), compared with yesterday up to the same time. The AI Supervisor's own planning steps are not counted."
        sub={
          activity.data && activity.error ? (
            NOT_UPDATED
          ) : activity.data && trend ? (
            <>
              <TrendArrow dir={trend.dir} />
              {trend.text}
            </>
          ) : activity.error ? (
            "Unavailable"
          ) : (
            "Loading…"
          )
        }
        subTone={activity.error ? (activity.data ? "unknown" : "bad") : trend?.dir ? "neutral" : "muted"}
      />
      <StatCard
        to="/websites"
        badge={<Glyph name="clock" />}
        tone="orange"
        value={upValue}
        label="Website Uptime"
        sub={upSub}
        subTone={upTone}
        title={
          up && up.value !== null
            ? `Share of ${up.checks.toLocaleString()} checks of ${up.sites} monitored site${up.sites === 1 ? "" : "s"} that found the site up (degraded counts as up), last 30 days. Time nobody checked, such as the PC switched off, is not counted.`
            : undefined
        }
      />
      <section className="stat-card stat-date" aria-label="Date, time and system status">
        <Calendar />
        <span className="stat-text">
          <span className="stat-day">{date.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric", year: "numeric" })}</span>
          <time className="stat-time tabular" dateTime={date.toISOString()} title={zone}>
            {clock(now)}
          </time>
          <span className={`stat-system tone-text-${line.tone}`} role="status">
            <span className={`dot dot-${line.tone}`} aria-hidden /> {line.text}
          </span>
        </span>
      </section>
    </div>
  );
}
