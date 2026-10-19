import { useState } from "react";
import { api, type Schedule } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon } from "../icons";
import "./Schedules.css";

export default function Schedules() {
  const { canOperate } = useAuth();
  const list = usePoll(() => api.get<{ schedules: Schedule[] }>("/api/schedules"), 15_000);
  const [error, setError] = useState<string | null>(null);

  async function patch(s: Schedule, body: Partial<Schedule>) {
    setError(null);
    try {
      await api.patch(`/api/schedules/${s.id}`, body);
      await list.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const all = list.data?.schedules ?? [];
  const enabled = all.filter((s) => s.enabled);
  const next = enabled.filter((s) => s.next_run_at).sort((a, b) => Date.parse(a.next_run_at!) - Date.parse(b.next_run_at!))[0];
  const last = all.filter((s) => s.last_run_at).sort((a, b) => Date.parse(b.last_run_at!) - Date.parse(a.last_run_at!))[0];

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Automation</div>
          <h1>Schedules</h1>
          <p className="muted">Agent routines run by the worker (times in the server's time zone, cron syntax). Cross-system workflows run in n8n.</p>
        </div>
      </header>
      {list.data ? (
        <div className="tiles sc-tiles">
          <Tile
            icon="calendar"
            tone="purple"
            label="Active routines"
            hint={all.length > enabled.length ? `${all.length - enabled.length} paused` : "None paused"}
          >
            <span className="tile-value tabular">{enabled.length}</span>
            <span className="tile-of">of {all.length}</span>
          </Tile>
          <Tile icon="clock" tone="cyan" label="Next run" hint={next ? <span className="sc-clip">{next.name}</span> : "Nothing is scheduled"}>
            <span className="tile-value sc-time">{next ? <Time iso={next.next_run_at} /> : "—"}</span>
          </Tile>
          <Tile icon="history" tone="blue" label="Last run" hint={last ? <span className="sc-clip">{last.name}</span> : "No routine has run yet"}>
            <span className="tile-value sc-time">{last ? <Time iso={last.last_run_at} /> : "—"}</span>
          </Tile>
        </div>
      ) : null}
      <ErrorNote error={list.error ?? error} />
      <Card
        title={
          <>
            <Icon name="calendar" size={18} className="card-icon" /> Agent routines
          </>
        }
        actions={canOperate ? <span className="muted small">Cron changes save when you leave the field</span> : null}
      >
        {list.data?.schedules.length ? (
          <div className="table-wrap">
            <table className="table sc-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Runs</th>
                  <th>Cron</th>
                  <th>Next</th>
                  <th>Last</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {list.data.schedules.map((s) => (
                  <tr key={s.id} className={s.enabled ? "" : "sc-paused"}>
                    <td className="sc-name">
                      <strong>{s.name}</strong>
                      <div className="muted small">{s.description}</div>
                    </td>
                    <td className="sc-runs" data-label="Runs">
                      <code className="sc-cap" title={`${s.agent}.${s.capability}`}>
                        {s.capability}
                      </code>
                      <span className="sc-agent">{s.agent}</span>
                    </td>
                    <td className="sc-cron" data-label="Cron">
                      {canOperate ? (
                        <input
                          aria-label={`Cron for ${s.name}`}
                          className="mono cron"
                          defaultValue={s.cron}
                          onBlur={(e) => e.target.value !== s.cron && void patch(s, { cron: e.target.value })}
                        />
                      ) : (
                        <code>{s.cron}</code>
                      )}
                    </td>
                    <td className="sc-nowrap sc-next" data-label="Next">
                      {s.enabled ? <Time iso={s.next_run_at} /> : "—"}
                    </td>
                    <td className="sc-nowrap sc-last muted" data-label="Last">
                      <Time iso={s.last_run_at} />
                    </td>
                    <td className="sc-nowrap sc-state-cell">
                      {canOperate ? (
                        <span className="sc-state">
                          <Pill value={s.enabled ? "up" : "disabled"} label={s.enabled ? "enabled" : "paused"} />
                          <button className="btn small" onClick={() => void patch(s, { enabled: !s.enabled })}>
                            {s.enabled ? "Pause" : "Enable"}
                          </button>
                        </span>
                      ) : (
                        <Pill value={s.enabled ? "up" : "disabled"} label={s.enabled ? "enabled" : "paused"} />
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty>No schedules.</Empty>
        )}
      </Card>
    </div>
  );
}
