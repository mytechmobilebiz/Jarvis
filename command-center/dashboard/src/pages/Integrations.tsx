import { useState } from "react";
import { api, type CCEvent, type Integration } from "../api";
import { useAuth } from "../App";
import { Card, ErrorNote, Pill, Time } from "../components";
import { usePoll } from "../hooks";

function N8nTest() {
  const [eventId, setEventId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const event = usePoll(
    () => (eventId ? api.get<{ event: CCEvent }>(`/api/events/${eventId}`) : Promise.resolve(null)),
    eventId ? 1_500 : 0,
    [eventId],
  );
  const e = event.data?.event;

  async function send() {
    setError(null);
    try {
      const r = await api.post<{ event: CCEvent }>("/api/integrations/n8n/test");
      setEventId(r.event.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="n8n-test">
      <button className="btn small" onClick={() => void send()}>
        Send test event through n8n
      </button>
      {e ? (
        <span className="small">
          {" "}
          Event #{e.id}: forwarding <Pill value={e.forward_status} />
          {e.forward_error ? <span className="text-bad"> {e.forward_error}</span> : null}
          {" · "}
          {e.n8n_ack_at ? (
            <>
              <Pill value="succeeded" label="acknowledged by n8n" /> execution {e.n8n_execution_id}
            </>
          ) : (
            "waiting for n8n's acknowledgement…"
          )}
        </span>
      ) : null}
      <ErrorNote error={error} />
    </div>
  );
}

export default function Integrations() {
  const { canOperate } = useAuth();
  const list = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 15_000);
  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Integrations</h1>
          <p className="muted">
            <strong>Live</strong> integrations really run. <strong>Mock</strong> integrations record what they would do and send nothing; each needs your
            authorization and credentials before it can be made live.
          </p>
        </div>
      </header>
      <ErrorNote error={list.error} />
      <div className="agent-grid">
        {(list.data?.integrations ?? []).map((i) => (
          <Card
            key={i.name}
            title={
              <>
                {i.title} <Pill value={i.mode} label={i.mode.toUpperCase()} />
              </>
            }
          >
            <p className="muted">{i.description}</p>
            {i.live_requirements ? (
              <p className="small">
                <strong>To go live:</strong> {i.live_requirements}
              </p>
            ) : null}
            {i.status ? (
              <p className="small">
                Last success <Time iso={i.status.last_ok_at} />
                {i.status.last_error_at ? (
                  <>
                    {" "}
                    · last error <Time iso={i.status.last_error_at} />: <span className="text-bad">{i.status.last_error}</span>
                  </>
                ) : null}
              </p>
            ) : null}
            {i.name === "n8n" ? (
              <>
                <p className="small muted">Events webhook: {i.webhook_url}</p>
                {i.workflows ? (
                  <ul className="list small">
                    {i.workflows.map((w) => (
                      <li key={w.id}>
                        <Pill value={w.active ? "up" : "disabled"} label={w.active ? "active" : "inactive"} /> {w.name}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="small muted">Set N8N_API_KEY to list workflows here. Provisioned: event router, morning briefing, lead intake.</p>
                )}
                {canOperate ? <N8nTest /> : null}
              </>
            ) : null}
          </Card>
        ))}
      </div>
    </div>
  );
}
