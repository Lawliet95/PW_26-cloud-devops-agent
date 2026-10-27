"""Genera docs/tools.md: una scheda per tool, con schemi letti dal server reale
ed esempi di invocazione eseguiti su un database appena inizializzato.

    python scripts/export_tool_docs.py [--out docs/tools.md]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from devops_agent.agent.mcp_client import McpToolExecutor
from devops_agent.backend.database import init_db
from devops_agent.config import PolicyConfig

EFFECTS = {
    "list_services": "Nessuno (sola lettura).",
    "list_teams": "Nessuno (sola lettura).",
    "list_requests": "Nessuno (sola lettura).",
    "get_request": "Nessuno (sola lettura).",
    "get_pipeline_status": "Nessuno (sola lettura).",
    "create_request": (
        "Inserisce una riga in `operational_request` (stato `open`, `team_id` NULL) e una voce "
        "`created` in `request_history`, nella stessa transazione. Richiede conferma dell'utente."
    ),
    "update_request_status": (
        "Aggiorna `status` e `updated_at` della richiesta secondo le transizioni ammesse e registra "
        "`status_changed` con la nota in `request_history`, nella stessa transazione. Le richieste "
        "`resolved` e `closed` escono dalla coda delle aperte. Richiede conferma dell'utente."
    ),
    "assign_request": (
        "Aggiorna `team_id` e `updated_at` della richiesta; se era `open` la porta a `in_progress`. "
        "Registra `assigned` (e `status_changed` se cambia lo stato) in `request_history`, nella stessa "
        "transazione. Richiede conferma dell'utente."
    ),
}

ERRORS = {
    "list_services": "Parametri non validi (ambiente non ammesso).",
    "list_teams": "—",
    "list_requests": "`INVALID_ARGUMENT` (limite fuori intervallo, filtri incompatibili), `NOT_FOUND` (team o servizio inesistente).",
    "get_request": "`NOT_FOUND` se la richiesta non esiste.",
    "get_pipeline_status": "`NOT_FOUND` se il servizio o la pipeline non esistono (il messaggio elenca le pipeline disponibili).",
    "create_request": "`INVALID_ARGUMENT` (titolo, descrizione, priorità), `NOT_FOUND` (servizio inesistente).",
    "update_request_status": "`NOT_FOUND` (richiesta), `CONFLICT` (stesso stato o transizione non ammessa, il messaggio elenca quelle valide), `INVALID_ARGUMENT` (nota fuori lunghezza).",
    "assign_request": "`NOT_FOUND` (richiesta o team), `CONFLICT` (richiesta chiusa o risolta, oppure già assegnata a quel team).",
}

EXAMPLES = {
    "list_services": {"name_contains": "billing"},
    "list_teams": {},
    "list_requests": {"status": "open", "priority": "high", "limit": 3, "order_by": "id_desc"},
    "get_request": {"request_id": 3},
    "get_pipeline_status": {"service_id": 3, "pipeline_name": "deploy"},
    "create_request": {
        "title": "Build lenta su payments-api",
        "description": "La build impiega il doppio del tempo da due giorni.",
        "priority": "high",
        "service_id": 1,
    },
    "assign_request": {"request_id": 2, "team_id": 4},
    "update_request_status": {"request_id": 3, "status": "resolved", "note": "Segreti ruotati e verificati"},
}


# Nella documentazione bastano un paio di elementi per far capire la forma del risultato, quindi qui
# accorciamo le liste lunghe degli esempi.
def _truncate(data: object, items: int = 2) -> object:
    if isinstance(data, dict):
        return {k: _truncate(v, items) for k, v in data.items()}
    if isinstance(data, list) and len(data) > items:
        return [_truncate(v, items) for v in data[:items]] + [f"… altri {len(data) - items} elementi"]
    return data


# Generiamo docs/tools.md partendo dal server avviato: ne leggiamo gli schemi e, su un database appena creato,
# lanciamo un esempio per ciascun tool.
async def build(out: Path) -> None:
    write_tools = set(PolicyConfig().write_tools)
    with tempfile.TemporaryDirectory() as tmp:
        db = init_db(Path(tmp) / "docs.db", seed=42)
        async with McpToolExecutor(db, actor="esempio") as ex:
            assert ex._client is not None
            listed = await ex._client.list_tools()
            lines = [
                "# Documentazione dei tool MCP",
                "",
                "> File generato da `scripts/export_tool_docs.py`: schemi letti dal server in esecuzione, "
                "esempi eseguiti su un database appena inizializzato (seed 42). Non modificarlo a mano.",
                "",
                "Gli errori previsti arrivano al modello con `is_error = true` e un codice stabile; "
                "l'harness li normalizza in `NOT_FOUND`, `INVALID_ARGUMENT`, `CONFLICT`, "
                "`PARAMETRI_NON_VALIDI`, `TOOL_FALLITO`, `TIMEOUT`, `TOOL_NON_DISPONIBILE`.",
                "",
                "| Tool | Classe | Conferma |",
                "|---|---|---|",
            ]
            for tool in listed.tools:
                is_write = tool.name in write_tools
                lines.append(
                    f"| `{tool.name}` | {'scrittura' if is_write else 'lettura'} | "
                    f"{'sì' if is_write else 'no'} |"
                )
            lines.append("")
            for tool in listed.tools:
                args = EXAMPLES[tool.name]
                outcome = await ex.call_tool(tool.name, args)
                result = json.loads(outcome.content) if outcome.ok else {"errore": outcome.content}
                lines += [
                    f"## `{tool.name}`",
                    "",
                    f"**Descrizione** (testo letto dal modello): {(tool.description or '').strip()}",
                    "",
                    f"**Classe**: {'scrittura' if tool.name in write_tools else 'lettura'}",
                    "",
                    f"**Effetti sui dati**: {EFFECTS[tool.name]}",
                    "",
                    f"**Errori previsti**: {ERRORS[tool.name]}",
                    "",
                    "**Schema di ingresso**",
                    "",
                    "```json",
                    json.dumps(tool.input_schema, ensure_ascii=False, indent=2),
                    "```",
                    "",
                    "**Schema di uscita**",
                    "",
                    "```json",
                    json.dumps(tool.output_schema, ensure_ascii=False, indent=2),
                    "```",
                    "",
                    "**Esempio di invocazione**",
                    "",
                    "```json",
                    json.dumps({"name": tool.name, "arguments": args}, ensure_ascii=False, indent=2),
                    "```",
                    "",
                    "**Risultato** (estratto)",
                    "",
                    "```json",
                    json.dumps(_truncate(result), ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"scritto {out}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/tools.md")
    asyncio.run(build(Path(parser.parse_args().out)))
