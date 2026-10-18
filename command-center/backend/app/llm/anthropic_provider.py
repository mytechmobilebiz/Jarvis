"""Claude, through the official Anthropic SDK."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

import anthropic
from sqlalchemy import func, select

from ..config import Settings
from ..db import session_scope
from ..models import LLMUsage
from .base import LLMBudgetExceeded, LLMError, LLMProvider, LLMRefused

log = logging.getLogger(__name__)

# Models that accept the server-side refusal fallback (`fallbacks: "default"`).
_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider(LLMProvider):
    available = True

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None, record_usage: bool = True):
        assert settings.anthropic_api_key is not None
        self.settings = settings
        self.model = settings.llm_model
        self.name = f"anthropic:{self.model}"
        self.record_usage = record_usage
        self.client = client or anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            timeout=settings.llm_timeout_seconds,
            max_retries=2,
        )

    async def _check_budget(self) -> None:
        if not self.record_usage:
            return
        start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with session_scope() as session:
            used = await session.scalar(
                select(func.count()).select_from(LLMUsage).where(LLMUsage.at >= start, LLMUsage.provider == "anthropic")
            )
        if (used or 0) >= self.settings.llm_daily_request_cap:
            raise LLMBudgetExceeded(
                f"daily cap of {self.settings.llm_daily_request_cap} model requests reached; "
                "falling back to rule-based behaviour until tomorrow (UTC)"
            )

    async def _record(self, purpose: str, usage: Any, ok: bool, error: str = "") -> None:
        if not self.record_usage:
            return
        async with session_scope() as session:
            session.add(
                LLMUsage(
                    provider="anthropic",
                    model=self.model,
                    purpose=purpose,
                    input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    ok=ok,
                    error=error[:500],
                )
            )

    async def _create(self, *, purpose: str, system: str, prompt: str, max_tokens: int, fmt: dict | None):
        await self._check_budget()
        output_config: dict[str, Any] = {"effort": self.settings.llm_effort}
        if fmt is not None:
            output_config["format"] = fmt
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": output_config,
        }
        if self.model in _FALLBACK_MODELS:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            response = await self.client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            await self._record(purpose, None, False, f"rate limited: {exc}")
            raise LLMError("the model API is rate limiting requests") from exc
        except anthropic.APIStatusError as exc:
            await self._record(purpose, None, False, f"HTTP {exc.status_code}: {exc.message}")
            raise LLMError(f"model API error (HTTP {exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            await self._record(purpose, None, False, f"connection: {exc}")
            raise LLMError("could not reach the model API") from exc

        if response.stop_reason == "refusal":
            await self._record(purpose, response.usage, False, "refusal")
            raise LLMRefused("the model declined this request")
        if response.stop_reason == "max_tokens":
            await self._record(purpose, response.usage, False, "max_tokens")
            raise LLMError("the model response was cut off (max_tokens)")
        await self._record(purpose, response.usage, True)
        text = "".join(block.text for block in response.content if block.type == "text")
        return text

    async def json(
        self, *, purpose: str, system: str, prompt: str, schema: dict[str, Any], max_tokens: int = 16000
    ) -> dict[str, Any]:
        text = await self._create(
            purpose=purpose,
            system=system,
            prompt=prompt,
            max_tokens=max_tokens,
            fmt={"type": "json_schema", "schema": schema},
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError("the model returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise LLMError("the model returned JSON that is not an object")
        return data

    async def text(self, *, purpose: str, system: str, prompt: str, max_tokens: int = 16000) -> str:
        return await self._create(purpose=purpose, system=system, prompt=prompt, max_tokens=max_tokens, fmt=None)
