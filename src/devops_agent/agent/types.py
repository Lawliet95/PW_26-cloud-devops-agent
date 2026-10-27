"""Tipi condivisi dall'harness.

I messaggi della conversazione usano un formato neutro (dict) indipendente dal
fornitore del modello; ogni adattatore lo converte nel proprio formato:

    {"role": "system" | "user", "content": str}
    {"role": "assistant", "content": str | None, "tool_calls": list[ToolCall]}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

Message = dict[str, Any]


# Una chiamata a uno strumento richiesta dal modello: id, nome e argomenti.
@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] | str  # alcuni provider restituiscono una stringa JSON, anche malformata


# Risposta del modello nel formato condiviso da tutti gli adattatori. Se il fornitore non comunica i token
# usati li lasciamo a None: preferiamo non stimarli.
@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    tokens_in: int | None  # None = metrica non fornita dal provider: va dichiarato, non stimato
    tokens_out: int | None
    finish_reason: str | None = None


# La solleviamo quando il modello resta irraggiungibile anche dopo i tentativi.
class ProviderError(Exception):
    """Errore non recuperabile del provider, dopo gli eventuali tentativi di retry."""


# Lo strumento visto dalla parte del modello, con in più l'indicazione se scrive o no.
@dataclass(frozen=True)
class ToolSpec:
    """Tool come lo vede il modello, con la classificazione decisa dall'harness."""

    name: str
    description: str
    input_schema: dict[str, Any]
    is_write: bool


# Com'è andata un'esecuzione: ok o no, il contenuto ed eventualmente il codice d'errore.
@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    content: str
    error_code: str | None = None


# I limiti del ciclo raccolti in un posto solo: iterazioni, token, errori, ripetizioni e timeout.
@dataclass
class Limits:
    max_iterations: int = 8
    max_total_tokens: int = 30_000
    max_invalid_args: int = 3
    max_repeated_calls: int = 2
    max_tool_calls_per_turn: int = 5
    tool_timeout_s: float = 20.0


# Traccia di una chiamata eseguita davvero, insieme al suo esito.
@dataclass
class ExecutedCall:
    name: str
    arguments: dict[str, Any]
    ok: bool
    error_code: str | None


# Ciò che resta alla fine di una richiesta: risposta, motivo dell'arresto e metriche.
@dataclass
class RunResult:
    answer: str
    stop_reason: str
    iterations: int
    llm_calls: int
    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int
    executed: list[ExecutedCall] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)  # conversazione senza il prompt di sistema
