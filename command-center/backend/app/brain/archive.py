"""The derived fields of an archived JARVIS session, recomputed from the rows it still holds.

A session's counts, labels, sensitivity and summary are never edited by hand: the importer, a rollback and the
withhold-a-message route (app/api/brain.py) all change messages and then call recompute_session(), so the session
always describes exactly the rows that remain.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ArchiveMessage, ArchiveSession
from . import text as brain_text

SUMMARY_FIRST = 200
SUMMARY_LAST = 300


def _line(text: str, limit: int) -> str:
    """text.one_line(), never ending inside a redaction marker ("[REDACTED:credential_assi…" would read as a new
    secret-like word when the summary is checked or cleaned again)."""
    out = brain_text.one_line(text, limit)
    start = out.rfind("[REDACTED")
    if start >= 0 and "]" not in out[start:]:
        out = out[:start].rstrip() + "…"
    return out


async def recompute_session(session: AsyncSession, archive_session_id: int) -> ArchiveSession | None:
    """Recompute counts, labels, sensitivity, summary and the secrets flag of one archived session.

    - imported_messages / withheld_messages: rows kept with and without their text;
    - tool_calls: the tool calls of the rows that still have text;
    - labels: how many rows carry each label (matched by keywords);
    - sensitivity: the highest level of any row (restricted > customer > general), at least customer when the title
      itself held a masked contact;
    - summary: the first user message (one line, 200) and the last assistant message (one line, 300) of the rows
      that still have text; a restricted session has no summary and no title.
    The caller flushes and commits.
    """
    sess = await session.get(ArchiveSession, archive_session_id)
    if sess is None:
        return None
    rows = (await session.execute(
        select(ArchiveMessage.role, ArchiveMessage.content, ArchiveMessage.withheld, ArchiveMessage.sensitivity,
               ArchiveMessage.labels, ArchiveMessage.redactions,
               func.jsonb_array_length(ArchiveMessage.tool_calls).label("calls"))
        .where(ArchiveMessage.session_id == archive_session_id)
        .order_by(ArchiveMessage.position, ArchiveMessage.id))).all()
    kept = [r for r in rows if not r.withheld]
    labels: Counter[str] = Counter()
    secrets: Counter[str] = Counter()
    for r in kept:
        labels.update(r.labels or [])
    for r in rows:
        for k, v in (r.redactions or {}).items():
            if isinstance(v, int):
                secrets[k] += v
    flags: dict[str, Any] = dict(sess.flags or {})
    for k, v in (flags.get("title_redactions") or {}).items():
        if isinstance(v, int):
            secrets[k] += v
    levels = [r.sensitivity for r in rows] + (["customer"] if flags.get("title_masked") else [])
    sensitivity = brain_text.most_sensitive(*levels) if levels else "general"
    if secrets:
        flags["secrets_redacted"] = dict(sorted(secrets.items()))
    else:
        flags.pop("secrets_redacted", None)
    summary = ""
    if sensitivity != "restricted":
        first = next((r.content for r in kept if r.role == "user" and r.content.strip()), "")
        last = next((r.content for r in reversed(kept) if r.role == "assistant" and r.content.strip()), "")
        pieces = [_line(first, SUMMARY_FIRST) if first else "", _line(last, SUMMARY_LAST) if last else ""]
        summary = " → ".join(p for p in pieces if p)
    else:
        sess.title = ""
    sess.imported_messages = len(kept)
    sess.withheld_messages = len(rows) - len(kept)
    sess.tool_calls = sum(int(r.calls or 0) for r in kept)
    sess.labels = dict(sorted(labels.items()))
    sess.sensitivity = sensitivity
    sess.summary = summary
    sess.flags = flags
    sess.updated_at = datetime.now(UTC)
    await session.flush()
    return sess
