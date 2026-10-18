"""The agent framework: risk levels, capabilities, results and the agent base class.

An agent is a Python class with one or more *capabilities*. Each capability
declares a risk level, and the task layer (app/tasks.py) — not the agent and
not the AI planner — decides from that level whether a human must approve the
task before it runs. Agents therefore cannot talk their way past an approval.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

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
    logs: list[tuple[str, str]] = field(default_factory=list)

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

    def params_schema(self) -> dict[str, Any]:
        return self.params_model.model_json_schema()


def capability(
    name: str | None = None,
    *,
    risk: Risk,
    description: str,
    params: type[Params] = NoParams,
    examples: tuple[str, ...] = (),
) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        fn.__capability__ = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "description": description,
            "risk": risk,
            "params_model": params,
            "examples": examples,
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

    async def run(self, ctx: AgentContext, capability_name: str, raw_params: dict[str, Any]) -> AgentResult:
        cap = self._capabilities[capability_name]
        params = cap.params_model.model_validate(raw_params or {})
        handler = getattr(self, cap.method_name)
        return await handler(ctx, params)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
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
