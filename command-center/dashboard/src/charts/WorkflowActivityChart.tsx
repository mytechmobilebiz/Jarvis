import { KeyboardEvent, PointerEvent, useEffect, useMemo, useRef, useState } from "react";
import type { ActivityRange, TaskActivity } from "../api";

/** Legend and drawing order. Colours follow the category, never its rank, and were validated as a set in this order
 * (see agents.css); a category the API adds later is appended in grey. */
export const CATEGORY_ORDER = ["website_scans", "seo", "email", "content", "reports", "server_checks", "credit_repair", "repairs"];

export const CATEGORY_COLOR: Record<string, string> = {
  website_scans: "var(--agent-monitoring)",
  seo: "var(--agent-seo_content)",
  email: "var(--agent-communications)",
  content: "var(--agent-social_marketing)",
  reports: "var(--series-reports)",
  server_checks: "var(--agent-server_management)",
  credit_repair: "var(--agent-credit_repair)",
  repairs: "var(--agent-wordpress_security)",
};

export const RANGE_LABEL: Record<ActivityRange, string> = { "24h": "last 24 hours", "7d": "last 7 days", "30d": "last 30 days" };
const PER: Record<TaskActivity["bucket"], string> = { hour: "per hour", "6 hours": "per 6 hours", day: "per day" };
const STEP_MS: Record<TaskActivity["bucket"], number> = { hour: 3_600_000, "6 hours": 6 * 3_600_000, day: 86_400_000 };

const PAD = { top: 10, right: 12, bottom: 22, left: 32 };
const MIN_HEIGHT = 160;
const TIP_W = 190;

export interface Series {
  key: string;
  label: string;
  color: string;
  total: number;
}

/** The categories in legend order with their colours and totals. */
export function seriesOf(data: TaskActivity): Series[] {
  const known = CATEGORY_ORDER.map((k) => data.categories.find((c) => c.key === k)).filter((c): c is { key: string; label: string } => !!c);
  const extra = data.categories.filter((c) => !CATEGORY_ORDER.includes(c.key));
  return [...known, ...extra].map((c) => ({ key: c.key, label: c.label, color: CATEGORY_COLOR[c.key] ?? "var(--agent-other)", total: data.totals[c.key] ?? 0 }));
}

/** A rounded top for the y axis and its ticks: [0, 1, 2, 3, 4] or [0, 10, 20, 30, 40]. */
export function yTicks(max: number): number[] {
  if (max <= 4) return [0, 1, 2, 3, 4];
  const raw = max / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  return [0, 1, 2, 3, 4].map((i) => i * step);
}

function hour(d: Date): string {
  return d.toLocaleTimeString([], { hour: "numeric" });
}

/** The bucket's time span for the tooltip and the table: "Mon 10 AM–11 AM", "Mon 6 AM–12 PM", "Mon, Oct 19". */
export function bucketLabel(start: Date, bucket: TaskActivity["bucket"]): string {
  if (bucket === "day") return start.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  const end = new Date(start.getTime() + STEP_MS[bucket]);
  return `${start.toLocaleDateString([], { weekday: "short" })} ${hour(start)}–${hour(end)}`;
}

/** Axis label for bucket i, or null to leave it unlabelled. */
function axisLabel(start: Date, i: number, n: number, bucket: TaskActivity["bucket"]): string | null {
  if (bucket === "hour") return start.getHours() % 4 === 0 ? hour(start) : null;
  if (bucket === "6 hours") return start.getHours() < 6 ? start.toLocaleDateString([], { weekday: "short" }) : null;
  const every = Math.max(1, Math.ceil(n / 6));
  return (n - 1 - i) % every === 0 ? start.toLocaleDateString([], { month: "short", day: "numeric" }) : null;
}

/** Tasks finished per category over time, one 2px line each, with a crosshair tooltip, keyboard reading (one tab stop;
 * the arrow keys, Home and End move between buckets) and a table view. */
