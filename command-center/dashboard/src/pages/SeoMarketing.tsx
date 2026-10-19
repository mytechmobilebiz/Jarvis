import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Agent, type Artifact, type Capability, type Integration, type Task } from "../api";
import { useAuth } from "../App";
import { Card, Empty, ErrorNote, Pill, RiskBadge, Tile, Time } from "../components";
import { usePoll } from "../hooks";
import { Icon, type IconName } from "../icons";
import { ArtifactView, RunModal, TaskDetail } from "../work";
import { isSample, SampleDataPill } from "./shared";
import "./SeoMarketing.css";

const SEO = "seo_content";
const SOCIAL = "social_marketing";
// The advertising half of the Social Media & Marketing agent; its social posting lives on the Social page.
const AD_CAPS = new Set(["ads_report", "change_budget"]);
const PAGE = 200;
const MAX_PAGES = 5;
const RECENT = 15;

type TaskPage = { tasks: Task[]; total: number };
type Scope = "all" | "content" | "ads";

const CAP_ICON: Record<string, IconName> = {
  draft_blog_post: "file",
  publish_draft: "send",
  audit_page: "search",
  keyword_brief: "book",
  ads_report: "trending",
  change_budget: "megaphone",
};

const KINDS: Record<string, { noun: string; icon: IconName }> = {
  seo_audit: { noun: "SEO audit", icon: "search" },
  keyword_brief: { noun: "keyword brief", icon: "book" },
  blog_draft: { noun: "blog draft", icon: "file" },
  ads_report: { noun: "ad report", icon: "trending" },
};

