import { KeyboardEvent, useEffect, useRef, useState } from "react";

export interface HourBucket {
  start: Date;
  completed: number;
  failed: number;
}

/* Validated (dataviz validator, dark surface #0d1426): blue/red pass every check; green/red did not (CVD). */
const SERIES = [
  { key: "completed", label: "Completed", color: "#3b82f6" },
  { key: "failed", label: "Failed", color: "#ef4444" },
] as const;

const PAD = { top: 12, right: 8, bottom: 26, left: 34 };
const GAP = 2;
const MIN_HEIGHT = 240;

function niceMax(v: number): number {
  if (v <= 4) return 4;
  const step = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * step >= v) return m * step;
  return 10 * step;
}

function hourLabel(d: Date): string {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Stacked columns per hour (completed, failed) for the last 24 hours, with hover/focus tooltips and a table view.
 * The bars are one tab stop; the arrow keys, Home and End move between the hours. */
export default function TaskActivityChart({ buckets, dim }: { buckets: HourBucket[]; dim?: boolean }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [{ width, height: HEIGHT }, setSize] = useState({ width: 640, height: MIN_HEIGHT });
  const [hover, setHover] = useState<number | null>(null);
  const [active, setActive] = useState(0);
  const [table, setTable] = useState(false);
  const bars = useRef<(SVGGElement | null)[]>([]);

  useEffect(() => {
    const el = wrap.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    // The plot fills whatever height its card has (CSS min-height MIN_HEIGHT), so it lines up with neighbouring panels.
    const ro = new ResizeObserver(([entry]) =>
      setSize({ width: Math.max(280, Math.floor(entry.contentRect.width)), height: Math.max(MIN_HEIGHT, Math.floor(entry.contentRect.height)) }),
    );
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const total = (b: HourBucket) => b.completed + b.failed;
  const max = niceMax(Math.max(0, ...buckets.map(total)));
  const plotW = width - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;
  const slot = plotW / Math.max(1, buckets.length);
  const barW = Math.min(24, Math.max(4, slot - 6));
  const y = (v: number) => PAD.top + plotH - (v / max) * plotH;
  const ticks = [0, max / 2, max];
  const sum = buckets.reduce((a, b) => ({ completed: a.completed + b.completed, failed: a.failed + b.failed }), { completed: 0, failed: 0 });
  const shown = hover !== null ? buckets[hover] : null;
  const current = Math.min(active, Math.max(0, buckets.length - 1));

  function onKeyDown(e: KeyboardEvent<SVGSVGElement>) {
    const last = buckets.length - 1;
    const next = e.key === "ArrowRight" ? current + 1 : e.key === "ArrowLeft" ? current - 1 : e.key === "Home" ? 0 : e.key === "End" ? last : null;
    if (next === null || last < 0) return;
    e.preventDefault();
    const i = Math.max(0, Math.min(last, next));
    setActive(i);
    bars.current[i]?.focus();
  }

  return (
    <div className={`chart ${dim ? "chart-dim" : ""}`}>
      <div className="chart-top">
        <div className="chart-legend" aria-hidden>
          {SERIES.map((s) => (
            <span key={s.key}>
              <i style={{ background: s.color }} /> {s.label} <strong className="tabular">{sum[s.key]}</strong>
            </span>
          ))}
        </div>
        <button className="btn ghost small" onClick={() => setTable((t) => !t)}>
          {table ? "Show chart" : "View as table"}
        </button>
      </div>
      {table ? (
        <div className="table-wrap chart-table">
          <table className="table compact">
            <thead>
              <tr>
                <th>Hour starting</th>
                <th>Completed</th>
                <th>Failed</th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((b) => (
                <tr key={b.start.toISOString()}>
                  <td>{hourLabel(b.start)}</td>
                  <td className="tabular">{b.completed}</td>
                  <td className="tabular">{b.failed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart-plot" ref={wrap} style={{ minHeight: MIN_HEIGHT }}>
          <svg
            width={width}
            height={HEIGHT}
            role="group"
            aria-label={`Tasks per hour over the last 24 hours: ${sum.completed} completed, ${sum.failed} failed`}
            onKeyDown={onKeyDown}
          >
            {ticks.map((t) => (
              <g key={t}>
                <line x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} className="chart-grid" />
                <text x={PAD.left - 8} y={y(t)} className="chart-tick" textAnchor="end" dominantBaseline="middle">
                  {t}
                </text>
              </g>
            ))}
            {buckets.map((b, i) => {
              const x = PAD.left + i * slot + (slot - barW) / 2;
              const segments: { v: number; color: string }[] = [
                { v: b.completed, color: SERIES[0].color },
                { v: b.failed, color: SERIES[1].color },
              ].filter((s) => s.v > 0);
              let base = 0;
              const label = `${hourLabel(b.start)}: ${b.completed} completed, ${b.failed} failed`;
              return (
                <g
                  key={b.start.toISOString()}
                  ref={(el) => {
                    bars.current[i] = el;
                  }}
                  tabIndex={i === current ? 0 : -1}
                  role="img"
                  aria-label={label}
                  className={`chart-bar ${hover === i ? "hovered" : ""}`}
                  onPointerEnter={() => setHover(i)}
                  onPointerLeave={() => setHover(null)}
                  onFocus={() => {
                    setHover(i);
                    setActive(i);
                  }}
                  onBlur={() => setHover(null)}
                >
                  <rect x={PAD.left + i * slot} y={PAD.top} width={slot} height={plotH} className="chart-hit" />
                  {segments.map((s, k) => {
                    const top = k === segments.length - 1;
                    const y0 = y(base + s.v);
                    const h = Math.max(1, y(base) - y0 - (k > 0 ? GAP : 0));
                    base += s.v;
                    const r = top ? Math.min(4, barW / 2, h) : 0;
                    // Rounded data-end (top), square at the baseline.
                    const d = `M${x},${y0 + h} V${y0 + r} Q${x},${y0} ${x + r},${y0} H${x + barW - r} Q${x + barW},${y0} ${x + barW},${y0 + r} V${y0 + h} Z`;
                    return <path key={k} d={d} fill={s.color} />;
                  })}
                </g>
              );
            })}
            <line x1={PAD.left} x2={width - PAD.right} y1={y(0)} y2={y(0)} className="chart-axis" />
            {buckets.map((b, i) =>
              i % 6 === 0 ? (
                <text key={i} x={PAD.left + i * slot + slot / 2} y={HEIGHT - 8} className="chart-tick" textAnchor="middle">
                  {hourLabel(b.start)}
                </text>
              ) : null,
            )}
          </svg>
          {shown && hover !== null ? (
            <div
              className="chart-tooltip"
              style={{ left: Math.min(width - 150, Math.max(0, PAD.left + hover * slot + slot / 2 - 75)), top: Math.max(0, y(total(shown)) - 74) }}
            >
              <div className="muted small">
                {hourLabel(shown.start)}–{hourLabel(new Date(shown.start.getTime() + 3_600_000))}
              </div>
              {SERIES.map((s) => (
                <div key={s.key} className="chart-tooltip-row">
                  <i style={{ background: s.color }} />
                  <strong className="tabular">{shown[s.key]}</strong> <span className="muted">{s.label.toLowerCase()}</span>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
