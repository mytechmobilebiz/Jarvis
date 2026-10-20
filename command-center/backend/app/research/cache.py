"""The shared research cache and the merging of identical searches (plan section 3.9).

The key is the SHA-256 of the kind, the canonical query (lower case, spaces collapsed), language, time range, page
and count. It never includes who asked, and not the provider either: a cached answer serves the same question
whichever provider answered it (the mock's answers are kept apart: the gateway gives them the kind "search:mock").
Entries are the cleaned, normalized results, never raw provider text.

Lifetimes: a search 6 hours, with time_range=day 1 hour, "no results" 1 hour; failures are never cached. A hit is
free (no budget slot, no spacing) and is recorded as cached; the gateway applies the asking agent's blocked domains
to it.

Identical searches running at the same time are merged: the second waits on a PostgreSQL advisory lock on the key
(held in a transaction of its own, so it is released whatever happens) and then finds the first one's answer here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db import get_engine, session_scope
from ..models import ResearchCache

SEARCH_TTL = timedelta(hours=6)
DAY_TTL = timedelta(hours=1)
EMPTY_TTL = timedelta(hours=1)
LOCK_NAMESPACE = 7_315  # pg_advisory_xact_lock(namespace, key): research cache keys
MERGE_WAIT_SECONDS = 30  # longer than a search may take (10 s); a longer wait is an error and refuses


def cache_key(kind: str, query: str, *, language: str, time_range: str | None, page: int, count: int) -> str:
    payload = {"kind": kind, "q": " ".join(query.lower().split()), "language": language,
               "time_range": time_range or "", "page": page, "count": count}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def lifetime(status: str, time_range: str | None) -> timedelta | None:
    """How long an answer with this status stays cached; None: not cached."""
    if status == "empty":
        return EMPTY_TTL
    if status not in ("ok", "partial"):
        return None
    return DAY_TTL if time_range == "day" else SEARCH_TTL


def _lock_key(key: str) -> int:
    """A signed 32-bit number for the key (two keys sharing one only wait for each other)."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:4], "big", signed=True)


async def lookup(key: str, at: datetime | None = None) -> dict[str, Any] | None:
    """The cached answer, counting the hit; None when there is none or it expired."""
    at = at or datetime.now(UTC)
    async with session_scope() as session:
        return await session.scalar(update(ResearchCache)
                                    .where(ResearchCache.cache_key == key, ResearchCache.expires_at > at)
                                    .values(hits=ResearchCache.hits + 1)
                                    .returning(ResearchCache.response))


async def put(key: str, *, kind: str, provider: str, response: dict[str, Any], ttl: timedelta,
              at: datetime | None = None) -> None:
    at = at or datetime.now(UTC)
    stmt = pg_insert(ResearchCache).values(cache_key=key, kind=kind, provider=provider, response=response,
                                           created_at=at, expires_at=at + ttl, hits=0)
    stmt = stmt.on_conflict_do_update(index_elements=[ResearchCache.cache_key],
                                      set_={"provider": provider, "response": response, "created_at": at,
                                            "expires_at": at + ttl, "hits": 0})
    async with session_scope() as session:
        await session.execute(stmt)


@asynccontextmanager
async def merged(key: str, wait_seconds: int = MERGE_WAIT_SECONDS) -> AsyncIterator[None]:
    """Holds the key's lock for the block: an identical search waits for it, then reads the cache."""
    async with get_engine().connect() as conn:
        async with conn.begin():
            await conn.execute(text(f"SET LOCAL lock_timeout = '{int(wait_seconds * 1000)}ms'"))
            await conn.execute(text("SELECT pg_advisory_xact_lock(:ns, :key)"),
                               {"ns": LOCK_NAMESPACE, "key": _lock_key(key)})
            yield
