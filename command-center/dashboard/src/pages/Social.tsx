import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Agent, type Artifact, type Capability, type Integration, type OutboxRecord, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Modal, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { ArtifactView, RunModal, TaskDetail } from "../work";
import { mockLabel, SampleDataPill } from "./shared";
import "./Social.css";

const AGENT = "social_marketing";
// The artifact kind draft_social_posts writes (backend/app/agents/content.py); the agent's ad reports are left to SEO & Marketing.
const DRAFT_KIND = "social_drafts";
const SOCIAL_CAPS = ["draft_social_posts", "publish_draft"];
const DRAFT_LIMIT = 200;
const OUTBOX_LIMIT = 500;
const RECENT = 20;
const SHOWN = 8;

type TaskPage = { tasks: Task[]; total: number };
type DraftFilter = "all" | "draft" | "approved";

const PLATFORM_LABEL: Record<string, string> = {
  facebook: "Facebook",
  instagram: "Instagram",
  linkedin: "LinkedIn",
  x: "X",
  google_business: "Google Business",
};

const CAP_INFO: Record<string, { label: string; icon: IconName }> = {
  draft_social_posts: { label: "Draft social posts", icon: "file" },
  publish_draft: { label: "Publish a reviewed draft", icon: "send" },
};

function capLabel(name: string): string {
  return CAP_INFO[name]?.label ?? name.replace(/_/g, " ");
}

function platformLabel(p: string): string {
  return PLATFORM_LABEL[p] ?? p.replace(/_/g, " ");
}

function platformsOf(a: Artifact): string[] {
  const p = a.data?.platforms;
  return Array.isArray(p) ? p.filter((x): x is string => typeof x === "string") : [];
}

function plural(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}

