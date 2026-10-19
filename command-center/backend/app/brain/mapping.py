"""What each JARVIS source record becomes (spec 2.3 to 2.5): pure decisions, no database and no writing.

- Profile and content files become memory items (or are excluded with a reason).
- memories/MEMORY.md and USER.md are split into entries, operations.md into role blocks.
- Scheduled jobs and the website watchdog become workflow drafts, which never run.
- URLs in knowledge/websites.md are compared with the registered sites.

Every text goes through text.clean() here before anything else looks at it. Titles come from the cleaned text, so a
title never holds what the body could not.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import JOBS, SKILLS, STAFF, STAFF_AGENT
from . import jarvis_kit as kit
from . import text as brain_text
from .source import JobSpec, SourceFile, parse_time, sha256_text

MAX_TITLE = 300
MAX_BODY = 200_000  # store.MAX_BODY: the database refuses longer bodies
MAX_SPEC = 20_000
LARGE_HTML = 200 * 1024
CUT_NOTE = brain_text.truncate("xx", 1)[0][1:]  # the note text.truncate() appends

JARVIS_BUSINESS = {"product", "targets", "org"}
ROLE_BUSINESS = {"faq", "tone", "refund-policy", "leads", "proposal-instructions"}
ROLE_INSTRUCTION = {"autopilot"}
INSTRUCTION_FILES = {"soul.md", "agents.md", ".hermes.md"}
REPORT_PREFIX = {"sarah": "communications", "scout": "seo_content", "ava": "social_marketing",
                 "bobby": "social_marketing", "tom": "supervisor", "jarvis": "supervisor"}
LANGUAGES = {".py": "python", ".ps1": "powershell", ".psm1": "powershell", ".sh": "shell", ".bash": "shell",
             ".zsh": "shell", ".command": "shell", ".bat": "batch", ".cmd": "batch", ".js": "javascript",
             ".mjs": "javascript", ".cjs": "javascript", ".ts": "typescript", ".rb": "ruby", ".php": "php"}
ROLE_BLOCK = re.compile(r"<!--\s*jarvis-setup:role:(?P<id>[A-Za-z0-9_\-]{1,40}):start\s*-->(?P<body>.*?)"
                        r"<!--\s*jarvis-setup:role:(?P=id):end\s*-->", re.S)
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")
DATE_IN_NAME = re.compile(r"(?<![0-9])(20[0-9]{2})-([01][0-9])-([0-3][0-9])(?![0-9])")
URL = re.compile(r"(?i)\bhttps?://[^\s<>()\[\]{}\"'`|]+")
ERROR_SENTENCE = re.compile(r"(?i)\b(?:error|errors|fail|fails|failed|failure|retry|retries|stop|stops|budget|"
                            r"blocker|blocked)\b")
CC_ERROR_HANDLING = ("one attempt; a failure is shown in Automation Logs, and the next scheduled run tries again")
DUPLICATE_RULE = ("The draft never runs. Converting it creates a disabled schedule. Switch the JARVIS job off before "
                  "enabling that schedule, so the work is not done twice.")
TIMEZONE_NOTE = "JARVIS ran cron in the PC's local time; Command Center schedules run in UTC"
WATCHDOG_KEY = "jarvis:watchdog:check_websites"
WATCHDOG_SCRIPT = "scripts/check_websites.py"


# --- Cleaning -------------------------------------------------------------------------------------------------


def clean_kind(rel: str) -> str:
    kind = kit.file_kind(rel)
    return "chat" if kind == "text" else kind


def clean(raw: str, kind: str = "chat") -> brain_text.Cleaned:
    """text.clean(), plus the conversation rules for configuration files: their own rules first (a value can run to
    the end of the line), then the in-sentence and long-token rules every other text gets."""
    if kind != "config":
        return brain_text.clean(raw, kind)
    first = brain_text.clean(raw, "config")
    if first.withheld:
        return first
    second = brain_text.clean(first.text, "chat")
    if second.withheld:
        return brain_text.Cleaned("", {**first.secret_types}, second.restricted, {}, first.think_removed)
    out = brain_text.Cleaned(second.text, dict(first.secret_types), [], dict(second.masked),
                             first.think_removed + second.think_removed)
    for k, v in second.secret_types.items():
        out.secret_types[k] = out.secret_types.get(k, 0) + v
    return out


def cut(text: str, limit: int) -> tuple[str, bool]:
    """text.truncate() whose result, note included, is never longer than limit, cut at a space when there is one
    near the limit (so a word, or a [REDACTED:...] marker, is never cut in half)."""
    if len(text) <= limit:
        return text, False
    room = max(0, limit - len(CUT_NOTE))
    head = text[:room]
    space = max(head.rfind(" "), head.rfind("\n"))
    if space > room - 400:
        head = head[:space]
    return head.rstrip() + CUT_NOTE, True


def title_from(cleaned_text: str, fallback: str) -> str:
    """The first heading or non-blank line of the cleaned text (after any front matter), else the fallback."""
    lines = cleaned_text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i, line in enumerate(lines[1:60], 1) if line.strip() == "---")
            lines = lines[end + 1:]
        except StopIteration:
            pass
    for line in lines[:60]:
        s = line.strip()
        if not s or s.startswith("<!--") or set(s) <= set("-=*_#|: "):
            continue
        s = s.lstrip("#").strip()
        if s:
            return brain_text.one_line(s, MAX_TITLE)
    return brain_text.one_line(fallback, MAX_TITLE) or "untitled"


def file_time(f: SourceFile, tz: ZoneInfo, from_name: bool = False) -> datetime:
    """The file's modification time, or (for reports and briefs) the date in its file name."""
    if from_name:
        m = DATE_IN_NAME.search(f.name)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=tz).astimezone(UTC)
            except ValueError:
                pass
    return datetime.fromtimestamp(f.mtime, UTC)


