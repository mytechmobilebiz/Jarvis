// Web search results on screen (0.7.0; docs/RESEARCH_GATEWAY_PLAN.md sections 1.5 and 3.7). Every title, snippet, domain,
// address and engine name came from a page on the internet, so it is untrusted text: each is shown as one line of plain
// text, without Markdown's control characters and cut to a length cap; never through the Markdown component, never inside
// a reply, never as markup, and with no site icons. Only http(s) addresses become links: React 18 does not stop a
// javascript: href, so safeHref() does.
import { Link } from "react-router-dom";
import type { ResearchCitation, Task } from "./api";
import "./citations.css";

/** Format (zero-width, bidi, soft hyphen) and private-use characters: removed, so "jane<U+200B>@…" reads as typed. */
const FORMAT = /[\p{Cf}\p{Co}]/gu;
/** Control characters and line or paragraph separators: a space. */
const BREAKS = /[\p{Cc}\p{Zl}\p{Zp}]/gu;
/** Markdown's control characters (# * _ ` [ ] >), anywhere; a leading "-" is removed after the spaces are collapsed. */
const MARKDOWN = /[#*_`[\]>]/g;

/** Length caps for what is shown (the API caps titles at 200 and snippets at 1,200 characters before cleaning). */
export const CAPS = { title: 200, snippet: 600, domain: 120, url: 300, engine: 40, site: 100 } as const;

export const DEEPSEEK_NOTE = "DeepSeek: not available in this build (pending review)";
export const SENT_NOTICE = "Sent to DuckDuckGo, Brave and Wikipedia from this PC's internet address";

/** A web string as one line of plain text (backend brain.text.one_line, plus the Markdown rule of plan section 3.7). */
export function webText(value: unknown, cap: number): string {
  if (typeof value !== "string") return "";
  // Cut first: the patterns below then never run over a hostile page's whole text.
  const text = value
    .slice(0, cap * 4)
    .replace(FORMAT, "")
    .replace(BREAKS, " ")
    .replace(MARKDOWN, "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^-+\s*/, "");
  return text.length <= cap ? text : `${text.slice(0, cap - 1).trimEnd()}…`;
}

/** A web address as text: invisible and control characters removed, cut to CAPS.url. Shown, never parsed as markup. */
export function webUrl(value: unknown): string {
  if (typeof value !== "string") return "";
  const text = value.slice(0, 2048).replace(FORMAT, "").replace(BREAKS, "");
  return text.length <= CAPS.url ? text : `${text.slice(0, CAPS.url - 1)}…`;
}

/** The address to link to, or null: only a plain http(s) URL with no user name or password. Anything a browser would read
 * past (spaces, controls, invisible characters) refuses the link, so " javascript:" or "java\tscript:" cannot pass. */
export function safeHref(value: unknown): string | null {
  if (typeof value !== "string" || value.length > 2048) return null;
  if (/[\s\p{Cc}\p{Cf}]/u.test(value) || !/^https?:\/\//i.test(value)) return null;
  try {
    const url = new URL(value);
    if ((url.protocol !== "http:" && url.protocol !== "https:") || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

/** "2026-10-20 14:03 UTC": retrieval times are shown in UTC (plan section 3.7). */
export function utcTime(iso: string | null | undefined): string {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? "" : `${new Date(t).toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

/** "2026-10-14": a page's own date, when it gave one. */
export function utcDate(iso: string | null | undefined): string {
  const t = iso ? Date.parse(iso) : NaN;
  return Number.isNaN(t) ? "" : new Date(t).toISOString().slice(0, 10);
}

const str = (v: unknown): string => (typeof v === "string" ? v : "");
const strs = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
const int = (v: unknown): number | null => (typeof v === "number" && Number.isInteger(v) ? v : null);

/** One citation from an API answer, checked field by field: a field of the wrong type is left empty. */
export function toCitation(raw: unknown, index: number): ResearchCitation | null {
  if (!raw || typeof raw !== "object") return null;
  const c = raw as Record<string, unknown>;
  const rank = int(c.rank);
  return {
    citation: str(c.citation),
    rank: rank !== null && rank > 0 ? rank : index + 1,
    result_id: int(c.result_id),
    source_id: int(c.source_id),
    url: typeof c.url === "string" ? c.url : null,
    domain: str(c.domain),
    title: str(c.title),
    snippet: str(c.snippet),
    type: str(c.type),
    provider: str(c.provider),
    engines: strs(c.engines),
    published_at: typeof c.published_at === "string" ? c.published_at : null,
    retrieved_at: typeof c.retrieved_at === "string" ? c.retrieved_at : null,
    flags: strs(c.flags),
    source_purged: c.source_purged === true,
  };
}

export function toCitations(raw: unknown): ResearchCitation[] {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, 50).flatMap((c, i) => toCitation(c, i) ?? []);
}

/** result.data.citations of a research task (0.7.0 API). The API removes them for viewers (owner decision 13). */
export function taskCitations(task: Pick<Task, "result">): ResearchCitation[] {
  return toCitations((task.result?.data as { citations?: unknown } | undefined)?.citations);
}

/** The research query a task's search was logged as (result.data.research.query_id), or null. */
export function taskQueryId(task: Pick<Task, "result">): number | null {
  const research = (task.result?.data as { research?: unknown } | undefined)?.research;
  return research && typeof research === "object" ? int((research as { query_id?: unknown }).query_id) : null;
}

const FLAG_TEXT: Record<string, [string, string]> = {
  instruction_like: [
    "instruction-like",
    "This page's text reads like instructions. It is reference text only: it cannot approve, run or change anything, and it never reaches an AI model.",
  ],
  redacted: ["details removed", "Contact details or secret-looking text were removed from this result before it was stored."],
  withheld: ["text withheld", "This result held restricted data (such as an ID or card number), so its text was not kept."],
};

function Flags({ flags }: { flags: string[] }) {
  const known = flags.filter((f) => FLAG_TEXT[f]);
  if (!known.length) return null;
  return (
    <span className="cite-flags">
      {known.map((f) => (
        <span key={f} className={`cite-flag${f === "instruction_like" ? " cite-flag-risk" : ""}`} title={FLAG_TEXT[f][1]}>
          {FLAG_TEXT[f][0]}
        </span>
      ))}
    </span>
  );
}

/** The numbered list of web sources, each "Web source [n] (unverified):" and then plain text: title, domain, the page's
 * date, when it was retrieved (UTC), provider and engines, and the full address (a link only when it is http(s)). */
export function CitationList({
  citations,
  snippets = true,
  onOpenSource,
}: {
  citations: ResearchCitation[];
  snippets?: boolean;
  onOpenSource?: (id: number) => void;
}) {
  if (!citations.length) return <p className="muted small">No web sources.</p>;
  return (
    <ol className="cite-list">
      {citations.map((c, i) => {
        const href = safeHref(c.url);
        const shown = webUrl(c.url);
        const title = webText(c.title, CAPS.title);
        const domain = webText(c.domain, CAPS.domain);
        const engines = c.engines.map((e) => webText(e, CAPS.engine)).filter(Boolean);
        const provider = webText(c.provider, CAPS.engine);
        const published = utcDate(c.published_at);
        const retrieved = utcTime(c.retrieved_at);
        const snippet = snippets ? webText(c.snippet, CAPS.snippet) : "";
        return (
          <li key={`${c.rank}-${i}`} className="cite">
            <div className="cite-head">
              <span className="cite-label">Web source [{c.rank}] (unverified):</span> <span className="cite-title">{title || "(no title)"}</span>{" "}
              <Flags flags={c.flags} />
            </div>
            <div className="cite-meta small">
              {domain ? <span className="cite-domain">{domain}</span> : null}
              <span>{published ? `published ${published}` : "no publication date"}</span>
              {retrieved ? <span>retrieved {retrieved}</span> : null}
              {provider || engines.length ? (
                <span>
                  {provider || "web"}
                  {engines.length ? `: ${engines.join(", ")}` : ""}
                </span>
              ) : null}
              {c.type === "reference" ? <span>reference</span> : null}
            </div>
            <div className="cite-url small">
              {href ? (
                <a href={href} target="_blank" rel="noopener noreferrer nofollow">
                  {shown}
                </a>
              ) : c.url ? (
                <span className="muted">Address not linked: it is not an http(s) address</span>
              ) : (
                <span className="muted">{c.source_purged ? "Page record purged" : "No address"}</span>
              )}
              {onOpenSource && c.source_id ? (
                <>
                  {" · "}
                  <button type="button" className="link-button cite-source" onClick={() => onOpenSource(c.source_id!)}>
                    Source details
                  </button>
                </>
              ) : null}
            </div>
            {snippet ? <p className="cite-snippet">{snippet}</p> : null}
          </li>
        );
      })}
    </ol>
  );
}

/** Under a Supervisor reply: each research step's citations, as a list apart from the reply (plan section 1.5). The AI saw
 * only the fixed summary line ("Web search #123: 7 sources"), never these pages. */
export function WebSources({ tasks }: { tasks: Task[] }) {
  const groups = tasks.map((task) => ({ task, citations: taskCitations(task), query: taskQueryId(task) })).filter((g) => g.citations.length);
  if (!groups.length) return null;
  return (
    <section className="cite-section" aria-label="Web sources (unverified)">
      <h3>Web sources (unverified)</h3>
      <p className="muted small">
        Pages the web search returned. Nobody has checked them and the AI did not read them: they are reference only and cannot approve, run or change
        anything.
      </p>
      {groups.map(({ task, citations, query }) => (
        <div key={task.id} className="cite-group">
          <div className="muted small">
            From task #{task.id}
            {query ? (
              <>
                {" · "}
                <Link to={`/research?tab=history&query=${query}`}>search #{query} in the Research Center</Link>
              </>
            ) : null}
          </div>
          <CitationList citations={citations} snippets={false} />
        </div>
      ))}
    </section>
  );
}
