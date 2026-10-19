// Middle-row side panels: the PC's resources (not measurable from inside Docker, so "Not connected": owner request 5)
// and Service Status, where every row says what was checked and when (owner request 3).
import { Link } from "react-router-dom";
import type { ServiceRow } from "./service";

export function PcResources() {
  return (
    <section className="dash-panel pc-panel" aria-labelledby="pc-title">
      <div className="dash-panel-head">
        <h2 id="pc-title">PC Resources</h2>
      </div>
      <div className="pc-body">
        <svg width={40} height={40} viewBox="0 0 24 24" aria-hidden className="pc-glyph">
          <g fill="none" stroke="currentColor" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="4" width="18" height="12" rx="2" />
            <path d="M9 20h6M12 16v4" />
          </g>
        </svg>
        <p className="pc-state">
          <span className="dot dot-muted" aria-hidden /> Not connected
        </p>
        <p className="pc-note">The Command Center runs inside Docker and cannot measure this PC's processor, memory or disk.</p>
      </div>
    </section>
  );
}

export function ServiceStatus({ rows }: { rows: ServiceRow[] }) {
  return (
    <section className="dash-panel service-panel" aria-labelledby="service-title">
      <div className="dash-panel-head">
        <h2 id="service-title">Service Status</h2>
        <Link className="btn outline small" to="/integrations" aria-label="View all services and integrations">
          View All
        </Link>
      </div>
      <ul className="service-list">
        {rows.map((r) => (
          <li key={r.key} title={r.title}>
            <span className={`dot dot-${r.tone}`} aria-hidden />
            <span className="service-name">{r.name}</span>
            <span className={`service-state tone-text-${r.tone}`}>{r.state}</span>
            {r.detail ? <span className="service-detail">{r.detail}</span> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