function titleCase(name: string): string {
  return name
    .split("_")
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

function capitalize(s: string): string {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

function kindInfo(kind: string): { noun: string; icon: IconName } {
  return KINDS[kind] ?? { noun: kind.replace(/_/g, " "), icon: "file" };
}

function plural(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}

/** Why an agent cannot run right now, or null when it can. */
function unavailable(agent: Agent | undefined): string | null {
  if (!agent) return null;
  if (!agent.entitled) return `${agent.title} is not included in this edition, so it cannot run.`;
  if (!agent.enabled) return `${agent.title} is switched off, so nothing it does will run.`;
  return null;
}

function modeLabel(mode: Integration["mode"]): string {
  return mode === "live" ? "Live" : mode === "mock" ? "Mock" : "Disabled";
}

/** Counts the SEO agent's publish_draft runs (done, and waiting for approval), paging through at most MAX_PAGES * PAGE tasks. */
async function publishCounts(): Promise<{ published: number; waiting: number; scanned: number; complete: boolean }> {
  let published = 0;
  let waiting = 0;
  let scanned = 0;
  let total = 0;
  for (let page = 0; page < MAX_PAGES; page++) {
    const r = await api.get<TaskPage>(`/api/tasks?agent=${SEO}&status=succeeded,awaiting_approval&limit=${PAGE}&offset=${page * PAGE}`);
    total = r.total;
    scanned += r.tasks.length;
    for (const t of r.tasks) {
      if (t.capability !== "publish_draft") continue;
      if (t.status === "succeeded") published += 1;
      else waiting += 1;
    }
    if (r.tasks.length < PAGE || scanned >= total) break;
  }
  return { published, waiting, scanned, complete: scanned >= total };
}

function ActionButton({ cap, blocked, onRun }: { cap: Capability; blocked: string | null; onRun?: () => void }) {
  const body = (
    <>
      <span className="seo-action-icon" aria-hidden>
        <Icon name={CAP_ICON[cap.name] ?? "sparkles"} size={18} />
      </span>
      <span className="seo-action-main">
        <span className="seo-action-name">{titleCase(cap.name)}</span>
        <span className="seo-action-desc">{cap.description}</span>
        <span className="seo-action-meta">
          <RiskBadge risk={cap.risk} />
          {cap.requires_approval ? (
            <span className="seo-approval">
              <Icon name="lock" size={12} /> Needs approval
            </span>
          ) : null}
        </span>
      </span>
      {onRun ? <Icon name="chevronRight" size={16} className="seo-action-go" /> : null}
    </>
  );
  if (!onRun) return <div className="seo-action seo-action-static">{body}</div>;
  return (
    <button type="button" className="seo-action" onClick={onRun} disabled={!!blocked} title={blocked ?? undefined}>
      {body}
    </button>
  );
}

export default function SeoMarketing() {
  const { user, canOperate } = useAuth();
  const agents = usePoll(() => api.get<{ agents: Agent[] }>("/api/agents"), 30_000);
  const integrations = usePoll(() => api.get<{ integrations: Integration[] }>("/api/integrations"), 120_000);
  // Artifact lists carry no content, so the newest 200 is cheap and also gives the review count.
  const artifacts = usePoll(() => api.get<{ artifacts: Artifact[] }>(`/api/artifacts?agent=${SEO}&limit=${PAGE}`), 15_000);
  const adReports = usePoll(() => api.get<{ artifacts: Artifact[] }>(`/api/artifacts?agent=${SOCIAL}&kind=ads_report&limit=20`), 30_000);
  const seoTasks = usePoll(() => api.get<TaskPage>(`/api/tasks?agent=${SEO}&limit=${RECENT}`), 10_000);
  const socialTasks = usePoll(() => api.get<TaskPage>(`/api/tasks?agent=${SOCIAL}&limit=30`), 15_000);
  const publishing = usePoll(publishCounts, 60_000);

  const [running, setRunning] = useState<{ agent: Agent; cap: Capability } | null>(null);
  const [openTask, setOpenTask] = useState<number | null>(null);
  const [openArtifact, setOpenArtifact] = useState<number | null>(null);
  const [scope, setScope] = useState<Scope>("all");

  const seo = agents.data?.agents.find((a) => a.name === SEO);
  const social = agents.data?.agents.find((a) => a.name === SOCIAL);
  const integration = (name: string) => integrations.data?.integrations.find((i) => i.name === name);
  const wordpress = integration("wordpress_publish");
  const ads = integration("ads");
  const ai = integration("anthropic");
  const seoBlocked = unavailable(seo);
  const adsBlocked = unavailable(social);

  const stats = seo?.stats_24h ?? {};
  const tasks24 = Object.values(stats).reduce((a, b) => a + b, 0);
  const succeeded24 = stats.succeeded ?? 0;
  const failed24 = stats.failed ?? 0;
  const waiting24 = stats.awaiting_approval ?? 0;
  const finished24 = succeeded24 + failed24;

  const seoArtifacts = artifacts.data?.artifacts ?? [];
  const drafts = seoArtifacts.filter((a) => a.status === "draft");
  const draftKinds = Object.entries(
    drafts.reduce<Record<string, number>>((acc, a) => ({ ...acc, [a.kind]: (acc[a.kind] ?? 0) + 1 }), {}),
  ).map(([kind, n]) => plural(n, kindInfo(kind).noun));
  const reviewedCount = seoArtifacts.filter((a) => a.status === "approved").length;
  const artifactsCapped = seoArtifacts.length >= PAGE;

  const shownArtifacts = useMemo(
    () => [...(artifacts.data?.artifacts.slice(0, 20) ?? []), ...(adReports.data?.artifacts ?? [])].sort((a, b) => b.id - a.id).slice(0, 20),
    [artifacts.data, adReports.data],
  );

  const recentTasks = useMemo(() => {
    const seoList = seoTasks.data?.tasks ?? [];
    const adList = (socialTasks.data?.tasks ?? []).filter((t) => AD_CAPS.has(t.capability));
    const merged = scope === "content" ? seoList : scope === "ads" ? adList : [...seoList, ...adList];
    return [...merged].sort((a, b) => b.id - a.id).slice(0, RECENT);
  }, [seoTasks.data, socialTasks.data, scope]);

  const idea = seo?.capabilities.flatMap((c) => c.examples).find(Boolean);

  const groups = [
    {
      key: "content",
      title: "Content & SEO",
      icon: "trending" as IconName,
      agent: seo,
      blocked: seoBlocked,
      caps: seo?.capabilities ?? [],
      integration: wordpress,
    },
    {
      key: "ads",
      title: "Advertising",
      icon: "megaphone" as IconName,
      agent: social,
      blocked: adsBlocked,
      caps: social?.capabilities.filter((c) => AD_CAPS.has(c.name)) ?? [],
      integration: ads,
    },
  ];

  function refreshWork() {
    void seoTasks.refresh();
    void socialTasks.refresh();
    void artifacts.refresh();
    void adReports.refresh();
    void publishing.refresh();
  }

  const agentState = !seo ? "muted" : !seo.entitled ? "bad" : seo.enabled ? "good" : "warn";

  return (
    <div className="page seo-page">
      <header className="page-head seo-head">
        <div>
          <div className="eyebrow">Growth</div>
          <h1>SEO &amp; Marketing</h1>
          <p className="muted">
            On-page SEO audits, keyword briefs and blog drafts from the SEO &amp; Content agent, plus ad performance reports and budget proposals.
            Everything starts as a draft for review; publishing and spend changes wait for approval.
          </p>
        </div>
        <div className="seo-head-side">
          <div className="row-actions">
            <Link className="btn" to="/drafts">
              <Icon name="file" size={16} /> Drafts &amp; reports
            </Link>
            {canOperate ? (
              <Link className="btn primary" to="/supervisor">
                <Icon name="sparkles" size={16} /> AI Supervisor
              </Link>
            ) : null}
          </div>
          {canOperate && idea ? (
            <p className="seo-idea">
              <Icon name="sparkles" size={13} /> Try asking the AI Supervisor: <q>{idea}</q>
            </p>
          ) : null}
        </div>
      </header>

      <ErrorNote error={agents.error} />
      {agents.data && !seo ? (
        <div className="alert alert-bad" role="alert">
          The SEO &amp; Content agent is not registered on this Command Center.
        </div>
      ) : null}
      {seoBlocked ? (
        <div className={`alert seo-alert ${seo?.entitled ? "seo-alert-warn" : "alert-bad"}`} role="status">
          <Icon name="alert" size={18} className="seo-alert-icon" />
          <div>
            <strong>{seo?.entitled ? "SEO & Content is switched off." : "SEO & Content is not in this edition."}</strong>{" "}
            {seo?.entitled
              ? "Audits, briefs, drafts and scheduled SEO work will not run until it is switched back on. History below stays visible."
              : "Its history stays visible, but it cannot run until your licence includes it."}{" "}
            {user.role === "admin" ? (
              <Link to="/agents">Open AI Agents</Link>
            ) : (
              <span>Ask an admin to change this on the AI Agents page.</span>
            )}
          </div>
        </div>
      ) : null}

      <div className="tiles cols-4">
        <Tile
          icon="trending"
          tone={agentState === "good" ? "green" : agentState}
          label="SEO & Content agent"
          to="/agents"
          hint={
            !seo ? (
              agents.error ? "Could not load agents" : "…"
            ) : seo.last_run_at ? (
              <>
                Last ran <Time iso={seo.last_run_at} />
              </>
            ) : (
              "Not run yet"
            )
          }
        >
          <span className={`tile-status ${agentState === "good" ? "text-good" : agentState === "bad" ? "text-bad" : agentState === "warn" ? "text-warn" : "muted"}`}>
            {!seo ? "Checking…" : !seo.entitled ? "Not in this edition" : seo.enabled ? "Active" : "Switched off"}
          </span>
          {ai ? (
            <span className="seo-tile-line">
              <span className={`dot dot-${ai.mode === "live" ? "good" : "muted"}`} aria-hidden />
              {ai.mode === "live" ? `AI writing: ${ai.model ?? "Claude"}` : "No AI model: drafts use templates"}
            </span>
          ) : null}
        </Tile>

        <Tile
          icon="tasks"
          tone="blue"
          label="SEO tasks, last 24 h"
          to="/tasks"
          hint={
            seo ? (
              <>
                {succeeded24} succeeded · <span className={failed24 ? "text-bad" : ""}>{failed24} failed</span>
                {waiting24 ? <> · <span className="text-warn">{waiting24} awaiting approval</span></> : null}
              </>
            ) : agents.error ? (
              "Could not load agents"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{seo ? tasks24.toLocaleString() : "—"}</span>
          {finished24 ? <span className="tile-of">{Math.round((succeeded24 / finished24) * 100)}% succeeded</span> : null}
          {finished24 ? (
            <div className="meter" role="meter" aria-label="Share of finished tasks that succeeded" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((succeeded24 / finished24) * 100)}>
              <span className={`meter-fill tone-${failed24 ? "warn" : "good"}`} style={{ width: `${Math.max(2, (succeeded24 / finished24) * 100)}%` }} />
            </div>
          ) : null}
        </Tile>

        <Tile
          icon="file"
          tone="purple"
          label="Drafts to review"
          to="/drafts"
          hint={
            artifacts.data
              ? drafts.length
                ? draftKinds.join(" · ")
                : `Nothing waiting · ${reviewedCount.toLocaleString()} marked reviewed`
              : artifacts.error
                ? "Could not load drafts"
                : "…"
          }
        >
          <span className="tile-value tabular">{artifacts.data ? drafts.length.toLocaleString() : "—"}</span>
          {artifacts.data ? (
            <span className="tile-of">
              of {artifactsCapped ? `the latest ${PAGE}` : `${seoArtifacts.length.toLocaleString()} total`}
            </span>
          ) : null}
        </Tile>

        <Tile
          icon="send"
          tone="orange"
          label={wordpress?.mode === "live" ? "WordPress publishes" : wordpress?.mode === "mock" ? "Publishes recorded (mock)" : "WordPress publishes"}
          to="/outbox"
          hint={
            publishing.data ? (
              <>
                {publishing.data.complete ? "All time" : `In the latest ${publishing.data.scanned.toLocaleString()} finished SEO tasks`}
                {publishing.data.waiting ? <> · <span className="text-warn">{publishing.data.waiting} awaiting approval</span></> : null}
                {wordpress?.mode === "mock" ? " · mock: recorded in the Outbox, nothing published" : null}
              </>
            ) : publishing.error ? (
              "Could not load tasks"
            ) : (
              "…"
            )
          }
        >
          <span className="tile-value tabular">{publishing.data ? publishing.data.published.toLocaleString() : "—"}</span>
          {wordpress ? <Pill value={wordpress.mode} label={`${modeLabel(wordpress.mode)} mode`} /> : null}
        </Tile>
      </div>

      <Card
        className="seo-actions-card"
        title={
          <>
            <Icon name="play" size={18} className="card-icon seo-card-icon" /> Quick actions
          </>
        }
        actions={
          <Link className="small" to="/agents">
            All agents
          </Link>
        }
      >
        {!canOperate ? (
          <p className="seo-note">
            <Icon name="lock" size={14} /> Your account is view-only. These are the actions an operator or admin can run here.
          </p>
        ) : null}
        <div className="seo-groups">
          {groups.map((g) => (
            <section key={g.key} className={`seo-group seo-group-${g.key}`} aria-labelledby={`seo-group-${g.key}`}>
              <div className="seo-group-head">
                <h3 id={`seo-group-${g.key}`}>
                  <Icon name={g.icon} size={16} /> {g.title}
                </h3>
                {g.integration ? <Pill value={g.integration.mode} label={`${g.integration.title.replace(/\s*\(.*\)$/, "")}: ${modeLabel(g.integration.mode)}`} /> : null}
              </div>
              {g.blocked ? (
                <p className="seo-note seo-note-warn">
                  <Icon name="alert" size={14} /> {g.blocked}
                </p>
              ) : null}
              {g.caps.length ? (
                <ul className="seo-action-list">
                  {g.caps.map((cap) => (
                    <li key={cap.name}>
                      <ActionButton
                        cap={cap}
                        blocked={g.blocked}
                        onRun={canOperate && g.agent ? () => setRunning({ agent: g.agent as Agent, cap }) : undefined}
                      />
                    </li>
                  ))}
                </ul>
              ) : (
                <Empty>{agents.data ? "No actions available." : "Loading…"}</Empty>
              )}
              {g.integration?.mode === "mock" || g.integration?.mode === "disabled" ? <p className="seo-foot">{g.integration.description}</p> : null}
            </section>
          ))}
        </div>
      </Card>

      <div className="seo-grid">
        <Card
          title={
            <>
              <Icon name="file" size={18} className="card-icon seo-card-icon" /> Drafts &amp; briefs
            </>
          }
          actions={
            <Link className="small" to="/drafts">
              All drafts
            </Link>
          }
        >
          <ErrorNote error={artifacts.error ?? adReports.error} />
          {shownArtifacts.length ? (
            <ul className="seo-rows">
              {shownArtifacts.map((a) => {
                const info = kindInfo(a.kind);
                const score = typeof a.data?.score === "number" ? a.data.score : null;
                const writer = typeof a.data?.writer === "string" ? a.data.writer : "";
                return (
                  <li key={a.id}>
                    <button type="button" className="seo-row" onClick={() => setOpenArtifact(a.id)}>
                      <span className={`seo-row-icon seo-kind-${a.agent === SOCIAL ? "ads" : "content"}`} aria-hidden>
                        <Icon name={info.icon} size={16} />
                      </span>
                      <span className="seo-row-main">
                        <span className="seo-row-title" title={a.title}>
                          {a.title}
                        </span>
                        <span className="seo-row-sub">
                          {capitalize(info.noun)}
                          {score !== null ? ` · score ${score}/100` : ""}
                          {writer === "template" ? " · template" : writer ? ` · by ${writer}` : ""}
                          {a.data?.published_mock ? " · publish recorded (mock), not published" : ""}
                        </span>
                      </span>
                      <span className="seo-row-side">
                        {isSample(a.data) ? <SampleDataPill /> : null}
                        <Pill value={a.status} label={a.status === "approved" ? "Reviewed" : undefined} />
                        <Time iso={a.created_at} />
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : artifacts.data ? (
            <Empty>No drafts yet.{canOperate ? " Use Draft Blog Post, Keyword Brief or Audit Page above to create one." : ""}</Empty>
          ) : (
            <Empty>Loading…</Empty>
          )}
          <p className="seo-foot">Latest 20 SEO &amp; Content drafts and ad reports. Marking a draft reviewed never publishes it.</p>
        </Card>

        <Card
          title={
            <>
              <Icon name="tasks" size={18} className="card-icon seo-card-icon" /> Recent SEO &amp; marketing tasks
            </>
          }
          actions={
            <Link className="small" to="/tasks">
              All tasks
            </Link>
          }
        >
          <div className="tabs seo-scope" role="group" aria-label="Show tasks for">
            {(
              [
                ["all", "All"],
                ["content", "Content & SEO"],
                ["ads", "Advertising"],
              ] as [Scope, string][]
            ).map(([key, label]) => (
              <button key={key} type="button" className={scope === key ? "active" : ""} aria-pressed={scope === key} onClick={() => setScope(key)}>
                {label}
              </button>
            ))}
          </div>
          <ErrorNote error={seoTasks.error ?? socialTasks.error} />
          {recentTasks.length ? (
            <ul className="seo-rows">
              {recentTasks.map((t) => {
                const isAd = t.agent === SOCIAL;
                return (
                  <li key={t.id}>
                    <button type="button" className="seo-row" onClick={() => setOpenTask(t.id)}>
                      <span className={`seo-row-icon seo-kind-${isAd ? "ads" : "content"}`} aria-hidden>
                        <Icon name={CAP_ICON[t.capability] ?? "tasks"} size={16} />
                      </span>
                      <span className="seo-row-main">
                        <span className="seo-row-title">
                          {titleCase(t.capability)} <span className="seo-row-tag">{isAd ? "Advertising" : "Content & SEO"}</span>
                        </span>
                        <span className={`seo-row-sub seo-clamp ${t.error && !t.summary ? "text-bad" : ""}`}>
                          {t.summary || t.error || (t.status === "awaiting_approval" ? "Waiting for approval." : `Task #${t.id}`)}
                        </span>
                      </span>
                      <span className="seo-row-side">
                        {isSample(t.result?.data) ? <SampleDataPill /> : null}
                        <Pill value={t.status} />
                        <Time iso={t.created_at} />
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : seoTasks.data && socialTasks.data ? (
            <Empty>
              {scope === "ads" ? "No ad reports or budget changes yet." : scope === "content" ? "No SEO & Content tasks yet." : "No SEO or advertising tasks yet."}
            </Empty>
          ) : (
            <Empty>Loading…</Empty>
          )}
          <p className="seo-foot">
            Latest {RECENT} SEO &amp; Content tasks, plus ad tasks among the latest 30 Social Media &amp; Marketing tasks.
          </p>
        </Card>
      </div>

      {running ? (
        <RunModal
          agent={running.agent}
          cap={running.cap}
          onClose={() => {
            setRunning(null);
            refreshWork();
          }}
        />
      ) : null}
      {openTask ? <TaskDetail id={openTask} onClose={() => setOpenTask(null)} /> : null}
      {openArtifact ? <ArtifactView id={openArtifact} onClose={() => setOpenArtifact(null)} onChanged={refreshWork} /> : null}
    </div>
  );
}
