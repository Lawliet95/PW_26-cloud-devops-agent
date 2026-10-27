"""Composizione dell'harness: configurazione, server MCP, policy e modello."""

from __future__ import annotations

from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from devops_agent.agent.logger import RunLogger
from devops_agent.agent.loop import ConfirmFn, run_agent
from devops_agent.agent.mcp_client import McpToolExecutor
from devops_agent.agent.policy import Policy
from devops_agent.agent.prompts import build_system_prompt
from devops_agent.agent.providers import LLMProvider, create_provider
from devops_agent.agent.types import Limits, Message, RunResult, ToolSpec
from devops_agent.config import Config


# Della chat conserviamo solo gli ultimi N turni. Tagliamo sempre in corrispondenza di un messaggio
# dell'utente, per non separare mai una chiamata a uno strumento dal suo risultato.
def trim_history(messages: list[Message], turns: int) -> list[Message]:
    """Mantiene gli ultimi `turns` turni (dal messaggio dell'utente in poi).

    Il taglio avviene sempre su un messaggio dell'utente, così non separa mai una
    chiamata a tool dal suo risultato. Con turns <= 0 la cronologia è vuota.
    """
    if turns <= 0:
        return []
    starts = [i for i, m in enumerate(messages) if m["role"] == "user"]
    if len(starts) <= turns:
        return list(messages)
    return list(messages[starts[-turns] :])


OLD_RESULT = (
    "[risultato di un turno precedente, non più disponibile: per dati aggiornati richiama lo strumento]"
)


# Cronologia pronta per il turno successivo: gli ultimi N turni, senza i vecchi risultati degli strumenti.
# Così il modello è costretto a rileggere i dati anziché ricopiarli.
def history_for_next_turn(messages: list[Message], turns: int) -> list[Message]:
    """Cronologia per il turno successivo della chat: ultimi `turns` turni, senza i risultati dei tool.

    I risultati delle letture precedenti possono essere superati (dopo una scrittura) o parziali (letti
    con un filtro): toglierli impedisce al modello di riusarli come se fossero attuali e riduce i token.
    Restano richieste, risposte e chiamate, quindi il filo della conversazione.
    """
    return [{**m, "content": OLD_RESULT} if m["role"] == "tool" else m for m in trim_history(messages, turns)]


# AgentSession mette insieme quello che serve per fare domande all'agente: configurazione, policy, modello e
# client MCP. Il server lo avviamo una volta sola e lo riutilizziamo per più richieste.
class AgentSession:
    """Una sessione apre il server MCP una volta e lo riusa per più richieste."""

    def __init__(
        self,
        cfg: Config,
        *,
        db_path: str | Path | None = None,
        provider: LLMProvider | None = None,
        fault_tools: list[str] | None = None,
        limits: Limits | None = None,
        script: list[dict[str, Any]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.db_path = Path(db_path or cfg.paths.db)
        self.policy = Policy(cfg.policy, cfg.agent.role)
        self.provider = provider or create_provider(cfg.model, script=script)
        self.limits = limits or cfg.limits
        self.executor = McpToolExecutor(
            self.db_path,
            actor=f"{cfg.agent.user} via agente",
            fault_tools=fault_tools,
            server_log=Path(cfg.paths.logs) / "mcp_server.log",
            read_timeout_s=self.limits.tool_timeout_s + 5,
        )
        self.tools: list[ToolSpec] = []
        self.excluded: dict[str, str] = {}
        self.system_prompt = build_system_prompt(
            organization=cfg.agent.organization, user=cfg.agent.user, role=cfg.agent.role
        )
        self._stack: AsyncExitStack | None = None

    # Entrando nella sessione avviamo il server MCP, leggiamo gli strumenti, applichiamo la policy del ruolo e
    # solo dopo prepariamo il prompt, che cambia se il ruolo non può scrivere.
    async def __aenter__(self) -> AgentSession:
        if not self.db_path.exists():
            raise FileNotFoundError(f"database non trovato: {self.db_path}. Esegui 'devops-agent init-db'.")
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(self.executor)
        self._stack.push_async_callback(self.provider.aclose)
        self.tools, self.excluded = self.policy.build_catalog(await self.executor.list_tools())
        # Il prompt dipende dagli strumenti effettivamente visibili: un ruolo senza scritture lo dichiara.
        self.system_prompt = build_system_prompt(
            organization=self.cfg.agent.organization,
            user=self.cfg.agent.user,
            role=self.cfg.agent.role,
            can_write=any(t.is_write for t in self.tools),
        )
        return self

    # Uscendo spegniamo il server MCP e chiudiamo il client del modello.
    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()

    # Un registro nuovo per ogni richiesta, salvato nella cartella dei log.
    def new_logger(self, log_dir: str | Path | None = None, **context: Any) -> RunLogger:
        logger = RunLogger(
            log_dir if log_dir is not None else Path(self.cfg.paths.logs) / "runs", context=context
        )
        return logger

    # Il metodo che usiamo per fare una domanda all'agente: lancia il ciclo con strumenti, prompt e limiti
    # della sessione (più l'eventuale cronologia della chat) e ne restituisce il risultato.
    async def ask(
        self, request: str, *, confirm: ConfirmFn, logger: RunLogger, history: list[Message] | None = None
    ) -> RunResult:
        if self.excluded:
            logger.event("tools_excluded", role=self.policy.role, excluded=self.excluded)
        return await run_agent(
            request,
            llm=self.provider,
            tools=self.tools,
            executor=self.executor,
            confirm=confirm,
            logger=logger,
            system_prompt=self.system_prompt,
            limits=self.limits,
            history=history,
        )
