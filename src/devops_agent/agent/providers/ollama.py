"""Adattatore per un modello locale servito da Ollama (API /api/chat)."""

from __future__ import annotations

from typing import Any

import httpx

from devops_agent.agent.providers.base import (
    arguments_as_dict,
    tool_to_function_spec,
    with_retries,
)
from devops_agent.agent.types import LLMResponse, Message, ProviderError, ToolCall, ToolSpec


# Adattatore per Ollama, ossia per il modello che gira in locale sul PC.
class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        *,
        temperature: float = 0.0,
        timeout_s: float = 180.0,
        num_ctx: int | None = None,
        think: bool | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.think = think
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self._call_counter = 0

    # Questo metodo interroga /api/chat di Ollama con messaggi e strumenti e riporta la risposta nel formato
    # comune: testo, chiamate, token di input e di output.
    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        options: dict[str, Any] = {"temperature": self.temperature}
        if self.num_ctx:
            options["num_ctx"] = self.num_ctx
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_wire(m) for m in messages],
            "tools": [tool_to_function_spec(t) for t in tools],
            "stream": False,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think

        data = await with_retries(lambda: self._client.post("/api/chat", json=payload), provider=self.name)
        message = data.get("message")
        if not isinstance(message, dict):
            raise ProviderError("ollama: risposta senza campo 'message'")

        calls = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            self._call_counter += 1
            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{self._call_counter}"),
                    name=str(function.get("name", "")),
                    arguments=function.get("arguments", {}),
                )
            )
        return LLMResponse(
            text=message.get("content") or None,
            tool_calls=calls,
            tokens_in=data.get("prompt_eval_count"),
            tokens_out=data.get("eval_count"),
            finish_reason=data.get("done_reason"),
        )

    # Dal nostro formato interno a quello che Ollama si aspetta.
    @staticmethod
    def _to_wire(m: Message) -> dict[str, Any]:
        if m["role"] == "assistant":
            wire: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
            if m.get("tool_calls"):
                wire["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": arguments_as_dict(c.arguments)}}
                    for c in m["tool_calls"]
                ]
            return wire
        if m["role"] == "tool":
            return {"role": "tool", "content": m["content"], "tool_name": m["name"]}
        return {"role": m["role"], "content": m["content"]}

    async def aclose(self) -> None:
        await self._client.aclose()