# --- Memory items from files ----------------------------------------------------------------------------------


@dataclass
class Part:
    """One source record of a file: the file itself, a memory entry, a role block or a URL."""

    key: str
    record_type: str  # file, memory_entry, role_block, url
    source_ref: str
    excluded: str = ""  # a reason when this record is excluded without creating anything
    raw: str = ""  # the raw text of this part (cleaned when the item is built)
    sha256: str = ""
    kind: str = ""
    agent: str = ""
    text_kind: str = "chat"
    flags: dict[str, Any] = field(default_factory=dict)
    customer: bool = False  # customer data by default (USER.md, Sarah's reports)
    fallback_title: str = ""
    title: str = ""  # an explicit title (skills, the proposals summary)
    source_at: datetime | None = None
    url: str = ""  # record_type url: the normalised URL
    duplicate_of_earlier: bool = False  # the same entry earlier in the same file


def _sha(data: str | bytes) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8", "replace")).hexdigest()


def _norm(text: str) -> str:
    return brain_text.SPACE.sub(" ", text).strip()


def split_entries(text: str) -> list[str]:
    """Hermes keeps one memory per entry, separated by a line holding only the section sign; a file without one is
    read per list item, and a file with neither is one entry."""
    if "§" in text:
        return [e for e in re.split(r"\n?§\n?", text) if e.strip()]
    entries: list[list[str]] = []
    preamble: list[str] = []
    for line in text.splitlines():
        if LIST_ITEM.match(line):
            entries.append([line])
        elif entries:
            entries[-1].append(line)
        else:
            preamble.append(line)
    out = ["\n".join(e) for e in entries]
    if not out:
        return [text] if text.strip() else []
    if any(line.strip() and not line.lstrip().startswith("#") for line in preamble):
        out.insert(0, "\n".join(preamble))
    return [e for e in out if e.strip()]


def _template_flags(f: SourceFile) -> tuple[str, dict[str, Any]]:
    """('', flags) for an importable file, or an exclusion reason for an unfilled template copy."""
    t = f.template.get("template")
    if t in ("identical", "same content as a template") and f.placeholders > 0:
        return "unfilled template", {}
    if t == "identical":
        return "", {"template": "shipped default"}
    if t == "same content as a template":
        return "", {"template": "copy of a shipped default"}
    if t == "modified":
        return "", {"template": "modified"}
    return "", {}


def _staff_of_template(f: SourceFile) -> str:
    source = f.template.get("template_source", "")
    m = re.match(r"^agents/([a-z]+)/", source)
    return m.group(1) if m and m.group(1) in STAFF else "jarvis"


