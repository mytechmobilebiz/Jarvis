"""Reference notes: after a task succeeds, the approved memory items that look relevant to it, by id, kind and title.

The worker (app/worker/runner.py) adds them to the task result as `data.memory_notes`, so an operator sees what the
business's memory already says about the same site or topic. They are local (no AI model, no network), they change
nothing the agent did, and they are never added for Credit Repair.

The topic words come only from what an operator typed or registered: the task's topic, title and category parameters,
the host of its URL, and the host of the site or server it names. Customer text (a support ticket or a lead) is never
read here, so the Email & Customer Communications agent's ticket and lead tasks get no notes. Only approved, general
items in the agent's own scope are listed (app/brain/policy.py); customer items never are.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import MemoryItem, Server, Site
from .policy import SENSITIVE_AGENT, agent_items
from .store import AgentMemory, search_terms, search_was_filtered

MAX_NOTES = 5
# Free-text parameters an operator types (never customer text): their topic words are searched.
TEXT_PARAMS = ("topic", "title", "category")
# Parameters holding a URL or a host name: the host is searched as one word, as PostgreSQL indexes it.
HOST_PARAMS = ("url", "host")
_IP_OR_HOST = re.compile(r"^[a-z0-9][a-z0-9.\-]{0,252}$")
# A domain written inside a topic ("acme.example.com redesign"): searched as a host, not as its separate labels.
_DOMAIN_IN_TEXT = re.compile(r"(?i)\b(?:https?://)?(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}\b")


def host_of(value: str) -> str:
    """'https://www.Example.com:8443/x' -> 'example.com' ('' when it is not a URL or a host name)."""
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlsplit(raw).hostname or "").lower()
    except ValueError:
        return ""
    host = host.removeprefix("www.")
    return host if "." in host and _IP_OR_HOST.match(host) else ""


async def topic_words(session: AsyncSession, params: dict[str, Any]) -> list[str]:
    """The words a task is about, from operator-supplied parameters only (see the module docstring)."""
    words: list[str] = []
    hosts: list[str] = []
    for key in TEXT_PARAMS:
        value = params.get(key)
        if isinstance(value, str):
            hosts += [host_of(m.group(0)) for m in _DOMAIN_IN_TEXT.finditer(value)]
            words += search_terms(_DOMAIN_IN_TEXT.sub(" ", value))
    for key in HOST_PARAMS:
        value = params.get(key)
        if isinstance(value, str):
            hosts.append(host_of(value))
    site_id = params.get("site_id")
    if isinstance(site_id, int) and not isinstance(site_id, bool):
        site = await session.get(Site, site_id)
        if site is not None:
            hosts.append(host_of(site.url))
    server_id = params.get("server_id")
    if isinstance(server_id, int) and not isinstance(server_id, bool):
        server = await session.get(Server, server_id)
        if server is not None:
            hosts.append(host_of(server.host))
    # Hosts first (they are the most specific). Indexed text keeps "www.example.com" and "example.com" as different
    # words: look for both.
    host_words = [w for host in filter(None, hosts) for w in (host, f"www.{host}")]
    seen: list[str] = []
    for w in host_words + words:
        if w.lower() not in {s.lower() for s in seen}:
            seen.append(w)
    return seen[:12]


async def reference_notes(memory: AgentMemory, params: dict[str, Any], *, limit: int = MAX_NOTES) -> list[dict[str, Any]]:
    """[{id, kind, title}] of approved, general items in the agent's scope matching the task's topic words, best first.

    Empty when the agent is Credit Repair, when the task has no topic words, or when nothing matches (no "newest"
    fallback here: a note must be about the task).
    """
    if memory.agent == SENSITIVE_AGENT:
        return []
    words = await topic_words(memory.session, params)
    if not words:
        return []
    query_text = " OR ".join(words)
    if not await search_was_filtered(memory.session, query_text):
        return []
    query = func.websearch_to_tsquery("english", query_text)
    stmt = (
        select(MemoryItem.id, MemoryItem.kind, MemoryItem.title)
        .where(and_(agent_items(memory.policy, general_only=True), MemoryItem.search.op("@@")(query)))
        .order_by(func.ts_rank_cd(MemoryItem.search, query).desc(), MemoryItem.id.desc())
        .limit(limit)
    )
    rows = (await memory.session.execute(stmt)).all()
    return [{"id": r.id, "kind": r.kind, "title": r.title} for r in rows]
