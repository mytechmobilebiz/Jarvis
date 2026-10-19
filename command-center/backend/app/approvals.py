"""Facts that approvals of outward-facing actions share: exactly which draft goes out, and whether delivery is a mock.

An approval to send or publish a draft names the draft (id, title, an excerpt)
and binds its content hash, so a draft that is swapped or changed after the
request no longer matches and the action is refused (see app/tasks.py).
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any

from .models import Artifact

EXCERPT_CHARS = 300
_TAG = re.compile(r"<(script|style)\b.*?</\1>|<[^>]+>", re.IGNORECASE | re.DOTALL)
NOT_SENT = {"email": "recorded in the Outbox, not sent", "postonomous": "recorded in the Outbox, not published",
            "wordpress_publish": "recorded in the Outbox, not published"}


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt(artifact: Artifact, limit: int = EXCERPT_CHARS) -> str:
    """The draft's opening text, as plain text (HTML drafts such as proposals without their markup)."""
    text = artifact.content or ""
    if "html" in (artifact.content_type or ""):
        text = html.unescape(_TAG.sub(" ", text))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def draft_facts(artifact: Artifact) -> dict[str, Any]:
    """What the approval card shows about the draft that would go out."""
    return {"artifact_id": artifact.id, "kind": artifact.kind, "title": artifact.title, "status": artifact.status,
            "sha256": content_sha256(artifact.content or ""), "excerpt": excerpt(artifact)}


def delivery(integration: str) -> str:
    """"mock" or "live": how the integration that would carry the action is set up in this build."""
    from .config import get_settings
    from .integrations import build_hub

    try:
        return build_hub(get_settings()).get(integration).mode
    except KeyError:
        return "unknown"


def delivery_note(integration: str) -> str:
    """For the approval's "environment": what really happens when it is approved."""
    mode = delivery(integration)
    return f"mock: {NOT_SENT.get(integration, 'recorded, not carried out')}" if mode == "mock" else mode


async def load_draft(session: Any, artifact_id: int | None, *, kinds: tuple[str, ...], what: str, lock: bool = False) -> Artifact:
    """The draft an action would send or publish; ValueError if there is none (an approval must not name a draft that does not exist)."""
    artifact = await session.get(Artifact, artifact_id, with_for_update=lock) if artifact_id else None
    if artifact is None or artifact.kind not in kinds:
        raise ValueError(f"there is no {what} #{artifact_id}" if artifact_id else f"there is no {what} yet: draft one first")
    if artifact.status in ("rejected", "archived"):
        raise ValueError(f"{what} #{artifact.id} was {artifact.status} in Drafts & reports; draft a new one")
    return artifact
