"""The Claude provider, against a mocked Messages API (no key or network needed)."""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from app.config import Settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMError, LLMRefused


def message(content: list[dict], stop_reason: str = "end_turn") -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 40},
    }


def provider(handler, **settings) -> tuple[AnthropicProvider, list]:
    seen: list = []

    def wrapped(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return handler(request)

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        base_url="https://api.anthropic.test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(wrapped)),
    )
    s = Settings(anthropic_api_key="sk-ant-test", llm_provider="anthropic", **settings)
    return AnthropicProvider(s, client=client, record_usage=False), seen


async def test_json_request_shape_and_parse():
    plan = {"reply": "On it.", "steps": []}
    p, seen = provider(
        lambda r: httpx2.Response(200, json=message([{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": json.dumps(plan)}]))
    )
    schema = {"type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"], "additionalProperties": False}
    result = await p.json(purpose="plan", system="sys", prompt="hello", schema=schema)
    assert result == plan
    body = json.loads(seen[0].content)
    assert body["model"] == "claude-opus-5-5"
    assert body["system"] == "sys"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    assert body["fallbacks"] == "default"
    assert "thinking" not in body and "temperature" not in body
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert seen[0].headers["x-api-key"] == "sk-ant-test"


async def test_text_request():
    p, seen = provider(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": "Summary."}])))
    assert await p.text(purpose="summarize", system="s", prompt="p") == "Summary."
    assert "format" not in json.loads(seen[0].content)["output_config"]


async def test_model_without_server_fallback_omits_it():
    p, seen = provider(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": "ok"}])), llm_model="claude-haiku-5-5")
    await p.text(purpose="x", system="s", prompt="p")
    body = json.loads(seen[0].content)
    assert "fallbacks" not in body
    assert "anthropic-beta" not in seen[0].headers


async def test_refusal_raises():
    p, _ = provider(lambda r: httpx2.Response(200, json=message([], stop_reason="refusal")))
    with pytest.raises(LLMRefused):
        await p.text(purpose="x", system="s", prompt="p")


async def test_truncation_raises():
    p, _ = provider(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": "{\"a\":"}], stop_reason="max_tokens")))
    with pytest.raises(LLMError):
        await p.json(purpose="x", system="s", prompt="p", schema={})


@pytest.mark.parametrize("status", [400, 401, 429, 500, 529])
async def test_api_errors_become_llm_errors(status):
    p, _ = provider(lambda r: httpx2.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "nope"}}))
    with pytest.raises(LLMError):
        await p.text(purpose="x", system="s", prompt="p")


async def test_invalid_json_raises():
    p, _ = provider(lambda r: httpx2.Response(200, json=message([{"type": "text", "text": "not json"}])))
    with pytest.raises(LLMError):
        await p.json(purpose="x", system="s", prompt="p", schema={})
