"""Adattatore per qualsiasi endpoint compatibile con Chat Completions.

Copre servizi con piano gratuito che espongono questa API (es. Groq, OpenRouter,
Google AI Studio in modalità compatibile) e anche Ollama stesso su /v1.
La chiave si legge da una variabile d'ambiente e non viene mai registrata nei log.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from devops_agent.agent.providers.base import arguments_as_str, tool_to_function_spec, with_retries
from devops_agent.agent.types import LLMResponse, Message, ProviderError, ToolCall, ToolSpec


# Qui copriamo i servizi che espongono API compatibili con OpenAI (/chat/completions). La chiave API la
# prendiamo da una variabile d'ambiente, mai dal file di configurazione.
class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        model: str,
        base_url: str,
        *,
        api_key_env: str | None = None,
        temperature: float = 0.0,
        timeout_s: float = 120.0,
    ) -> None:
        self.model = model
        self.temperature = temperature
        headers = {}
        if api_key_env:
            key = os.environ.get(api_key_env)
            if not key:
                raise ProviderError(f"variabile d'ambiente {api_key_env} non impostata")
            headers["Authorization"] = f"Bearer {key}"
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s, headers=headers)

    # Stessa idea dell'adattatore Ollama: chiamiamo /chat/completions e convertiamo la risposta nel formato
    # comune.
    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_wire(m) for m in messages],
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = [tool_to_function_spec(t) for t in tools]
            payload["tool_choice"] = "auto"

        data = await with_retries(
            lambda: self._client.post("/chat/completions", json=payload), provider=self.name
        )
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError("openai_compat: risposta senza 'choices'") from exc

        calls = [
            ToolCall(
                id=str(raw.get("id") or f"call_{i}"),
                name=str((raw.get("function") or {}).get("name", "")),
                arguments=(raw.get("function") or {}).get("arguments", "{}"),
            )
            for i, raw in enumerate(message.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        return LLMResponse(
            text=message.get("content") or None,
            tool_calls=calls,
            tokens_in=usage.get("prompt_tokens"),
            tokens_out=usage.get("completion_tokens"),
            finish_reason=choice.get("finish_reason"),
        )

    # Traduzione del messaggio verso il formato delle API OpenAI.
    @staticmethod
    def _to_wire(m: Message) -> dict[str, Any]:
        if m["role"] == "assistant":
            wire: dict[str, Any] = {"role": "assistant", "content": m.get("content")}
            if m.get("tool_calls"):
                wire["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": arguments_as_str(c.arguments)},
                    }
                    for c in m["tool_calls"]
                ]
            return wire
        if m["role"] == "tool":
            return {"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]}
        return {"role": m["role"], "content": m["content"]}

    async def aclose(self) -> None:
        await self._client.aclose()
