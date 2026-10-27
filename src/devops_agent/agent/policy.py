"""Policy dell'harness: quali tool vede il modello e quali richiedono conferma.

Regole:
- default deny: un tool esposto dal server ma non classificato in configurazione
  non viene mai mostrato al modello;
- la classificazione lettura/scrittura è dell'harness, non delle annotazioni del
  server (la specifica MCP le considera informazioni non fidate);
- ogni tool di scrittura richiede la conferma dell'utente sui parametri esatti.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from devops_agent.agent.types import ToolSpec
from devops_agent.config import PolicyConfig

EFFECT_TEMPLATES = {
    "create_request": 'Crea la richiesta "{title}" con priorità {priority} sul servizio {service_id}',
    "assign_request": "Assegna la richiesta {request_id} al team {team_id}",
    "update_request_status": 'Porta la richiesta {request_id} allo stato {status} · nota: "{note}"',
}


# Uno strumento così come lo dichiara il server MCP.
@dataclass(frozen=True)
class RemoteTool:
    """Tool come dichiarato dal server MCP."""

    name: str
    description: str
    input_schema: dict[str, Any]


# La Policy stabilisce quali strumenti può usare un ruolo. Uno strumento non classificato in configurazione
# non arriva mai al modello: di default neghiamo.
class Policy:
    # Nel costruttore verifichiamo che il ruolo esista e che nessuno strumento sia insieme di lettura e di
    # scrittura.
    def __init__(self, cfg: PolicyConfig, role: str) -> None:
        if role not in cfg.roles:
            raise ValueError(f"ruolo sconosciuto: {role}. Ruoli disponibili: {', '.join(cfg.roles)}")
        overlap = set(cfg.read_tools) & set(cfg.write_tools)
        if overlap:
            raise ValueError(f"tool classificati sia lettura sia scrittura: {', '.join(sorted(overlap))}")
        self.role = role
        self.read_tools = frozenset(cfg.read_tools)
        self.write_tools = frozenset(cfg.write_tools)
        self.allowed = frozenset(cfg.roles[role])

    # Lettura, scrittura o niente (None) se lo strumento non è classificato.
    def classify(self, name: str) -> str | None:
        if name in self.write_tools:
            return "write"
        if name in self.read_tools:
            return "read"
        return None

    # Dividiamo gli strumenti del server in due gruppi, quelli visibili al ruolo e quelli esclusi, ciascuno
    # con il proprio motivo.
    def build_catalog(self, remote: Iterable[RemoteTool]) -> tuple[list[ToolSpec], dict[str, str]]:
        """Restituisce i tool visibili al ruolo e i tool esclusi con il motivo."""
        visible: list[ToolSpec] = []
        excluded: dict[str, str] = {}
        for tool in remote:
            kind = self.classify(tool.name)
            if kind is None:
                excluded[tool.name] = "non classificato in configurazione"
            elif tool.name not in self.allowed:
                excluded[tool.name] = f"non consentito al ruolo {self.role}"
            else:
                visible.append(ToolSpec(tool.name, tool.description, tool.input_schema, kind == "write"))
        return visible, excluded

    # Descrizione in italiano dell'effetto di una scrittura, che mostriamo nel pannello di conferma.
    @staticmethod
    def describe_effect(name: str, args: dict[str, Any]) -> str:
        template = EFFECT_TEMPLATES.get(name)
        if template is None:
            return f"Esegue {name}"
        try:
            return template.format(**args)
        except (KeyError, IndexError, ValueError):
            return f"Esegue {name}"
