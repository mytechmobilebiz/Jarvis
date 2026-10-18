"""LLM provider interface. The supervisor works without one (rule-based planning)."""

from __future__ import annotations

from typing import Any


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """No model is configured."""


class LLMRefused(LLMError):
    """The model declined the request."""


class LLMBudgetExceeded(LLMError):
    """The daily request cap for the metered model has been reached."""


class LLMProvider:
    name: str = "none"
    available: bool = False

    async def json(
        self, *, purpose: str, system: str, prompt: str, schema: dict[str, Any], max_tokens: int = 16000
    ) -> dict[str, Any]:
        raise LLMUnavailable("no language model configured")

    async def text(self, *, purpose: str, system: str, prompt: str, max_tokens: int = 16000) -> str:
        raise LLMUnavailable("no language model configured")


class NullProvider(LLMProvider):
    """Used when no API key is set: every call says so, and callers fall back."""
