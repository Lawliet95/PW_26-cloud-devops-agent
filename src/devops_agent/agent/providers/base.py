"""Astrazione dal fornitore del modello.

Il ciclo agentico dipende solo da LLMProvider: per cambiare modello o fornitore
basta un altro adattatore e una riga di configurazione.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

import httpx

from devops_agent.agent.types import LLMResponse, Message, ProviderError, ToolSpec

T = TypeVar("T")

_DROPPED_KEYS = {"title"}


# Il contratto di ogni adattatore: generate() riceve messaggi e strumenti e risponde con testo, chiamate e
# token. Il ciclo agentico non conosce altro che questa interfaccia.
class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse: ...

    async def aclose(self) -> None: ...


# Prima di spedire gli schemi dei parametri al modello li alleggeriamo (via i titoli, campi facoltativi
# semplificati) per risparmiare token. Per la validazione resta comunque lo schema completo.
def simplify_schema(schema: Any) -> Any:
    """Versione compatta dello schema da mostrare al modello.

    - toglie i 'title' generati da Pydantic (token inutili);
    - riduce 'anyOf: [X, null]' a X: il parametro è comunque facoltativo perché
      non compare in 'required'.
    La validazione dell'harness continua a usare lo schema originale del server.
    """
    if isinstance(schema, list):
        return [simplify_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        non_null = [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]
        if len(non_null) == 1 and len(variants) == 2:
            merged = {k: v for k, v in schema.items() if k not in {"anyOf", "default"}}
            if schema.get("default") is not None:
                merged["default"] = schema["default"]
            return simplify_schema({**non_null[0], **merged})
    return {
        key: simplify_schema(value)
        for key, value in schema.items()
        if key not in _DROPPED_KEYS or not isinstance(value, str)
    }


# Convertiamo uno strumento nel formato "function calling" che capiscono sia Ollama sia OpenAI.
def tool_to_function_spec(tool: ToolSpec) -> dict[str, Any]:
    """Formato 'function calling' comune a Ollama e alle API compatibili OpenAI."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": simplify_schema(tool.input_schema),
        },
    }


# Argomenti → dizionario. Se non sono JSON valido non li buttiamo: finiscono in _raw.
def arguments_as_dict(arguments: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": arguments}
    return value if isinstance(value, dict) else {"_raw": arguments}


# Il percorso inverso: dagli argomenti otteniamo una stringa JSON.
def arguments_as_str(arguments: dict[str, Any] | str) -> str:
    return arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)


# Spediamo la richiesta HTTP e, se l'errore è temporaneo (rete, troppe richieste, errore del server), ci
# riproviamo con attese via via più lunghe. Se proprio non va, solleviamo ProviderError.
async def with_retries(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    provider: str,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> dict[str, Any]:
    """Esegue la richiesta HTTP con retry su errori transitori (rete, 429, 5xx)."""
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = await send()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                retry_after = response.headers.get("retry-after")
                if retry_after and retry_after.isdigit() and attempt < attempts:
                    await asyncio.sleep(min(int(retry_after), 30))
                    continue
            elif response.status_code >= 400:
                # Errore del client (modello inesistente, tool non supportati, chiave errata): inutile riprovare.
                raise ProviderError(f"{provider}: HTTP {response.status_code}: {response.text[:500]}")
            else:
                try:
                    return response.json()
                except ValueError as exc:
                    raise ProviderError(f"{provider}: risposta non JSON") from exc
        if attempt < attempts:
            await asyncio.sleep(base_delay * 2 ** (attempt - 1))
    raise ProviderError(f"{provider}: non raggiungibile dopo {attempts} tentativi ({last_error})")