def file_parts(f: SourceFile, tz: ZoneInfo) -> list[Part]:
    """The file's own record first, then its entries, role blocks or URLs."""
    base = Part(key=f.key, record_type="file", source_ref=f.rel if f.area == "profile" else "jarvis-content/" + f.rel)
    lower = f.rel.lower()
    parts = lower.split("/")
    ext = os.path.splitext(lower)[1]
    cat = f.category

    def excluded(reason: str) -> list[Part]:
        base.excluded = reason
        return [base]

    if cat in ("secret", "database", "cache", "program", "temporary", "log", "session_transcript", "demo",
               "template_demo", "setup_state"):
        return excluded("not imported (%s)" % cat)
    if f.unreadable:
        return excluded("not imported (unreadable)")
    if f.too_large:
        return excluded("not imported (larger than %d MB)" % (kit.TEXT_SCAN_LIMIT // 2 ** 20))
    if f.area == "content":
        if f.text is None:
            return excluded("binary")
        if f.template.get("template") != "not from a template":
            return excluded("template")
        return _item(base, f, tz, kind="knowledge", agent="", flags={"from_content_folder": True})
    if cat == "configuration":
        return excluded("setting names only")
    if cat == "schedule":
        if lower == "cron/jobs.json":
            return excluded("read as workflow drafts (one record per job)")
        if lower == "cron/jarvis-setup-registered.json":
            return excluded("read for job names only")
        return excluded("not imported (schedule)")
    if f.text is None:
        return excluded("binary")
    if not f.text.strip():
        return excluded("empty")
    if cat == "workspace":
        if len(parts) == 3 and parts[1] in ("reports", "briefs") and ext == ".md":
            prefix = parts[2].split("-", 1)[0]
            agent = REPORT_PREFIX.get(prefix, "supervisor") if "-" in parts[2] else "supervisor"
            return _item(base, f, tz, kind="project", agent=agent, customer=prefix == "sarah" and "-" in parts[2],
                         source_at=file_time(f, tz, from_name=True))
        if lower == "workspace/proposals/sent-log.jsonl":
            return _proposals_summary(base, f, tz)
        return excluded("workspace state")
    if cat == "memory":
        return _entries(base, f, tz, user=parts[-1] == "user.md")
    if cat == "instructions":
        if parts[-1] == "operations.md":
            return _operations(base, f, tz)
        return _item(base, f, tz, kind="instruction", agent="supervisor")
    if cat == "knowledge":
        if ext in (".html", ".htm") and f.size > LARGE_HTML:
            return excluded("large template file")
        stem = os.path.splitext(parts[-1])[0]
        role = parts[1] if len(parts) > 2 and parts[1] in STAFF else ""
        if lower == "knowledge/websites.md":
            items = _item(base, f, tz, kind="knowledge", agent="monitoring")
            return items + _urls(f)
        if role:
            kind = "business" if stem in ROLE_BUSINESS else "instruction" if stem in ROLE_INSTRUCTION else "knowledge"
            return _item(base, f, tz, kind=kind, agent=STAFF_AGENT[role])
        return _item(base, f, tz, kind="business" if stem in JARVIS_BUSINESS else "knowledge", agent="")
    if cat == "skill":
        if len(parts) == 3 and parts[2] == "skill.md":
            return _skill(base, f, tz)
        return excluded("not imported (skill)")
    if cat == "plugin_or_hook":
        return _item(base, f, tz, kind="skill", agent="supervisor",
                     flags={"plugin_or_hook": True, "executable": False})
    if cat == "script":
        language = LANGUAGES.get(ext, ext.lstrip(".") or "text")
        return _item(base, f, tz, kind="skill", agent=STAFF_AGENT[_staff_of_template(f)],
                     flags={"script": True, "executable": False, "language": language})
    return excluded("not imported (%s)" % cat)


def _item(base: Part, f: SourceFile, tz: ZoneInfo, *, kind: str, agent: str, flags: dict[str, Any] | None = None,
          customer: bool = False, source_at: datetime | None = None, raw: str | None = None) -> list[Part]:
    reason, template_flags = _template_flags(f)
    if reason:
        base.excluded = reason
        return [base]
    base.raw = f.text if raw is None else raw
    base.sha256 = f.sha256
    base.kind, base.agent = kind, agent
    base.text_kind = clean_kind(f.rel)
    base.flags = {**template_flags, **(flags or {})}
    base.customer = customer
    base.fallback_title = f.name
    base.source_at = source_at or file_time(f, tz)
    return [base]


def _entries(base: Part, f: SourceFile, tz: ZoneInfo, *, user: bool) -> list[Part]:
    base.excluded = "split into entries"
    out = [base]
    seen: dict[str, int] = {}
    for n, entry in enumerate(split_entries(f.text or ""), 1):
        digest = _sha(_norm(entry))[:16]
        seen[digest] = seen.get(digest, 0) + 1
        suffix = "" if seen[digest] == 1 else "-%d" % seen[digest]
        part = Part(key="%s#entry-%s%s" % (f.key, digest, suffix), record_type="memory_entry",
                    source_ref=base.source_ref + "#entry", raw=entry.strip("\n"), sha256=_sha(entry),
                    kind="business" if user else "knowledge", agent="", text_kind="chat",
                    flags={"owner_profile": True} if user else {}, customer=user,
                    fallback_title="%s entry %d" % (f.name, n), source_at=file_time(f, tz),
                    duplicate_of_earlier=seen[digest] > 1)
        out.append(part)
    return out


def _operations(base: Part, f: SourceFile, tz: ZoneInfo) -> list[Part]:
    text = f.text or ""
    blocks = []
    seen: dict[str, int] = {}
    for m in ROLE_BLOCK.finditer(text):
        role = m.group("id")
        seen[role] = seen.get(role, 0) + 1
        suffix = "" if seen[role] == 1 else "-%d" % seen[role]
        agent = STAFF_AGENT.get(role, "supervisor")
        blocks.append(Part(key="%s#role-%s%s" % (f.key, role, suffix), record_type="role_block",
                           source_ref="%s#role %s" % (base.source_ref, role), raw=m.group("body").strip("\n"),
                           sha256=_sha(m.group(0)), kind="instruction", agent=agent, text_kind="chat",
                           flags={} if role in STAFF else {"unknown_role": True},
                           fallback_title="%s role %s" % (f.name, role), source_at=file_time(f, tz),
                           duplicate_of_earlier=seen[role] > 1))
    rest = ROLE_BLOCK.sub("", text)
    if not rest.strip():
        base.excluded = "split into role blocks"
        return [base, *blocks]
    items = _item(base, f, tz, kind="instruction", agent="supervisor", raw=rest)
    if blocks and not items[0].excluded:
        items[0].sha256 = _sha(rest)
    return items + blocks


def _skill(base: Part, f: SourceFile, tz: ZoneInfo) -> list[Part]:
    name = f.rel.split("/")[1]
    front = kit.frontmatter(f.text or "")
    shown = {}
    for k in ("name", "description", "version"):
        if isinstance(front.get(k), str) and front[k]:
            c = brain_text.clean(front[k], "chat")
            if not c.withheld:
                shown[k] = brain_text.one_line(c.text, 300)
    if name in SKILLS:
        staff, agent, caps, compatibility, note = SKILLS[name]
        flags: dict[str, Any] = {"skill": name, "staff": staff, "capabilities": list(caps),
                                 "compatibility": compatibility, "note": note, "frontmatter": shown}
    else:
        agent = "supervisor"
        flags = {"skill": name, "staff": "jarvis", "capabilities": [], "compatibility": "reference_only",
                 "note": "No Command Center counterpart; kept as text.", "frontmatter": shown, "custom": True}
    items = _item(base, f, tz, kind="skill", agent=agent, flags=flags)
    if not items[0].excluded:
        items[0].title = shown.get("name") or name
    return items


def _proposals_summary(base: Part, f: SourceFile, tz: ZoneInfo) -> list[Part]:
    """JARVIS's log of sent proposals, as counts per month and status: no names, addresses or prices."""
    counts: dict[tuple[str, str], int] = {}
    unreadable = 0
    for line in (f.text or "").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if not isinstance(row, dict):
            unreadable += 1
            continue
        when = None
        for k in ("sent", "sent_at", "date", "at", "timestamp", "time"):
            if k in row:
                when, _ = parse_time(row[k], tz)
                if when:
                    break
        month = when.astimezone(tz).strftime("%Y-%m") if when else "(no date)"
        status = kit.label(row.get("status"), "(other)") if row.get("status") else "sent"
        counts[(month, status)] = counts.get((month, status), 0) + 1
    lines = ["Proposals JARVIS sent, counted per month and status. Names, addresses and prices are not kept.", "",
             "| Month | Status | Proposals |", "|---|---|---|"]
    lines += ["| %s | %s | %d |" % (m, s, n) for (m, s), n in sorted(counts.items())]
    if unreadable:
        lines += ["", "%d line(s) of the log could not be read." % unreadable]
    items = _item(base, f, tz, kind="project", agent="communications", raw="\n".join(lines) + "\n")
    if not items[0].excluded:
        items[0].title = "Proposals JARVIS sent"
        items[0].text_kind = "chat"
    return items


# --- URLs (knowledge/websites.md) ------------------------------------------------------------------------------


def normalize_url(url: str) -> str | None:
    """https://host/path: no user, password, port, query or fragment; lower-case host; no trailing slash."""
    try:
        p = urllib.parse.urlsplit(url.strip().rstrip(".,;:!?)]}>'\""))
        host = (p.hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not host or not re.fullmatch(r"[a-z0-9.\-]{1,253}", host):
        return None
    return "https://%s%s" % (host, p.path.rstrip("/"))


def _urls(f: SourceFile) -> list[Part]:
    out, seen = [], set()
    for m in URL.finditer(f.text or ""):
        url = normalize_url(m.group(0))
        if not url or url in seen:
            continue
        seen.add(url)
        part = Part(key="jarvis:url:%s" % url, record_type="url", source_ref=f.rel, url=url, sha256=_sha(url))
        cleaned = brain_text.clean(url, "chat")
        if cleaned.withheld or cleaned.secret_types or cleaned.text != url:
            part.excluded = "held a secret or personal data"
        out.append(part)
    return out


# --- Workflow drafts (spec 2.4) ----------------------------------------------------------------------------------


def _cron_ok(expr: str) -> bool:
    if not expr or not kit.CRON_EXPR.match(expr):
        return False
    from croniter import croniter

    try:
        return bool(croniter.is_valid(expr))
    except Exception:  # croniter raises several types for odd input
        return False


def describe_cron(expr: str) -> str:
    m = re.fullmatch(r"\*/([0-9]{1,2}) \* \* \* \*", expr or "")
    if m:
        return "every %s minutes" % m.group(1)
    return "on the schedule %s" % expr if expr else "on its schedule"


def _risk_text(agent: str, capability: str) -> str:
    if not capability:
        return "no matching Command Center capability (reference only)"
    from ..agents import registry

    a = registry.get_agent(agent)
    cap = a.capabilities().get(capability) if a else None
    if cap is None:
        return "no matching Command Center capability (reference only)"
    if cap.risk.requires_approval:
        return "needs approval (%s)" % cap.risk.value
    return "runs without approval (read or draft only)"


def _error_lines(text: str) -> list[str]:
    out = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = brain_text.one_line(sentence.strip(" -*#>"), 300)
        if s and ERROR_SENTENCE.search(s) and s not in out:
            out.append(s)
        if len(out) == 5:
            break
    return out


@dataclass
class DraftPlan:
    key: str
    record_type: str  # job or watchdog
    fields: dict[str, Any]
    report_name: str
    secret_types: dict[str, int]
    restricted: list[str]
    overlaps: str


def draft_for_job(job: JobSpec, *, skill_texts: dict[str, str], cron_mode: str, tz_name: str,
                  history: dict[str, Any], schedules: list[dict[str, Any]]) -> DraftPlan:
    mapped = JOBS.get(job.name)
    staff = str(mapped["staff"]) if mapped else "jarvis"
    agent = str(mapped["agent"]) if mapped else "supervisor"
    capability = str(mapped["capability"]) if mapped else ""
    params = dict(mapped["params"]) if mapped else {}  # type: ignore[call-overload]
    compatibility = str(mapped["compatibility"]) if mapped else "reference_only"
    skills = job.skills or ([job.name] if job.name in skill_texts else [])
    raw_spec = "\n\n".join([job.prompt] + [skill_texts[s] for s in skills if s in skill_texts])
    cleaned = clean(raw_spec[:200_000], "chat")
    spec, _ = cut(cleaned.text, MAX_SPEC)
    overlaps = str(mapped.get("overlaps", "")) if mapped else ""
    for s in schedules:
        if capability and s["agent"] == agent and s["capability"] == capability:
            overlaps = (overlaps + " " if overlaps else "") + "The Command Center schedule %s already runs %s %s." % (
                s["name"], capability, describe_cron(s["cron"]))
    notes = [n for n in (str(mapped.get("note", "")) if mapped else
                         "A job JARVIS's templates do not have: kept as a reference only.", overlaps,
                         "%s (the customer time zone is %s)." % (TIMEZONE_NOTE, tz_name)) if n]
    if cleaned.withheld:
        notes.append("The job's text held restricted data and was left out.")
    if cleaned.secret_types:
        notes.append("Secrets were removed from the job's text (kinds: %s); rotate them."
                     % ", ".join(sorted(cleaned.secret_types)))
    fields = {
        "name": brain_text.one_line(clean(job.name or "", "chat").text, 200) or job.report_name,
        "staff": staff, "agent": agent, "capability": capability, "params": params, "compatibility": compatibility,
        "trigger": "schedule", "cron": job.expr if _cron_ok(job.expr) else "",
        "schedule_display": brain_text.one_line(clean(job.display or job.expr, "chat").text, 200),
        "timezone": tz_name, "enabled_in_source": bool(job.enabled), "deliver": job.deliver,
        "approval": {"jarvis": cron_mode or "unknown", "jarvis_rules": brain_text.instruction_flags(spec),
                     "command_center": _risk_text(agent, capability)},
        "error_handling": {"jarvis": _error_lines(spec), "command_center": CC_ERROR_HANDLING},
        "duplicate_guard": {"jarvis_job_enabled": bool(job.enabled), "overlaps": overlaps, "rule": DUPLICATE_RULE},
        "spec": spec, "history": history, "notes": notes,
        "source_fp": sha256_text(job.name, job.expr, job.enabled, job.deliver, job.prompt),
    }
    return DraftPlan(job.key, "job", fields, job.report_name, dict(cleaned.secret_types), list(cleaned.restricted),
                     overlaps)


def draft_for_watchdog(script: SourceFile, *, cron_mode: str, tz_name: str,
                       schedules: list[dict[str, Any]]) -> DraftPlan:
    sweep = next((s for s in schedules if s["name"] == "wordpress-uptime-sweep"), None)
    if sweep:
        overlaps = "the wordpress-uptime-sweep schedule already checks every registered site %s" % describe_cron(
            sweep["cron"])
    else:
        overlaps = "the monitoring agent's check_all_sites capability checks every registered site"
    cleaned = clean((script.text or "")[:200_000], "code")
    body, _ = cut(cleaned.text, MAX_SPEC - 400)
    spec = ("JARVIS's website watchdog script (scripts/check_websites.py). The script is never run: the Command "
            "Center's own check does the same job.\n\n" + body)
    notes = ["The script itself is kept as a skill reference; it is never run.", overlaps,
             "%s (the customer time zone is %s)." % (TIMEZONE_NOTE, tz_name)]
    if cleaned.secret_types:
        notes.append("Secrets were removed from the script's text (kinds: %s); rotate them."
                     % ", ".join(sorted(cleaned.secret_types)))
    fields = {
        "name": "website-watchdog", "staff": "jarvis", "agent": "monitoring", "capability": "check_all_sites",
        "params": {"deep": False}, "compatibility": "mapped", "trigger": "schedule", "cron": "",
        "schedule_display": "", "timezone": tz_name, "enabled_in_source": False, "deliver": "",
        "approval": {"jarvis": cron_mode or "unknown", "jarvis_rules": brain_text.instruction_flags(spec),
                     "command_center": _risk_text("monitoring", "check_all_sites")},
        "error_handling": {"jarvis": _error_lines(body), "command_center": CC_ERROR_HANDLING},
        "duplicate_guard": {"jarvis_job_enabled": False, "overlaps": overlaps, "rule": DUPLICATE_RULE},
        "spec": spec, "history": {"runs": 0, "from": "the JARVIS backup does not record the script's runs"},
        "notes": notes, "source_fp": sha256_text("check_websites", script.sha256),
    }
    return DraftPlan(WATCHDOG_KEY, "watchdog", fields, "website watchdog", dict(cleaned.secret_types),
                     list(cleaned.restricted), overlaps)
