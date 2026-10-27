"""Configurazione da file TOML (config.toml), con valori predefiniti ragionevoli."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from devops_agent.agent.types import Limits

DEFAULT_CONFIG_PATH = Path("config.toml")

READ_TOOLS = ["list_services", "list_teams", "list_requests", "get_request", "get_pipeline_status"]
WRITE_TOOLS = ["create_request", "assign_request", "update_request_status"]


# Ogni sezione di config.toml ha la sua classe. Partiamo da [model], il modello da usare.
@dataclass
class ModelConfig:
    provider: str = "ollama"  # ollama | openai_compat | scripted
    model: str = "qwen2.5:7b"
    base_url: str = "http://localhost:11434"
    api_key_env: str | None = None
    temperature: float = 0.0
    timeout_s: float = 180.0
    num_ctx: int | None = None
    think: bool | None = None


# [agent]: ruolo, utente, organizzazione e quanti turni di chat ricordare.
@dataclass
class AgentConfig:
    role: str = "operator"
    user: str = "operatore.demo"
    organization: str = "Nimbus Managed Services S.r.l."
    # Turni precedenti (richiesta dell'utente e relativi passi) mantenuti in chat.
    # Una cronologia illimitata satura la finestra di contesto dei modelli locali.
    history_turns: int = 4


# [paths]: dove teniamo database e log.
@dataclass
class PathsConfig:
    db: str = "data/devops.db"
    logs: str = "logs"


# [policy]: strumenti di lettura, di scrittura e cosa può usare ciascun ruolo.
@dataclass
class PolicyConfig:
    read_tools: list[str] = field(default_factory=lambda: list(READ_TOOLS))
    write_tools: list[str] = field(default_factory=lambda: list(WRITE_TOOLS))
    roles: dict[str, list[str]] = field(
        default_factory=lambda: {
            "viewer": list(READ_TOOLS),
            "operator": [*READ_TOOLS, *WRITE_TOOLS],
        }
    )


# La configurazione completa, con tutte le sezioni.
@dataclass
class Config:
    model: ModelConfig = field(default_factory=ModelConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    limits: Limits = field(default_factory=Limits)
    paths: PathsConfig = field(default_factory=PathsConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)


# Costruiamo una sezione della configurazione e protestiamo se nel file compare una chiave sconosciuta: un
# errore di battitura non deve passare inosservato.
def _build(cls: type, data: dict[str, Any], section: str) -> Any:
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"[{section}] chiavi non riconosciute: {', '.join(sorted(unknown))}")
    return cls(**data)


# Carichiamo config.toml; se il file manca, ripieghiamo sui valori predefiniti.
def load_config(path: str | Path | None = None) -> Config:
    """Carica la configurazione; se il file non esiste usa i valori predefiniti."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        if path:
            raise FileNotFoundError(f"file di configurazione non trovato: {cfg_path}")
        return Config()
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    sections = {
        "model": ModelConfig,
        "agent": AgentConfig,
        "limits": Limits,
        "paths": PathsConfig,
        "policy": PolicyConfig,
    }
    unknown = set(raw) - set(sections)
    if unknown:
        raise ValueError(f"sezioni non riconosciute in {cfg_path}: {', '.join(sorted(unknown))}")
    return Config(**{name: _build(cls, raw.get(name, {}), name) for name, cls in sections.items()})
