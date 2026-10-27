"""Adattatori dei fornitori di modelli e factory basata sulla configurazione."""

from __future__ import annotations

from typing import Any

from devops_agent.agent.providers.base import LLMProvider
from devops_agent.agent.providers.ollama import OllamaProvider
from devops_agent.agent.providers.openai_compat import OpenAICompatProvider
from devops_agent.agent.providers.scripted import ScriptedProvider
from devops_agent.config import ModelConfig

__all__ = ["LLMProvider", "OllamaProvider", "OpenAICompatProvider", "ScriptedProvider", "create_provider"]


# Fabbrica degli adattatori: in base alla configurazione restituisce Ollama, l'API compatibile OpenAI o le
# risposte registrate. È l'unico punto del codice che conosce i fornitori.
def create_provider(cfg: ModelConfig, *, script: list[dict[str, Any]] | None = None) -> LLMProvider:
    """Unico punto in cui il codice conosce i fornitori concreti."""
    if cfg.provider == "ollama":
        return OllamaProvider(
            cfg.model,
            cfg.base_url,
            temperature=cfg.temperature,
            timeout_s=cfg.timeout_s,
            num_ctx=cfg.num_ctx,
            think=cfg.think,
        )
    if cfg.provider == "openai_compat":
        return OpenAICompatProvider(
            cfg.model,
            cfg.base_url,
            api_key_env=cfg.api_key_env,
            temperature=cfg.temperature,
            timeout_s=cfg.timeout_s,
        )
    if cfg.provider == "scripted":
        return ScriptedProvider(script or [])
    raise ValueError(f"provider non supportato: {cfg.provider}")