/** The subject, or the first non-empty line of the body without Markdown heading or emphasis marks. */
function firstLine(e: OutboxRecord): string {
  if (e.subject) return e.subject;
  const line = e.body.split("\n").find((l) => l.trim());
  return line ? line.replace(/^\s*#+\s*/, "").replace(/[*_]{1,2}/g, "").trim() : "";
}

function scheduledFor(e: OutboxRecord): string {
  const when = e.payload?.scheduled_for;
  return typeof when === "string" ? when : "";
}

function formatWhen(value: string): string {
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

/** Why the agent cannot run right now, or null when it can. */
function unavailable(agent: Agent | undefined): string | null {
  if (!agent) return null;
  if (!agent.entitled) return `${agent.title} is not included in this edition, so it cannot run.`;
  if (!agent.enabled) return `${agent.title} is switched off, so nothing it does will run.`;
  return null;
}

function modeLabel(mode: Integration["mode"]): string {
  return mode === "live" ? "Live" : mode === "mock" ? "Mock" : "Disabled";
}

/**
 * publish_draft as this page offers it: the schema is shared with WordPress publishing, so drop site_id and
 * describe a social destination; when a draft is given, pre-fill its id and first platform. Still goes through
 * RunModal, so the server holds it for approval.
 */
function socialPublish(cap: Capability, draft?: Artifact): Capability {
  const platforms = draft ? platformsOf(draft) : [];
  const props = Object.fromEntries(
    Object.entries(cap.params_schema.properties ?? {})
      .filter(([key]) => key !== "site_id")
      .map(([key, prop]) => {
        if (key === "artifact_id" && draft) return [key, { ...prop, default: draft.id }];
        if (key === "destination")
          return [
            key,
            {
              ...prop,
              ...(platforms[0] ? { default: platforms[0] } : {}),
              description: platforms.length
                ? `Social platform to post to; this draft was written for ${platforms.join(", ")}.`
                : "Social platform to post to, one the draft was written for (shown on the Social page).",
            },
          ];
        return [key, prop];
      }),
  );
  return { ...cap, params_schema: { ...cap.params_schema, properties: props } };
}

function ActionCard({ cap, blocked, onRun }: { cap: Capability; blocked: string | null; onRun?: () => void }) {
  const info = CAP_INFO[cap.name] ?? { label: cap.name, icon: "sparkles" as IconName };
  const body = (
    <>
      <span className={`so-action-icon so-tone-${cap.name === "publish_draft" ? "cyan" : "purple"}`} aria-hidden>
        <Icon name={info.icon} size={18} />
      </span>
      <span className="so-action-main">
        <span className="so-action-name">{info.label}</span>
        <span className="so-action-desc">{cap.description}</span>
        <span className="so-action-meta">
          <RiskBadge risk={cap.risk} />
          {cap.requires_approval ? (
            <span className="so-approval">
              <Icon name="lock" size={12} /> Needs approval
            </span>
          ) : null}
        </span>
      </span>
      {onRun ? <Icon name="chevronRight" size={16} className="so-action-go" /> : null}
    </>
  );
  if (!onRun) return <div className="so-action so-action-static">{body}</div>;
  return (
    <button type="button" className="so-action" onClick={onRun} disabled={!!blocked} title={blocked ?? undefined}>
      {body}
    </button>
  );
}

export default function Social() {
  const { canOperate } = useAuth();
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 30_000);
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 120_000);
  // Artifact lists carry no content, so the newest 200 social drafts are cheap and also give the counts.
  const drafts = usePoll(() => api.get<{ artifacts: Artifact[] }>(`/api/artifacts?agent=${AGENT}&kind=${DRAFT_KIND}&limit=${DRAFT_LIMIT}`), 15_000);
  const outbox = usePoll(() => api.get<{ entries: OutboxRecord[] }>(`/api/outbox?limit=${OUTBOX_LIMIT}`), 15_000);
  const recent = usePoll(() => api.get<TaskPage>(`/api/tasks?agent=${AGENT}&limit=${RECENT}`), 10_000);
  const waiting = usePoll(() => api.get<TaskPage>(`/api/tasks?agent=${AGENT}&status=awaiting_approval&limit=200`), 15_000);

  const [running, setRunning] = useState<Capability | null>(null);
  const [openTask, setOpenTask] = useState<number | null>(null);
  const [openDraft, setOpenDraft] = useState<number | null>(null);
  const [openPost, setOpenPost] = useState<OutboxRecord | null>(null);
  const [filter, setFilter] = useState<DraftFilter>("all");

  const agent = agents.data?.agents.find((a) => a.name === AGENT);
  const blocked = unavailable(agent);
  const agentState = !agent ? "muted" : !agent.entitled ? "bad" : agent.enabled ? "good" : "warn";
  const caps = SOCIAL_CAPS.map((name) => agent?.capabilities.find((c) => c.name === name)).filter((c): c is Capability => !!c);
  const publishCap = caps.find((c) => c.name === "publish_draft");

  const integration = (name: string) => integrations.data?.integrations.find((i) => i.name === name);
  const postonomous = integration("postonomous");
  const ai = integration("anthropic");
  const mode = postonomous?.mode;

  const allDrafts = drafts.data?.artifacts ?? [];
  const toReview = allDrafts.filter((a) => a.status === "draft");
  const reviewed = allDrafts.filter((a) => a.status === "approved");
  const draftsCapped = allDrafts.length >= DRAFT_LIMIT;
  const shownDrafts = (filter === "draft" ? toReview : filter === "approved" ? reviewed : allDrafts).slice(0, SHOWN);

  const posts = useMemo(() => (outbox.data?.entries ?? []).filter((e) => e.integration === "postonomous"), [outbox.data]);
  const livePosts = posts.filter((e) => e.mode === "live").length;
  const outboxCapped = (outbox.data?.entries.length ?? 0) >= OUTBOX_LIMIT;
  const waitingPublish = (waiting.data?.tasks ?? []).filter((t) => t.capability === "publish_draft").length;
  const socialTasks = (recent.data?.tasks ?? []).filter((t) => SOCIAL_CAPS.includes(t.capability));

  function refreshWork() {
    void drafts.refresh();
    void recent.refresh();
    void waiting.refresh();
    void outbox.refresh();
  }

  const stages: { key: string; icon: IconName; value: number | null; label: string; hint: string; to?: string }[] = [
    { key: "drafted", icon: "file", value: drafts.data ? toReview.length : null, label: "Drafted", hint: "waiting for review" },
    { key: "reviewed", icon: "check", value: drafts.data ? reviewed.length : null, label: "Reviewed", hint: "ready to publish" },
    { key: "approval", icon: "lock", value: waiting.data ? waitingPublish : null, label: "Approval", hint: "requests waiting", to: "/approvals" },
    {
      key: "posted",
      icon: "send",
      value: outbox.data ? posts.length : null,
      label: mode === "live" ? "Published" : "Recorded",
      hint: mode === "mock" ? "simulated, in the Outbox" : mode === "disabled" ? "publishing is off" : "through Postonomous",
      to: "/outbox",
    },
  ];

  return (
    <div className="page so-page">
      <header className="page-head">
        <div>
          <div className="eyebrow">Social</div>
          <h1>Social Media / Postonomous</h1>
          <p className="muted">
            The Social Media &amp; Marketing agent drafts posts for each platform for you to review. A post is published through Postonomous only after a
            person approves the publish request; reviewing a draft never publishes it.
          </p>
        </div>
        <div className="row-actions">
          <Link className="btn" to="/drafts">
            <Icon name="file" size={16} /> Drafts &amp; reports
          </Link>
          <Link
            className="btn primary"
            to="/approvals"
            aria-label={waitingPublish ? `Approvals, ${plural(waitingPublish, "social post")} waiting` : undefined}
            title={waitingPublish ? `${plural(waitingPublish, "social post")} waiting for approval` : undefined}
          >
            <Icon name="approvals" size={16} /> Approvals
            {waitingPublish ? <span className="btn-count">{waitingPublish}</span> : null}
          </Link>
        </div>
      </header>

      <ErrorNote error={agents.error} />
      {agents.data && !agent ? (
        <div className="alert alert-bad" role="alert">
          The Social Media &amp; Marketing agent is not registered on this Command Center.
        </div>
      ) : null}
      {blocked ? (
        <div className={`alert so-alert ${agent?.entitled ? "so-alert-warn" : "alert-bad"}`} role="status">
          <Icon name="alert" size={18} className="so-alert-icon" />
          <div>
            <strong>{agent?.entitled ? "Social Media & Marketing is switched off." : "Social Media & Marketing is not in this edition."}</strong>{" "}
            {agent?.entitled ? "Drafting and publishing will not run until it is switched back on." : "It cannot run until your licence includes it."} History
            below stays visible.
          </div>
        </div>
      ) : null}

      <div className="tiles cols-4">
        <Tile
          icon="megaphone"
          tone={agentState === "good" ? "green" : agentState}
          label="Social agent"
          to="/agents"
          hint={
            !agent ? (
              agents.error ? "Could not load agents" : "…"
            ) : agent.last_run_at ? (
              <>
                Last ran <Time iso={agent.last_run_at} />
              </>
            ) : (
              "Not run yet"
            )
          }
        >
          <span className={`tile-status ${agentState === "good" ? "text-good" : agentState === "bad" ? "text-bad" : agentState === "warn" ? "text-warn" : "muted"}`}>
            {!agent ? "Checking…" : !agent.entitled ? "Not in this edition" : agent.enabled ? "Active" : "Switched off"}
          </span>
          {ai ? (
            <span className="so-tile-line">
              <span className={`dot dot-${ai.mode === "live" ? "good" : "muted"}`} aria-hidden />
              {ai.mode === "live" ? `AI writing: ${ai.model ?? "Claude"}` : "No AI model: drafts use templates"}
            </span>
          ) : null}
        </Tile>

        <Tile
          icon="file"
          tone="purple"
          label="Drafts to review"
          to="/drafts"
          hint={
            drafts.data
              ? allDrafts.length
                ? `${reviewed.length.toLocaleString()} reviewed · ${allDrafts.filter((a) => a.status === "rejected").length.toLocaleString()} rejected`
                : "No social drafts yet"
              : drafts.error
                ? "Could not load drafts"
                : "…"
          }
        >
          <span className="tile-value tabular">{drafts.data ? toReview.length.toLocaleString() : "—"}</span>
          {drafts.data && allDrafts.length ? (
            <span className="tile-of">of {draftsCapped ? `the latest ${DRAFT_LIMIT}` : plural(allDrafts.length, "social draft")}</span>
          ) : null}
        </Tile>

        <Tile
          icon="send"
          tone="blue"
          label="Posts published"
          to="/outbox"
          hint={
            outbox.data ? (
              <>
                {posts.length ? `${livePosts.toLocaleString()} live · ${(posts.length - livePosts).toLocaleString()} simulated` : "Nothing published yet"}
                {outboxCapped ? ` · in the latest ${OUTBOX_LIMIT} outbox entries` : ""}
                {waitingPublish ? (
                  <>
                    {" "}
                    · <span className="text-warn">{waitingPublish} awaiting approval</span>
                  </>
                ) : null}
              </>
            ) : outbox.error ? (
              "Could not load the outbox"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{outbox.data ? posts.length.toLocaleString() : "—"}</span>
          {outbox.data && posts.length && livePosts === 0 ? <Pill value="mock" label="Simulated" /> : null}
        </Tile>

        <Tile
          icon="plug"
          tone={!postonomous ? "muted" : mode === "live" ? "green" : mode === "mock" ? "orange" : "muted"}
          label="Postonomous"
          to="/integrations"
          hint={
            !postonomous
              ? integrations.error
                ? "Could not load integrations"
                : integrations.data
                  ? "Not configured on this Command Center"
                  : "…"
              : mode === "live"
                ? "Approved posts go to your connected accounts"
                : mode === "mock"
                  ? "Approved posts are recorded in the Outbox, not posted"
                  : "Publishing is switched off"
          }
        >
          <span className={`tile-status ${mode === "live" ? "text-good" : mode === "mock" ? "so-text-orange" : "muted"}`}>
            {!postonomous ? (integrations.data ? "Not available" : "Checking…") : mode === "mock" ? "Mock mode" : mode === "live" ? "Live" : "Disabled"}
          </span>
          {postonomous ? (
            <span className="so-tile-line">
              {postonomous.status?.last_error_at &&
              (!postonomous.status.last_ok_at || new Date(postonomous.status.last_error_at) > new Date(postonomous.status.last_ok_at)) ? (
                <>
                  <span className="dot dot-bad" aria-hidden /> Last check failed <Time iso={postonomous.status.last_error_at} />
                </>
              ) : postonomous.status?.last_ok_at ? (
                <>
                  <span className="dot dot-good" aria-hidden /> Connected, checked <Time iso={postonomous.status.last_ok_at} />
                </>
              ) : (
                <>
                  <span className="dot dot-muted" aria-hidden /> No connection checks recorded
                </>
              )}
            </span>
          ) : null}
        </Tile>
      </div>

      <div className="so-top">
        <Card
          className="so-create"
          title={
            <>
              <Icon name="sparkles" size={18} className="card-icon so-card-icon" /> Create
            </>
          }
          actions={
            <Link className="small" to="/agents">
              All agents
            </Link>
          }
        >
          {!canOperate ? (
            <p className="so-note">
              <Icon name="lock" size={14} /> Your account is view-only. These are the actions an operator or admin can start here.
            </p>
          ) : null}
          {blocked ? (
            <p className="so-note so-note-warn">
              <Icon name="alert" size={14} /> {blocked}
            </p>
          ) : null}
          {caps.length ? (
            <ul className="so-actions">
              {caps.map((cap) => (
                <li key={cap.name}>
                  <ActionCard
                    cap={cap}
                    blocked={blocked}
                    onRun={canOperate && agent ? () => setRunning(cap.name === "publish_draft" ? socialPublish(cap) : cap) : undefined}
                  />
                  {cap.examples.length ? (
                    <div className="so-examples">
                      <span className="so-examples-label">
                        <Icon name="sparkles" size={12} /> Or ask the AI Supervisor
                      </span>
                      <ul>
                        {cap.examples.map((ex) => (
                          <li key={ex}>
                            <q>{ex}</q>
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : cap.name === "publish_draft" ? (
                    <p className="so-examples so-examples-plain">
                      Pick a draft you have marked reviewed. Each publish request waits on the Approvals page until a person approves it.
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <Empty>{agents.data ? "No social actions available." : "Loading…"}</Empty>
          )}

          <h3 className="so-flow-title">How a post goes out</h3>
          <ol className="so-flow">
            {stages.map((s) => {
              const inner = (
                <>
                  <span className="so-flow-icon" aria-hidden>
                    <Icon name={s.icon} size={15} />
                  </span>
                  <span className="so-flow-text">
                    <span className="so-flow-value tabular">{s.value === null ? "—" : s.value.toLocaleString()}</span>
                    <span className="so-flow-label">{s.label}</span>
                    <span className="so-flow-hint">{s.hint}</span>
                  </span>
                </>
              );
              return (
                <li key={s.key} className={`so-flow-step so-flow-${s.key}`}>
                  {s.to ? (
                    <Link className="so-flow-box" to={s.to}>
                      {inner}
                    </Link>
                  ) : (
                    <div className="so-flow-box">{inner}</div>
                  )}
                </li>
              );
            })}
          </ol>
        </Card>

        <Card
          className={`so-connection so-mode-${mode ?? "unknown"}`}
          title={
            <>
              <Icon name="plug" size={18} className="card-icon so-card-icon" /> Postonomous connection
            </>
          }
          actions={
            <Link className="small" to="/integrations">
              Integrations
            </Link>
          }
        >
          <ErrorNote error={integrations.error} />
          {postonomous ? (
            <>
              <div className="so-conn-head">
                <span className="so-conn-badge" aria-hidden>
                  <Icon name="megaphone" size={20} />
                </span>
                <div className="so-conn-title">
                  <strong>{postonomous.title}</strong>
                  <Pill value={postonomous.mode} label={`${modeLabel(postonomous.mode)} mode`} />
                </div>
              </div>
              {mode === "mock" ? (
                <div className="so-callout so-callout-mock">
                  <strong>Publishing is simulated in this build.</strong> Nothing is posted to any social network; approved posts are kept in the{" "}
                  <Link to="/outbox">Outbox</Link>.
                </div>
              ) : mode === "disabled" ? (
                <div className="so-callout so-callout-off">
                  <strong>Publishing is switched off.</strong> Drafts can still be written and reviewed, but approved posts cannot be published.
                </div>
              ) : (
                <div className="so-callout so-callout-live">
                  <strong>Live.</strong> Approved posts are published to the connected accounts through Postonomous.
                </div>
              )}
              {mode !== "mock" ? <p className="so-conn-desc">{postonomous.description}</p> : null}
              {mode !== "live" && postonomous.live_requirements ? (
                <div className="so-requirements">
                  <h3>To go live</h3>
                  <p>{postonomous.live_requirements}</p>
                </div>
              ) : null}
              <dl className="so-conn-kv">
                <dt>Last successful check</dt>
                <dd>{postonomous.status?.last_ok_at ? <Time iso={postonomous.status.last_ok_at} /> : "None recorded"}</dd>
                <dt>Last error</dt>
                <dd>
                  {postonomous.status?.last_error_at ? (
                    <>
                      <Time iso={postonomous.status.last_error_at} />
                      {postonomous.status.last_error ? <span className="so-conn-error">{postonomous.status.last_error}</span> : null}
                    </>
                  ) : (
                    "None recorded"
                  )}
                </dd>
              </dl>
            </>
          ) : integrations.data ? (
            <Empty>Postonomous is not configured on this Command Center.</Empty>
          ) : (
            <Empty>Loading…</Empty>
          )}
        </Card>
      </div>

      <div className="so-grid">
        <Card
          className="so-list-card"
          title={
            <>
              <Icon name="file" size={18} className="card-icon so-card-icon" /> Drafts
            </>
          }
          actions={
            <Link className="small" to="/drafts">
              All drafts
            </Link>
          }
        >
          <div className="tabs so-filter" role="group" aria-label="Show drafts">
            {(
              [
                ["all", "All", allDrafts.length],
                ["draft", "To review", toReview.length],
                ["approved", "Reviewed", reviewed.length],
              ] as [DraftFilter, string, number][]
            ).map(([key, label, n]) => (
              <button key={key} type="button" className={filter === key ? "active" : ""} aria-pressed={filter === key} onClick={() => setFilter(key)}>
                {label}
                {drafts.data ? <span className="so-filter-count tabular">{n}</span> : null}
              </button>
            ))}
          </div>
          <ErrorNote error={drafts.error} />
          {shownDrafts.length ? (
            <ul className="so-rows">
              {shownDrafts.map((a) => {
                const platforms = platformsOf(a);
                const writer = typeof a.data?.writer === "string" ? a.data.writer : "";
                const meta = [
                  writer === "template" ? "Template draft" : writer ? `Written by ${writer}` : "",
                  a.data?.published_mock ? "publish recorded (mock)" : "",
                ].filter(Boolean);
                const canPublish = canOperate && !!publishCap && !blocked && a.status === "approved";
                return (
                  <li key={a.id} className={canPublish ? "so-row-with-action" : undefined}>
                    <button type="button" className="so-row" onClick={() => setOpenDraft(a.id)}>
                      <span className="so-row-icon so-tone-purple" aria-hidden>
                        <Icon name="file" size={16} />
                      </span>
                      <span className="so-row-main">
                        <span className="so-row-title" title={a.title}>
                          {a.title}
                        </span>
                        <span className="so-row-sub">
                          {platforms.length ? (
                            platforms.map((p) => (
                              <span key={p} className="so-platform">
                                {platformLabel(p)}
                              </span>
                            ))
                          ) : (
                            <span>No platforms recorded</span>
                          )}
                        </span>
                        {meta.length ? <span className="so-row-meta">{meta.join(" · ")}</span> : null}
                      </span>
                      <span className="so-row-side">
                        <Pill value={a.status} label={a.status === "approved" ? "Reviewed" : undefined} />
                        <Time iso={a.created_at} />
                      </span>
                    </button>
                    {canPublish && publishCap ? (
                      <button
                        type="button"
                        className="btn small so-row-action"
                        onClick={() => setRunning(socialPublish(publishCap, a))}
                        aria-label={`Request publishing of ${a.title}`}
                      >
                        <Icon name="send" size={14} /> Publish…
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : drafts.data ? (
            <Empty>
              {filter === "draft"
                ? "Nothing waiting for review."
                : filter === "approved"
                  ? "No drafts marked reviewed yet. Open a draft to review it."
                  : `No social drafts yet.${canOperate ? " Use Draft social posts above to write the first ones." : ""}`}
            </Empty>
          ) : (
            <Empty>Loading…</Empty>
          )}
          <p className="so-foot">
            {(() => {
              const n = filter === "draft" ? toReview.length : filter === "approved" ? reviewed.length : allDrafts.length;
              return n > SHOWN ? `Newest ${SHOWN} of ${n.toLocaleString()}. ` : "";
            })()}
            Marking a draft reviewed never publishes it.
          </p>
        </Card>

        <Card
          className="so-list-card"
          title={
            <>
              <Icon name="send" size={18} className="card-icon so-card-icon" /> Published &amp; scheduled posts
            </>
          }
          actions={
            <Link className="small" to="/outbox">
              Outbox
            </Link>
          }
        >
          <ErrorNote error={outbox.error} />
          {posts.length ? (
            <ul className="so-rows">
              {posts.slice(0, SHOWN).map((e) => {
                const when = scheduledFor(e);
                return (
                  <li key={e.id}>
                    <button type="button" className="so-row" onClick={() => setOpenPost(e)}>
                      <span className="so-row-icon so-tone-cyan" aria-hidden>
                        <Icon name="send" size={16} />
                      </span>
                      <span className="so-row-main">
                        <span className="so-row-title">
                          {e.recipient ? platformLabel(e.recipient) : "No platform"}
                          {when ? <span className="so-row-tag">scheduled for {formatWhen(when)}</span> : null}
                        </span>
                        <span className="so-row-sub so-row-text" title={firstLine(e)}>
                          {firstLine(e) || "No text"}
                        </span>
                        <span className="so-row-meta">Approved by {e.approved_by || "—"}</span>
                      </span>
                      <span className="so-row-side">
                        <Pill value={e.mode} label={e.mode === "mock" ? mockLabel("postonomous") : "Live: posted"} />
                        <Time iso={e.created_at} />
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : outbox.data ? (
            <Empty>
              Nothing published yet. Posts appear here once a publish request is approved
              {mode === "mock" ? "; in this build they are recorded, not posted." : "."}
            </Empty>
          ) : (
            <Empty>Loading…</Empty>
          )}
          <p className="so-foot">
            {posts.length > SHOWN ? `Newest ${SHOWN} of ${posts.length.toLocaleString()}. ` : ""}
            Postonomous entries from the Outbox{outboxCapped ? ` (latest ${OUTBOX_LIMIT} entries)` : ""}.
          </p>
        </Card>
      </div>

      <Card
        title={
          <>
            <Icon name="history" size={18} className="card-icon so-card-icon" /> Recent activity
          </>
        }
        actions={
          <Link className="small" to="/tasks">
            All tasks
          </Link>
        }
      >
        <ErrorNote error={recent.error} />
        {socialTasks.length ? (
          <ul className="so-rows so-activity">
            {socialTasks.map((t) => (
              <li key={t.id}>
                <button type="button" className="so-row" onClick={() => setOpenTask(t.id)}>
                  <span className={`so-row-icon so-tone-${t.capability === "publish_draft" ? "cyan" : "purple"}`} aria-hidden>
                    <Icon name={CAP_INFO[t.capability]?.icon ?? "tasks"} size={16} />
                  </span>
                  <span className="so-row-main">
                    <span className="so-row-title">
                      {capLabel(t.capability)} <span className="so-row-tag">#{t.id}</span>
                    </span>
                    <span className={`so-row-sub so-row-text ${t.error && !t.summary ? "text-bad" : ""}`}>
                      {t.summary || t.error || (t.status === "awaiting_approval" ? "Waiting for approval." : t.status === "queued" ? "Queued." : `Task #${t.id}`)}
                    </span>
                  </span>
                  <span className="so-row-side">
                    <Pill value={t.status} />
                    <span>
                      {t.created_by ? <span className="so-row-by">{t.created_by} · </span> : null}
                      <Time iso={t.created_at} />
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        ) : recent.data ? (
          <Empty>No social drafting or publishing yet.</Empty>
        ) : (
          <Empty>Loading…</Empty>
        )}
        <p className="so-foot">
          Drafting and publishing tasks among the latest {RECENT} Social Media &amp; Marketing tasks. This agent's ad reports are on{" "}
          <Link to="/seo">SEO &amp; Marketing</Link>, marked <SampleDataPill /> until an ad account is connected.
        </p>
      </Card>

      {running && agent ? (
        <RunModal
          agent={agent}
          cap={running}
          onClose={() => {
            setRunning(null);
            refreshWork();
          }}
        />
      ) : null}
      {openTask ? <TaskDetail id={openTask} onClose={() => setOpenTask(null)} /> : null}
      {openDraft ? <ArtifactView id={openDraft} onClose={() => setOpenDraft(null)} onChanged={refreshWork} /> : null}
      {openPost ? (
        <Modal title={`Post #${openPost.id}: ${openPost.recipient ? platformLabel(openPost.recipient) : "no platform"}`} onClose={() => setOpenPost(null)} wide>
          <p>
            <Pill value={openPost.mode} label={openPost.mode === "mock" ? "Mock: not posted" : "Live"} /> <span className="muted">{openPost.result}</span>
          </p>
          <dl className="kv">
            <dt>Platform</dt>
            <dd>{openPost.recipient ? platformLabel(openPost.recipient) : "—"}</dd>
            <dt>Scheduled for</dt>
            <dd>{scheduledFor(openPost) ? formatWhen(scheduledFor(openPost)) : "Immediately"}</dd>
            <dt>Approved by</dt>
            <dd>{openPost.approved_by || "—"}</dd>
            <dt>Recorded</dt>
            <dd>
              <Time iso={openPost.created_at} />
            </dd>
            {openPost.task_id ? (
              <>
                <dt>Task</dt>
                <dd>
                  <Link to={`/tasks?id=${openPost.task_id}`}>#{openPost.task_id}</Link>
                </dd>
              </>
            ) : null}
          </dl>
          <h3>Post text</h3>
          {openPost.body ? <pre className="plain boxed so-post-body">{openPost.body}</pre> : <p className="muted">No text recorded.</p>}
        </Modal>
      ) : null}
    </div>
  );
}
