"""Log strutturato JSON Lines: un evento per riga, un file per esecuzione.

Eventi: run_start, llm_request, llm_response, llm_error, tool_rejected,
tool_invalid_args, confirmation, tool_call, tool_result, loop_detected, stop.

I token sono registrati come null quando il provider non li fornisce: il valore
mancante va dichiarato nella relazione, non stimato.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Abbastanza da contenere per intero il risultato più grande di un tool (50 richieste, ~10 kB):
# per verificare la risposta del modello contro i dati il log deve riportarli senza tagli.
MAX_STRING = 20000

Listener = Callable[[dict[str, Any]], None]


# Ogni esecuzione prende un nome fatto da data e ora UTC più sei caratteri casuali: così due file di log non
# rischiano mai di sovrascriversi.
def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


# Prima di finire nel log un valore va ripulito: tagliamo le stringhe troppo lunghe e convertiamo in testo ciò
# che JSON non sa rappresentare. Dizionari e liste li visitiamo ricorsivamente.
def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + f"…[+{len(value) - MAX_STRING}]"
    if isinstance(value, dict):
        return {str(k): _sanitize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(v) for v in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _sanitize(str(value))


# RunLogger è il registro di un'esecuzione. Ogni evento diventa una riga del file .jsonl, rimane in memoria
# (comodo nei test) e viene girato ai listener, come la CLI che lo mostra a video.
class RunLogger:
    def __init__(
        self, log_dir: str | Path | None, run_id: str | None = None, context: dict[str, Any] | None = None
    ) -> None:
        self.run_id = run_id or new_run_id()
        self.context = context or {}
        self.events: list[dict[str, Any]] = []
        self.listeners: list[Listener] = []
        self.path: Path | None = None
        if log_dir is not None:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            self.path = Path(log_dir) / f"{self.run_id}.jsonl"

    # Con event() annotiamo un evento: ci aggiungiamo data, id dell'esecuzione e contesto, lo scriviamo su
    # file e lo passiamo a chi sta ascoltando.
    def event(self, kind: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "run_id": self.run_id,
            **self.context,
            "event": kind,
            **_sanitize(fields),
        }
        self.events.append(record)
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        for listener in self.listeners:
            listener(record)
