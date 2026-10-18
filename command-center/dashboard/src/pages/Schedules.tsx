import { useState } from "react";
import { api, type Schedule } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, Time } from "../components";
import { usePoll } from "../hooks";

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

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Schedules</h1>
          <p className="muted">Agent routines run by the worker (times in the server's time zone, cron syntax). Cross-system workflows run in n8n.</p>
        </div>
      </header>
      <ErrorNote error={list.error ?? error} />
      <Card>
        {list.data?.schedules.length ? (
          <table className="table">
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
                <tr key={s.id}>
                  <td>
                    <strong>{s.name}</strong>
                    <div className="muted small">{s.description}</div>
                  </td>
                  <td>
                    <code>
                      {s.agent}.{s.capability}
                    </code>
                  </td>
                  <td>
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
                  <td>{s.enabled ? <Time iso={s.next_run_at} /> : "—"}</td>
                  <td>
                    <Time iso={s.last_run_at} />
                  </td>
                  <td>
                    {canOperate ? (
                      <button className="btn small" onClick={() => void patch(s, { enabled: !s.enabled })}>
                        {s.enabled ? "Pause" : "Enable"}
                      </button>
                    ) : (
                      <Pill value={s.enabled ? "up" : "disabled"} label={s.enabled ? "enabled" : "paused"} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty>No schedules.</Empty>
        )}
      </Card>
    </div>
  );
}
