import { useState } from "react";
import { api, type CCEvent, type Integration } from "../api";
import { useAuth } from "../App";
import { Card, ErrorNote, Pill, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import "./Integrations.css";

const ICONS: Record<string, IconName> = {
  n8n: "workflows",
  webhook: "workflows",
  anthropic: "sparkles",
  email: "mail",
  postonomous: "megaphone",
  wordpress_publish: "globe",
  ads: "trending",
  postal_mail: "send",
};

/** The latest recorded call failed: an error newer than the last success. */
function failing(i: Integration): boolean {
  const s = i.status;
  if (!s?.last_error_at) return false;
  return !s.last_ok_at || new Date(s.last_error_at).getTime() > new Date(s.last_ok_at).getTime();
}

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
    <div className="n8n-test in-test">
      <button className="btn small" onClick={() => void send()}>
        <Icon name="send" size={14} /> Send test event through n8n
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

  // Summary of the same list the cards show.
  const all = list.data?.integrations ?? [];
  const loaded = !!list.data;
  const pending = list.error ?? "…";
  const inMode = (mode: Integration["mode"]) => all.filter((i) => i.mode === mode);
  const live = inMode("live");
  const failingNow = all.filter(failing);

  return (
    <div className="page in-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">System</div>
          <h1>Integrations</h1>
          <p className="muted">
            <strong>Live</strong> integrations really run. <strong>Mock</strong> integrations record what they would do and send nothing; each needs your
            authorization and credentials before it can be made live.
          </p>
        </div>
      </header>

      <div className="tiles cols-4">
        <Tile icon="plug" tone="green" label="Live" hint={loaded ? (live.length ? live.map((i) => i.title).join(" · ") : "Nothing runs for real yet") : pending}>
          <span className="tile-value tabular">{loaded ? live.length : "—"}</span>
          {loaded ? <span className="tile-of">of {all.length}</span> : null}
        </Tile>
        <Tile icon="file" tone="cyan" label="Mock" hint={loaded ? "Record what they would do; send nothing" : pending}>
          <span className="tile-value tabular">{loaded ? inMode("mock").length : "—"}</span>
        </Tile>
        <Tile icon="lock" tone="muted" label="Disabled" hint={loaded ? "Need a key or configuration first" : pending}>
          <span className="tile-value tabular">{loaded ? inMode("disabled").length : "—"}</span>
        </Tile>
        <Tile
          icon="alert"
          tone={failingNow.length ? "bad" : "green"}
          label="Failing now"
          hint={loaded ? (failingNow.length ? failingNow.map((i) => i.title).join(" · ") : "No integration's latest call failed") : pending}
        >
          <span className={`tile-value tabular ${failingNow.length ? "text-bad" : ""}`}>{loaded ? failingNow.length : "—"}</span>
        </Tile>
      </div>

      <ErrorNote error={list.error} />
      <div className="in-grid">
        {(list.data?.integrations ?? []).map((i) => (
          <Card
            key={i.name}
            className={`in-card in-${i.mode}`}
            title={
              <>
                <span className="in-logo" aria-hidden>
                  <Icon name={ICONS[i.name] ?? "plug"} size={18} />
                </span>
                <span className="in-title">{i.title}</span>
              </>
            }
            actions={<Pill value={i.mode} label={i.mode.toUpperCase()} />}
          >
            <p className="muted in-desc">{i.description}</p>
            {i.live_requirements ? (
              <p className="small in-req">
                <strong>To go live:</strong> {i.live_requirements}
              </p>
            ) : null}
            {i.status ? (
              <ul className="in-status small">
                <li>
                  <span className={`dot dot-${i.status.last_ok_at ? "good" : "muted"}`} />
                  <span>
                    Last success <Time iso={i.status.last_ok_at} />
                  </span>
                </li>
                {i.status.last_error_at ? (
                  <li>
                    <span className={`dot dot-${failing(i) ? "bad" : "warn"}`} />
                    <span className="in-error-line">
                      Last error <Time iso={i.status.last_error_at} />:{" "}
                      <span className="text-bad in-error" title={i.status.last_error}>
                        {i.status.last_error}
                      </span>
                    </span>
                  </li>
                ) : null}
              </ul>
            ) : null}
            {i.name === "n8n" ? (
              <div className="in-n8n">
                <p className="small muted">
                  Events webhook: <code className="in-url">{i.webhook_url}</code>
                </p>
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
              </div>
            ) : null}
          </Card>
        ))}
      </div>
    </div>
  );
}