export default function WorkflowActivityChart({ data, dim }: { data: TaskActivity; dim?: boolean }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [{ width, height }, setSize] = useState({ width: 560, height: MIN_HEIGHT });
  const [active, setActive] = useState<number | null>(null);
  const [focused, setFocused] = useState(false);
  const [table, setTable] = useState(false);

  useEffect(() => {
    const el = wrap.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([entry]) =>
      setSize({ width: Math.max(220, Math.floor(entry.contentRect.width)), height: Math.max(MIN_HEIGHT, Math.floor(entry.contentRect.height)) }),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, [table]);

  const series = useMemo(() => seriesOf(data), [data]);
  const starts = useMemo(() => data.buckets.map((b) => new Date(b.start)), [data]);
  const n = data.buckets.length;
  const grand = series.reduce((a, s) => a + s.total, 0);
  const drawn = series.filter((s) => s.total > 0);
  const max = Math.max(0, ...data.buckets.flatMap((b) => drawn.map((s) => b.counts[s.key] ?? 0)));
  const ticks = yTicks(max);
  const top = ticks[ticks.length - 1];
  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const x = (i: number) => PAD.left + (n <= 1 ? plotW / 2 : (i / (n - 1)) * plotW);
  const y = (v: number) => PAD.top + plotH - (v / top) * plotH;
  const range = RANGE_LABEL[data.range] ?? data.range;
  const current = active === null || active >= n ? null : active;

  function readout(i: number): string {
    const b = data.buckets[i];
    const parts = series.map((s) => `${b.counts[s.key] ?? 0} ${s.label}`);
    return `${bucketLabel(starts[i], data.bucket)}: ${parts.join(", ")}${b.failed ? `; ${b.failed} failed` : ""}`;
  }

  function onKeyDown(e: KeyboardEvent<SVGSVGElement>) {
    if (!n) return;
    const at = current ?? n - 1;
    const next = e.key === "ArrowRight" ? at + 1 : e.key === "ArrowLeft" ? at - 1 : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : null;
    if (next === null) return;
    e.preventDefault();
    setActive(Math.max(0, Math.min(n - 1, next)));
  }

  function onPointerMove(e: PointerEvent<SVGSVGElement>) {
    const box = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - box.left;
    const i = n <= 1 ? 0 : Math.round(((px - PAD.left) / plotW) * (n - 1));
    setActive(Math.max(0, Math.min(n - 1, i)));
  }

  const legend = (
    <div className="wfa-side">
      <ul className="wfa-legend" aria-label={`Tasks finished, ${range}, by kind`}>
        {series.map((s) => (
          <li key={s.key}>
            <i style={{ background: s.color }} aria-hidden />
            <span className="wfa-legend-label">{s.label}</span>
            <strong className="tabular">{s.total.toLocaleString()}</strong>
          </li>
        ))}
      </ul>
      <div className="wfa-side-foot">
        {data.failed ? <span className="muted small">{data.failed.toLocaleString()} of these failed</span> : <span />}
        <button type="button" className="btn ghost small" onClick={() => setTable((t) => !t)} aria-pressed={table}>
          {table ? "Show chart" : "View as table"}
        </button>
      </div>
    </div>
  );

  if (table) {
    return (
      <div className={`wfa ${dim ? "chart-dim" : ""}`}>
        <div className="table-wrap wfa-table" tabIndex={0} role="region" aria-label={`Tasks finished ${PER[data.bucket]}, ${range}`}>
          <table className="table compact">
            <thead>
              <tr>
                <th scope="col">{data.bucket === "day" ? "Day" : "Starting"}</th>
                {series.map((s) => (
                  <th key={s.key} scope="col">
                    {s.label}
                  </th>
                ))}
                <th scope="col">Failed</th>
              </tr>
            </thead>
            <tbody>
              {data.buckets.map((b, i) => (
                <tr key={b.start}>
                  <th scope="row">{bucketLabel(starts[i], data.bucket)}</th>
                  {series.map((s) => (
                    <td key={s.key} className="tabular">
                      {b.counts[s.key] ?? 0}
                    </td>
                  ))}
                  <td className="tabular">{b.failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {legend}
      </div>
    );
  }

  const tip = current !== null ? data.buckets[current] : null;
  const tipLeft = current !== null ? (x(current) + 12 + TIP_W > width ? x(current) - 12 - TIP_W : x(current) + 12) : 0;

  return (
    <div className={`wfa ${dim ? "chart-dim" : ""}`}>
      <div className="wfa-plot" ref={wrap}>
        <svg
          width={width}
          height={height}
          tabIndex={0}
          role="group"
          aria-label={`Tasks finished ${PER[data.bucket]}, ${range}: ${grand.toLocaleString()} in total. Left and right arrow keys read each ${data.bucket === "day" ? "day" : "period"}.`}
          className={focused ? "wfa-focused" : undefined}
          onKeyDown={onKeyDown}
          onFocus={() => {
            setFocused(true);
            setActive((a) => (a === null ? n - 1 : a));
          }}
          onBlur={() => {
            setFocused(false);
            setActive(null);
          }}
          onPointerMove={onPointerMove}
          onPointerLeave={() => !focused && setActive(null)}
        >
          {ticks.map((t) => (
            <g key={t}>
              <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} className={t === 0 ? "chart-axis" : "chart-grid"} />
              <text x={PAD.left - 8} y={y(t)} className="chart-tick" textAnchor="end" dominantBaseline="middle">
                {t.toLocaleString()}
              </text>
            </g>
          ))}
          {starts.map((d, i) => {
            const label = axisLabel(d, i, n, data.bucket);
            return label ? (
              <text key={i} x={x(i)} y={height - 6} className="chart-tick" textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"}>
                {label}
              </text>
            ) : null;
          })}
          {drawn.map((s) => (
            <polyline
              key={s.key}
              className="wfa-line"
              points={data.buckets.map((b, i) => `${x(i).toFixed(1)},${y(b.counts[s.key] ?? 0).toFixed(1)}`).join(" ")}
              style={{ stroke: s.color }}
            />
          ))}
          {current !== null ? (
            <g className="wfa-cross">
              <line x1={x(current)} x2={x(current)} y1={PAD.top} y2={PAD.top + plotH} />
              {drawn.map((s) => (
                <circle key={s.key} cx={x(current)} cy={y(data.buckets[current].counts[s.key] ?? 0)} r={4} style={{ fill: s.color }} />
              ))}
            </g>
          ) : null}
        </svg>
        {!grand ? <div className="wfa-empty">No tasks finished in the {range}</div> : null}
        {tip && current !== null ? (
          <div className="chart-tooltip wfa-tooltip" style={{ left: Math.max(0, tipLeft), width: TIP_W }} aria-hidden>
            <div className="muted small">{bucketLabel(starts[current], data.bucket)}</div>
            {series.map((s) => (
              <div key={s.key} className="chart-tooltip-row">
                <i style={{ background: s.color }} />
                <span className="wfa-tip-label">{s.label}</span>
                <strong className="tabular">{tip.counts[s.key] ?? 0}</strong>
              </div>
            ))}
            {tip.failed ? <div className="small text-bad">{tip.failed} failed</div> : null}
          </div>
        ) : null}
        <div className="sr-only" aria-live="polite">
          {focused && current !== null ? readout(current) : ""}
        </div>
      </div>
      {legend}
    </div>
  );
}
