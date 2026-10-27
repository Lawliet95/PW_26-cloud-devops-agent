"""Client MCP: avvia il server via stdio e traduce i risultati in ToolOutcome."""

from __future__ import annotations

import json
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import IO, Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from devops_agent.agent.policy import RemoteTool
from devops_agent.agent.types import ToolOutcome

_ERROR_RE = re.compile(r"^Error executing tool \S+(?:: (?P<detail>.*))?$", re.DOTALL)
_CODE_RE = re.compile(r"^(?P<code>[A-Z][A-Z_]+): (?P<msg>.*)$", re.DOTALL)


# Il server MCP manda gli errori come testo: qui separiamo il codice dal messaggio.
def parse_error(text: str) -> tuple[str, str]:
    """Ricava (codice, messaggio) dal testo di errore restituito dal server."""
    if text.startswith("Unknown tool"):
        return "TOOL_NON_DISPONIBILE", text
    match = _ERROR_RE.match(text)
    detail = match.group("detail") if match else text
    if not detail:
        return "TOOL_FALLITO", "errore interno del server durante l'esecuzione del tool"
    if "validation error" in detail:
        return "PARAMETRI_NON_VALIDI", detail
    coded = _CODE_RE.match(detail)
    if coded:
        return coded.group("code"), coded.group("msg")
    return "TOOL_FALLITO", detail


# Il client MCP vero e proprio. Avvia il server come processo separato e dialoga con lui via stdin/stdout. Va
# usato con "async with", così siamo sicuri che il server venga chiuso alla fine.
class McpToolExecutor:
    """Connessione al server MCP del progetto, usata come async context manager."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        actor: str,
        fault_tools: list[str] | None = None,
        server_log: str | Path | None = None,
        read_timeout_s: float = 30.0,
    ) -> None:
        env = {"DEVOPS_AGENT_DB": str(Path(db_path).resolve()), "DEVOPS_AGENT_ACTOR": actor}
        if fault_tools:
            env["DEVOPS_AGENT_FAULT_TOOLS"] = ",".join(fault_tools)
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        self._params = StdioServerParameters(
            command=sys.executable, args=["-m", "devops_agent.mcp_server"], env=env
        )
        self._server_log = server_log
        self._read_timeout_s = read_timeout_s
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None

    # All'ingresso nel blocco avviamo il server e apriamo la connessione; i suoi messaggi li dirottiamo su un
    # file di log.
    async def __aenter__(self) -> McpToolExecutor:
        self._stack = AsyncExitStack()
        errlog: IO[str]
        if self._server_log:
            Path(self._server_log).parent.mkdir(parents=True, exist_ok=True)
            errlog = self._stack.enter_context(open(self._server_log, "a", encoding="utf-8"))  # noqa: SIM115
        else:
            errlog = self._stack.enter_context(open(os.devnull, "w"))  # noqa: SIM115
        client = Client(stdio_client(self._params, errlog=errlog), read_timeout_seconds=self._read_timeout_s)
        self._client = await self._stack.enter_async_context(client)
        return self

    # All'uscita chiudiamo sia la connessione sia il processo del server.
    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._client = self._stack = None

    # Domandiamo al server quali strumenti offre, con descrizione e schema dei parametri.
    async def list_tools(self) -> list[RemoteTool]:
        assert self._client is not None, "executor non avviato"
        result = await self._client.list_tools()
        return [RemoteTool(t.name, t.description or "", dict(t.input_schema)) for t in result.tools]

    # Questo metodo invoca uno strumento sul server e restituisce un ToolOutcome: in caso di successo il
    # risultato come JSON compatto, altrimenti codice e messaggio dell'errore.
    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        assert self._client is not None, "executor non avviato"
        result = await self._client.call_tool(name, args)
        text = "\n".join(getattr(block, "text", "") for block in result.content).strip()
        if result.is_error:
            code, message = parse_error(text)
            return ToolOutcome(False, message, code)
        if result.structured_content is not None:
            # JSON compatto: stesso contenuto del testo indentato, meno token.
            text = json.dumps(result.structured_content, ensure_ascii=False, separators=(",", ":"))
        return ToolOutcome(True, text)
