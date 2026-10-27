"""Test degli adattatori dei modelli con un trasporto HTTP simulato."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from devops_agent.agent.providers.ollama import OllamaProvider
from devops_agent.agent.providers.openai_compat import OpenAICompatProvider
from devops_agent.agent.types import ProviderError, ToolCall, ToolSpec

TOOL = ToolSpec("get_request", "dettaglio", {"type": "object", "properties": {}}, False)
HISTORY = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "u"},
    {"role": "assistant", "content": None, "tool_calls": [ToolCall("c1", "get_request", {"request_id": 3})]},
    {"role": "tool", "tool_call_id": "c1", "name": "get_request", "content": "{}"},
]


def _mock(provider, handler):
    provider._client = httpx.AsyncClient(
        base_url="http://test", transport=httpx.MockTransport(handler), headers=provider._client.headers
    )
    return provider


def test_ollama_wire_format_and_parsing():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "get_request", "arguments": {"request_id": 5}}}],
                },
                "prompt_eval_count": 120,
                "eval_count": 15,
                "done_reason": "stop",
            },
        )

    provider = _mock(OllamaProvider("modello"), handler)
    response = asyncio.run(provider.generate(HISTORY, [TOOL]))
    assert seen["stream"] is False and seen["tools"][0]["function"]["name"] == "get_request"
    assert seen["messages"][2]["tool_calls"][0]["function"]["arguments"] == {"request_id": 3}
    assert seen["messages"][3] == {"role": "tool", "content": "{}", "tool_name": "get_request"}
    assert response.tool_calls[0].arguments == {"request_id": 5}
    assert (response.tokens_in, response.tokens_out, response.text) == (120, 15, None)


def test_openai_compat_wire_format_and_missing_usage(monkeypatch):
    monkeypatch.setenv("TEST_KEY", "segreto")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "x1",
                                    "type": "function",
                                    "function": {"name": "get_request", "arguments": '{"request_id": 7}'},
                                }
                            ],
                        },
                    }
                ]
            },
        )

    provider = _mock(OpenAICompatProvider("m", "http://test", api_key_env="TEST_KEY"), handler)
    response = asyncio.run(provider.generate(HISTORY, [TOOL]))
    assert seen["auth"] == "Bearer segreto"
    assert seen["messages"][2]["tool_calls"][0]["function"]["arguments"] == '{"request_id": 3}'
    assert seen["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
    assert response.tool_calls[0] == ToolCall("x1", "get_request", '{"request_id": 7}')
    assert response.tokens_in is None  # usage assente: dichiarato, non stimato


def test_missing_api_key_is_reported(monkeypatch):
    monkeypatch.delenv("ASSENTE", raising=False)
    with pytest.raises(ProviderError):
        OpenAICompatProvider("m", "http://test", api_key_env="ASSENTE")


def test_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": "model does not support tools"})

    provider = _mock(OllamaProvider("m"), handler)
    with pytest.raises(ProviderError, match="does not support tools"):
        asyncio.run(provider.generate(HISTORY, [TOOL]))
    assert len(calls) == 1


def test_server_error_is_retried(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503, text="occupato")
        return httpx.Response(200, json={"message": {"content": "ok"}})

    async def no_sleep(_):
        return None

    monkeypatch.setattr("devops_agent.agent.providers.base.asyncio.sleep", no_sleep)
    provider = _mock(OllamaProvider("m"), handler)
    response = asyncio.run(provider.generate(HISTORY, [TOOL]))
    assert response.text == "ok" and len(calls) == 3


def test_simplified_schema_keeps_parameter_named_title():
    from devops_agent.agent.providers.base import simplify_schema

    schema = {
        "type": "object",
        "title": "create_requestArguments",
        "properties": {
            "title": {"type": "string", "title": "Title"},
            "status": {"anyOf": [{"enum": ["open"], "type": "string"}, {"type": "null"}], "default": None},
        },
        "required": ["title"],
    }
    simple = simplify_schema(schema)
    assert "title" not in {k for k, v in simple.items() if isinstance(v, str)}
    assert simple["properties"]["title"] == {"type": "string"}
    assert simple["properties"]["status"] == {"enum": ["open"], "type": "string"}
