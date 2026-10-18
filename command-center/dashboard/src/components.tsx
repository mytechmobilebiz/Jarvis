import { ReactNode, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { Finding, Risk, Severity } from "./api";
import { Icon, type IconName } from "./icons";

export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "never";
  const seconds = Math.round((now - new Date(iso).getTime()) / 1000);
  const future = seconds < 0;
  const s = Math.abs(seconds);
  const unit =
    s < 45 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min` : s < 86400 ? `${Math.round(s / 3600)} h` : `${Math.round(s / 86400)} d`;
  if (unit === "just now") return unit;
  return future ? `in ${unit}` : `${unit} ago`;
}

export function Time({ iso }: { iso: string | null | undefined }) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 30_000);
    return () => clearInterval(id);
  }, []);
  return <time title={iso ? new Date(iso).toLocaleString() : ""}>{timeAgo(iso)}</time>;
}

const STATUS_TONE: Record<string, string> = {
  up: "good",
  succeeded: "good",
  completed: "good",
  approved: "good",
  sent: "good",
  live: "good",
  degraded: "warn",
  awaiting_approval: "warn",
  pending: "warn",
  warning: "warn",
  mock: "info",
  planning: "info",
  queued: "info",
  running: "info",
  info: "muted",
  down: "bad",
  failed: "bad",
  critical: "bad",
  rejected: "bad",
  cancelled: "muted",
  unknown: "muted",
  skipped: "muted",
  disabled: "muted",
  draft: "info",
  archived: "muted",
  production: "warn",
  staging: "info",
  test: "muted",
};

export function Pill({ value, label }: { value: string | undefined | null; label?: string }) {
  value = value ?? "unknown";
  const text = label ?? value.replace(/_/g, " ");
  return <span className={`pill tone-${STATUS_TONE[value] ?? "muted"}`}>{label ? text : text.charAt(0).toUpperCase() + text.slice(1)}</span>;
}

const RISK_LABEL: Record<Risk, string> = {
  read: "read-only",
  draft: "draft only",
  external: "external · approval",
  change: "change · approval",
  financial: "money · approval",
  destructive: "destructive · approval",
};

export function RiskBadge({ risk }: { risk: Risk }) {
  return <span className={`risk risk-${risk}`}>{RISK_LABEL[risk]}</span>;
}

export function SeverityIcon({ severity }: { severity: Severity }) {
  const symbol = severity === "critical" ? "●" : severity === "warning" ? "▲" : "○";
  return (
    <span className={`sev sev-${severity}`} aria-label={severity}>
      {symbol}
    </span>
  );
}

export function Findings({ findings, includeInfo = false }: { findings: Finding[]; includeInfo?: boolean }) {
  const order: Record<Severity, number> = { critical: 0, warning: 1, info: 2 };
  const shown = findings.filter((f) => includeInfo || f.severity !== "info").sort((a, b) => order[a.severity] - order[b.severity]);
  if (!shown.length) return <p className="muted">No issues found.</p>;
  return (
    <ul className="findings">
      {shown.map((f, i) => (
        <li key={`${f.code}-${i}`}>
          <SeverityIcon severity={f.severity} />
          <div>
            <strong>{f.title}</strong>
            {f.detail ? <div className="muted small">{f.detail}</div> : null}
          </div>
        </li>
      ))}
    </ul>
  );
}

export function Card({ title, actions, children, className = "" }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`card ${className}`}>
      {title || actions ? (
        <header className="card-head">
          {title ? <h2>{title}</h2> : <span />}
          {actions ? <div className="card-actions">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}

export function Stat({ label, value, tone, hint }: { label: string; value: ReactNode; tone?: string; hint?: ReactNode }) {
  return (
    <div className={`stat ${tone ? `tone-${tone}` : ""}`}>
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {hint ? <div className="stat-hint">{hint}</div> : null}
    </div>
  );
}

export function ErrorNote({ error }: { error: string | null | undefined }) {
  if (!error) return null;
  return (
    <div className="alert alert-bad" role="alert">
      {error}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function JsonView({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}

/** Minimal Markdown (headings, lists, bold, paragraphs) rendered as React elements: no raw HTML. */
export function Markdown({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let list: ReactNode[] = [];
  const flush = () => {
    if (list.length) {
      blocks.push(<ul key={`ul-${blocks.length}`}>{list}</ul>);
      list = [];
    }
  };
  const inline = (s: string): ReactNode[] =>
    s.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
      part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <strong key={i}>{part.slice(2, -2)}</strong> : part,
    );
  text.split("\n").forEach((line, i) => {
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    const item = /^\s*[-*]\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      const level = Math.min(heading[1].length + 1, 5);
      const Tag = `h${level}` as "h2";
      blocks.push(<Tag key={i}>{inline(heading[2])}</Tag>);
    } else if (item) {
      list.push(<li key={i}>{inline(item[1])}</li>);
    } else if (line.trim() === "") {
      flush();
    } else {
      flush();
      blocks.push(<p key={i}>{inline(line)}</p>);
    }
  });
  flush();
  return <div className="markdown">{blocks}</div>;
}

export function Modal({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`modal ${wide ? "modal-wide" : ""}`} role="dialog" aria-modal="true" aria-label={title}>
        <header className="card-head">
          <h2>{title}</h2>
          <button className="btn ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

/** A metric card (see tiles.css): icon, label, big value, optional hint and link. Tones: blue, purple, cyan, orange, green, warn, bad, muted. */
export function Tile({ icon, tone, label, children, hint, to }: { icon: IconName; tone: string; label: string; children: ReactNode; hint?: ReactNode; to?: string }) {
  return (
    <section className={`tile tile-${tone}`} aria-label={label}>
      <div className="tile-head">
        <span className="tile-icon" aria-hidden>
          <Icon name={icon} size={18} />
        </span>
        <span className="tile-label">{label}</span>
        {to ? (
          <Link className="tile-link" to={to} aria-label={`Open ${label}`}>
            <Icon name="chevronRight" size={16} />
          </Link>
        ) : null}
      </div>
      <div className="tile-body">{children}</div>
      {hint ? <div className="tile-hint">{hint}</div> : null}
    </section>
  );
}
