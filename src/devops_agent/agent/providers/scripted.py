"""Provider deterministico per i test: restituisce risposte predefinite.

Non è un modello: serve a verificare l'harness (arresti, errori, conferme, log)
senza dipendere da un LLM. I risultati ottenuti con questo provider NON vanno
presentati come prestazioni di un modello.
"""

from __future__ import annotations

from typing import Any

from devops_agent.agent.types import LLMResponse, Message, ToolCall, ToolSpec


# Un finto modello che legge da un copione: a ogni chiamata restituisce la risposta successiva già scritta. Lo
# impieghiamo nei test e negli scenari in CI; non dice nulla sulle capacità di un modello vero.
class ScriptedProvider:
    name = "scripted"

    def __init__(self, steps: list[dict[str, Any]], model: str = "script") -> None:
        self.model = model
        self._steps = list(steps)
        self._index = 0
        self.received: list[list[Message]] = []  # copia dei messaggi ricevuti, per le asserzioni

    # Tira fuori la prossima risposta del copione e si tiene da parte i messaggi ricevuti, così nei test
    # possiamo verificare cosa è arrivato al "modello".
    async def generate(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.received.append([dict(m) for m in messages])
        if self._index >= len(self._steps):
            return LLMResponse(text="[script esaurito]", tool_calls=[], tokens_in=None, tokens_out=None)
        step = self._steps[self._index]
        self._index += 1
        calls = [
            ToolCall(id=f"s{self._index}_{i}", name=c["name"], arguments=c.get("arguments", {}))
            for i, c in enumerate(step.get("tool_calls", []))
        ]
        return LLMResponse(text=step.get("text"), tool_calls=calls, tokens_in=None, tokens_out=None)

    async def aclose(self) -> None:
        return None
