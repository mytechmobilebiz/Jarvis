"""The agent framework: risk levels, capabilities, results and the agent base class.

An agent is a Python class with one or more *capabilities*. Each capability
declares a risk level, and the task layer (app/tasks.py) — not the agent and
not the AI planner — decides from that level whether a human must approve the
task before it runs. Agents therefore cannot talk their way past an approval.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from ..brain.store import AgentMemory
    from ..config import Settings
    from ..integrations import IntegrationHub
    from ..llm.base import LLMProvider
    from .http import ReadOnlyHttpClient

log = logging.getLogger(__name__)

Severity = Literal["info", "warning", "critical"]
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class Risk(str, Enum):
    READ = "read"  # observes only
    DRAFT = "draft"  # writes drafts inside the Command Center only
    EXTERNAL = "external"  # contacts or publishes to the outside world
    CHANGE = "change"  # changes a website or server we manage (repairs, restarts)
    FINANCIAL = "financial"  # moves or commits money (ad spend, refunds)
    DESTRUCTIVE = "destructive"  # irreversible change or deletion

    @property
    def requires_approval(self) -> bool:
        return self not in (Risk.READ, Risk.DRAFT)


class Finding(BaseModel):
    code: str
    severity: Severity
    title: str
    detail: str = ""


class EventSpec(BaseModel):
    kind: str
    severity: Severity = "info"
    title: str
    detail: dict[str, Any] = Field(default_factory=dict)


class ArtifactSpec(BaseModel):
    kind: str
    title: str
    content: str = ""
    content_type: str = "text/markdown"
    data: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    findings: list[Finding] = Field(default_factory=list)
    artifacts: list[ArtifactSpec] = Field(default_factory=list)
    events: list[EventSpec] = Field(default_factory=list)


class Params(BaseModel):
    """Base for capability parameters: unknown keys are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


@dataclass
class AgentContext:
    """Everything a capability handler may use. Handlers get no other way out."""

    session: AsyncSession
    settings: Settings
    http: ReadOnlyHttpClient
    llm: LLMProvider
    integrations: IntegrationHub
    task_id: int | None = None
    request_id: int | None = None
    approved_by: str = ""  # set only when a human approved this task
    approved_target: dict[str, Any] | None = None  # what was approved (the approval's details: target, binding, production)
    logs: list[tuple[str, str]] = field(default_factory=list)
    ssh: Any = None  # app.ssh.gateway.SSHGateway: servers, only through cc-gate
    requested_by: str = ""
    agent_name: str = ""
    deadline: float | None = None  # loop.time() when the task's time runs out
    # This agent's read-only view of the JARVIS Brain (app/brain/store.py), through its access policy; None for agents
    # without a policy (test stand-ins). Memory is reference data: it never decides what a capability may do.
    memory: AgentMemory | None = None

    def time_left(self) -> float | None:
        if self.deadline is None:
            return None
        return self.deadline - asyncio.get_running_loop().time()

    def log(self, message: str, level: str = "info") -> None:
        self.logs.append((level, message))
        log.log(_LEVELS.get(level, logging.INFO), message)


Handler = Callable[[Any, AgentContext, Any], Awaitable[AgentResult]]


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    risk: Risk
    params_model: type[Params]
    method_name: str
    examples: tuple[str, ...] = ()
    timeout_seconds: int | None = None  # overrides TASK_TIMEOUT_SECONDS for slow work

    def params_schema(self) -> dict[str, Any]:
        return self.params_model.model_json_schema()


def capability(
    name: str | None = None,
    *,
    risk: Risk,
    description: str,
    params: type[Params] = NoParams,
    examples: tuple[str, ...] = (),
    timeout_seconds: int | None = None,
) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        fn.__capability__ = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "description": description,
            "risk": risk,
            "params_model": params,
            "examples": examples,
            "timeout_seconds": timeout_seconds,
        }
        return fn

    return decorate


@dataclass
class Intent:
    """A rule the deterministic planner uses to route a request to a capability.

    `build` receives the request text and the URLs found in it, and returns the
    capability's parameters, or None when the rule does not apply after all.
    """

    capability: str
    patterns: tuple[str, ...]
    build: Callable[[str, list[str]], dict[str, Any] | None] = lambda text, urls: {}
    weight: int = 1

    def matches(self, text: str) -> int:
        return sum(self.weight for p in self.patterns if re.search(p, text, re.IGNORECASE))


class BaseAgent:
    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    # External services this agent would use; shown on the Integrations page.
    integrations: ClassVar[tuple[str, ...]] = ()
    # Sensitive agents handle protected personal data: the supervisor never
    # routes to them, and only users with the sensitive-data permission may
    # run or approve their tasks.
    sensitive: ClassVar[bool] = False
    _capabilities: ClassVar[dict[str, Capability]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        caps: dict[str, Capability] = {}
        for klass in reversed(cls.__mro__):
            for attr, value in vars(klass).items():
                meta = getattr(value, "__capability__", None)
                if meta:
                    caps[meta["name"]] = Capability(method_name=attr, **meta)
        cls._capabilities = caps

    @classmethod
    def capabilities(cls) -> dict[str, Capability]:
        return dict(cls._capabilities)

    def intents(self) -> list[Intent]:
        return []

    async def approval_context(self, session: AsyncSession, capability: str, params: dict[str, Any], *,
                               lock: bool = False) -> dict[str, Any]:
        """Facts shown with an approval request: {"target": ..., "environment": ...}.

        "production": True makes the approval need an admin who types the
        "confirm" text (the target's name) before it is accepted. "binding"
        identifies the exact target; it is computed again when the approval is
        decided and before the task runs, and a difference refuses the change.
        Raises ValueError when the target does not exist; lock=True locks its rows.
        """
        return {}

    async def run(self, ctx: AgentContext, capability_name: str, raw_params: dict[str, Any]) -> AgentResult:
        cap = self._capabilities[capability_name]
        params = cap.params_model.model_validate(raw_params or {})
        handler = getattr(self, cap.method_name)
        return await handler(ctx, params)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": "supervisor" if self.name == "supervisor" else "agent",
            "title": self.title,
            "description": self.description,
            "integrations": list(self.integrations),
            "sensitive": self.sensitive,
            "capabilities": [
                {
                    "name": c.name,
                    "description": c.description,
                    "risk": c.risk.value,
                    "requires_approval": c.risk.requires_approval,
                    "params_schema": c.params_schema(),
                    "examples": list(c.examples),
                }
                for c in self._capabilities.values()
            ],
        }


URL_RE = re.compile(r"\bhttps?://[^\s<>\"')]+", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"(?<![@\w.-])((?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24})(?::\d{2,5})?(/[^\s<>\"')]*)?",
    re.IGNORECASE,
)
_NOT_DOMAINS = {"e.g", "i.e"}


def extract_urls(text: str) -> list[str]:
    """URLs and bare domains in a request, normalised to https:// URLs, in order."""
    found: list[str] = []
    for m in URL_RE.finditer(text):
        found.append(m.group(0).rstrip(".,;:!?"))
    stripped = URL_RE.sub(" ", text)
    for m in DOMAIN_RE.finditer(stripped):
        host = m.group(1).lower()
        if host in _NOT_DOMAINS or re.fullmatch(r"[\d.]+", host):
            continue
        found.append("https://" + m.group(0).rstrip(".,;:!?"))
    seen: list[str] = []
    for url in found:
        if url not in seen:
            seen.append(url)
    return seen
