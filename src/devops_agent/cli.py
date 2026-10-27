"""Interfaccia a riga di comando.

devops-agent init-db                 crea il database con i dati sintetici
devops-agent tools                   mostra i tool del server e la loro classificazione
devops-agent ask "richiesta"         esegue una singola richiesta
devops-agent chat                    conversazione interattiva
devops-agent scenarios               esegue gli scenari di test e produce il report
devops-agent tokens RISULTATI.json   analisi di token e costi dai risultati degli scenari
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from devops_agent.agent.policy import Policy
from devops_agent.agent.session import AgentSession, history_for_next_turn
from devops_agent.backend.database import init_db
from devops_agent.config import Config, load_config

console = Console()


# --------------------------------------------------------------------------- #
# Presentazione degli eventi dell'harness
# --------------------------------------------------------------------------- #


# Accorciamo i testi lunghi da stampare a video, aggiungendo i puntini.
def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Restituisce la funzione che stampa a video gli eventi del log mentre l'agente lavora (richieste al modello,
# strumenti, correzioni, conferme, arresto).
def make_renderer(verbose: bool):
    # Un evento alla volta, ognuno con il proprio formato a seconda del tipo.
    def render(e: dict[str, Any]) -> None:
        kind = e["event"]
        if kind == "llm_request":
            console.print(f"[dim]· iterazione {e['iteration']}: richiesta al modello[/dim]")
        elif kind == "llm_response":
            tokens = (
                f"{e.get('tokens_in') if e.get('tokens_in') is not None else 'n/d'} in / "
                f"{e.get('tokens_out') if e.get('tokens_out') is not None else 'n/d'} out"
            )
            console.print(f"[dim]  risposta in {e['duration_ms']} ms · token {tokens}[/dim]")
            for call in e["tool_calls"]:
                console.print(f"  [cyan]→ {call['name']}[/cyan] {_short(call['arguments'])}")
        elif kind == "tool_rejected":
            console.print(f"  [yellow]✗ {e['tool']} rifiutato ({e['reason']})[/yellow]")
        elif kind == "tool_invalid_args":
            console.print(f"  [yellow]✗ {e['tool']}: parametri non validi · {_short(e['error'])}[/yellow]")
        elif kind == "harness_lookup":
            console.print(
                f"  [cyan]↺ {e['tool']} eseguito dall'harness: {e['count']} valori ammessi per {e['field']}[/cyan]"
            )
        elif kind == "tool_args_adjusted":
            console.print(f"  [cyan]↺ {e['tool']}: {'; '.join(e['adjustments'])}[/cyan]")
        elif kind == "tool_ungrounded":
            console.print(
                f"  [yellow]✗ {e['tool']} bloccato: id non verificati ({', '.join(e['fields'])})[/yellow]"
            )
        elif kind == "confirmation":
            esito = (
                "[green]operazione confermata[/green]" if e["approved"] else "[red]operazione negata[/red]"
            )
            console.print(f"  {esito}")
        elif kind == "tool_result":
            if e["ok"]:
                console.print(f"  [green]← {e['tool']} ok[/green] [dim]({e['duration_ms']} ms)[/dim]")
                if verbose:
                    console.print(f"    [dim]{_short(e['result'], 600)}[/dim]")
            else:
                console.print(f"  [red]← {e['tool']} errore {e['error_code']}[/red]: {_short(e['result'])}")
        elif kind == "loop_detected":
            console.print(f"  [red]✗ ripetizione rilevata su {e['tool']}[/red]")
        elif kind == "llm_error":
            console.print(f"  [red]✗ errore del modello: {_short(e['error'], 300)}[/red]")
        elif kind == "stop":
            tin = e["tokens_in"] if e["tokens_in"] is not None else "n/d"
            tout = e["tokens_out"] if e["tokens_out"] is not None else "n/d"
            console.print(
                f"[dim]■ arresto: {e['reason']} · iterazioni {e['iterations']} · tool {e['tool_calls']} · "
                f"token {tin}/{tout} · {e['duration_ms']} ms[/dim]"
            )

    return render


# Recuperiamo i nomi leggibili degli id da confermare (servizio, team, richiesta): nel pannello l'operatore
# vede "payments-api (prod)" e non soltanto "1".
async def describe_ids(session: AgentSession, args: dict[str, Any]) -> dict[str, str]:
    """Nome leggibile degli identificativi, per far riconoscere all'operatore un id sbagliato.

    Usa i tool di lettura direttamente (non passa dal modello e non entra nel log dell'agente).
    """
    labels: dict[str, str] = {}
    lookups = {
        "service_id": ("list_services", {}, lambda i: f"{i['name']} ({i['environment']})"),
        "team_id": ("list_teams", {}, lambda i: i["name"]),
    }
    for field, (tool, tool_args, fmt) in lookups.items():
        if isinstance(args.get(field), int):
            outcome = await session.executor.call_tool(tool, tool_args)
            items = json.loads(outcome.content).get("items", []) if outcome.ok else []
            match = next((i for i in items if i["id"] == args[field]), None)
            labels[field] = fmt(match) if match else "[red]inesistente[/red]"
    if isinstance(args.get("request_id"), int):
        outcome = await session.executor.call_tool("get_request", {"request_id": args["request_id"]})
        labels["request_id"] = (
            json.loads(outcome.content)["title"] if outcome.ok else "[red]inesistente[/red]"
        )
    return labels


# Prepara la funzione di conferma che il ciclo chiama prima di ogni scrittura.
def make_confirm(session: AgentSession):
    # Mostriamo il pannello con i parametri esatti e l'effetto previsto, poi insistiamo con s/n finché
    # l'utente non risponde. True solo se conferma.
    async def confirm(tool: str, args: dict[str, Any]) -> bool:
        labels = await describe_ids(session, args)
        table = Table(show_header=False, box=None, padding=(0, 1))
        for key, value in args.items():
            label = f"  [cyan]→ {labels[key]}[/cyan]" if key in labels else ""
            table.add_row(f"[bold]{key}[/bold]", _short(value, 300) + label)
        console.print(
            Panel(
                table,
                title=f"[bold yellow]Conferma richiesta · {tool}[/bold yellow]",
                subtitle=Policy.describe_effect(tool, args),
                border_style="yellow",
            )
        )
        while True:
            answer = (await asyncio.to_thread(console.input, "Confermi l'operazione? [s/n] ")).strip().lower()
            if answer in {"s", "si", "sì", "y", "yes"}:
                return True
            if answer in {"n", "no"}:
                return False

    return confirm


# La risposta finale dell'agente, stampata dentro un riquadro.
def print_answer(text: str) -> None:
    console.print(Panel(Markdown(text or "_(nessun testo)_"), title="Agente", border_style="blue"))


# --------------------------------------------------------------------------- #
# Comandi
# --------------------------------------------------------------------------- #


# init-db: crea il database e lo riempie con i dati sintetici.
def cmd_init_db(cfg: Config, args: argparse.Namespace) -> int:
    path = init_db(args.db or cfg.paths.db, seed=args.seed)
    console.print(f"Database creato in [bold]{path}[/bold] (seed {args.seed}).")
    return 0


# tools: mostra gli strumenti del server e quali vede il ruolo corrente.
async def cmd_tools(cfg: Config, args: argparse.Namespace) -> int:
    async with AgentSession(cfg, db_path=args.db) as session:
        remote = await session.executor.list_tools()
    policy = Policy(cfg.policy, cfg.agent.role)
    table = Table(title=f"Tool del server MCP · ruolo {cfg.agent.role}")
    for col in ("Tool", "Classe", "Visibile", "Parametri"):
        table.add_column(col)
    for tool in remote:
        kind = policy.classify(tool.name) or "non classificato"
        visible = "sì" if kind != "non classificato" and tool.name in policy.allowed else "no"
        table.add_row(tool.name, kind, visible, ", ".join(tool.input_schema.get("properties", {})))
    console.print(table)
    return 0


# ask: una sola domanda all'agente, con i passi e la risposta a video.
async def cmd_ask(cfg: Config, args: argparse.Namespace) -> int:
    async with AgentSession(cfg, db_path=args.db) as session:
        logger = session.new_logger()
        logger.listeners.append(make_renderer(args.verbose))
        result = await session.ask(args.request, confirm=make_confirm(session), logger=logger)
    print_answer(result.answer)
    console.print(f"[dim]log: {logger.path}[/dim]")
    return 0 if result.stop_reason == "final_answer" else 2


# chat: conversazione con l'agente. Dopo ogni risposta prepariamo la cronologia del turno successivo; /reset
# la azzera, /esci chiude.
async def cmd_chat(cfg: Config, args: argparse.Namespace) -> int:
    async with AgentSession(cfg, db_path=args.db) as session:
        console.print(
            Panel(
                f"Modello: [bold]{session.provider.name}/{session.provider.model}[/bold] · "
                f"ruolo: [bold]{cfg.agent.role}[/bold] · tool visibili: {len(session.tools)}\n"
                "Comandi: /reset azzera la conversazione, /esci termina.",
                title="Assistente Cloud/DevOps",
                border_style="blue",
            )
        )
        unclassified = [n for n, why in session.excluded.items() if why.startswith("non classificato")]
        if unclassified:
            console.print(
                f"[yellow]Attenzione: il server espone tool non classificati in config.toml, quindi nascosti "
                f"al modello: {', '.join(unclassified)}. Aggiorna la sezione [policy] da "
                f"config.example.toml.[/yellow]"
            )
        history: list[dict[str, Any]] = []
        while True:
            try:
                request = (await asyncio.to_thread(console.input, "[bold]> [/bold]")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not request:
                continue
            if request.lower() in {"/esci", "/exit", "/quit", "esci", "exit", "quit"}:
                break
            if request == "/reset":
                history = []
                console.print("[dim]conversazione azzerata[/dim]")
                continue
            logger = session.new_logger()
            logger.listeners.append(make_renderer(args.verbose))
            result = await session.ask(request, confirm=make_confirm(session), logger=logger, history=history)
            print_answer(result.answer)
            history = (
                history_for_next_turn(result.messages, cfg.agent.history_turns)
                if result.stop_reason == "final_answer"
                else []
            )
    return 0


# scenarios: lancia gli scenari di test e scrive report e risultati.
async def cmd_scenarios(cfg: Config, args: argparse.Namespace) -> int:
    from devops_agent.evaluation.runner import run_scenarios

    summary = await run_scenarios(
        cfg,
        scenario_dir=Path(args.dir),
        only=args.only,
        repeat=args.repeat,
        report_path=Path(args.report) if args.report else None,
        console=console,
    )
    return 0 if summary.all_passed else 1


# tokens: a partire da results.json stampa i token consumati per scenario.
def cmd_tokens(cfg: Config, args: argparse.Namespace) -> int:
    from devops_agent.evaluation.tokens import token_report

    console.print(
        Markdown(
            token_report(
                Path(args.results),
                price_in=args.price_in,
                price_out=args.price_out,
                daily_requests=args.daily,
            )
        )
    )
    return 0


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


# Qui definiamo comandi e opzioni della riga di comando.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devops-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", help="file di configurazione TOML (default: config.toml)")
    parser.add_argument("--provider", choices=["ollama", "openai_compat", "scripted"])
    parser.add_argument("--model", help="nome del modello")
    parser.add_argument("--base-url", help="URL del servizio del modello")
    parser.add_argument("--role", help="ruolo dell'utente (es. operator, viewer)")
    parser.add_argument("--db", help="percorso del database SQLite")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="crea il database con dati sintetici")
    p.add_argument("--seed", type=int, default=42)

    sub.add_parser("tools", help="elenca i tool del server MCP")

    p = sub.add_parser("ask", help="esegue una singola richiesta")
    p.add_argument("request")
    p.add_argument("-v", "--verbose", action="store_true", help="mostra i risultati dei tool")

    p = sub.add_parser("chat", help="conversazione interattiva")
    p.add_argument("-v", "--verbose", action="store_true")

    p = sub.add_parser("scenarios", help="esegue gli scenari di test")
    p.add_argument("--dir", default="tests/scenarios")
    p.add_argument("--only", nargs="*", help="ID degli scenari da eseguire, es. TST-05")
    p.add_argument("--repeat", type=int, default=1, help="ripetizioni per scenario")
    p.add_argument("--report", help="file Markdown del report (default: nella cartella dei risultati)")

    p = sub.add_parser("tokens", help="analisi di token e costi")
    p.add_argument("results", help="results.json prodotto da 'scenarios'")
    p.add_argument("--price-in", type=float, help="prezzo in USD per milione di token di input")
    p.add_argument("--price-out", type=float, help="prezzo in USD per milione di token di output")
    p.add_argument("--daily", type=int, default=100, help="richieste al giorno per la proiezione")
    return parser


# Le opzioni passate da terminale hanno la precedenza: le sovrapponiamo alla configurazione del file.
def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    model = cfg.model
    if args.provider:
        model = replace(model, provider=args.provider)
    if args.model:
        model = replace(model, model=args.model)
    if args.base_url:
        model = replace(model, base_url=args.base_url)
    agent = replace(cfg.agent, role=args.role) if args.role else cfg.agent
    paths = replace(cfg.paths, db=args.db) if args.db else cfg.paths
    return replace(cfg, model=model, agent=agent, paths=paths)


# Punto d'ingresso del programma: leggiamo le opzioni, carichiamo la configurazione e avviamo il comando
# scelto. Gli errori di configurazione li stampiamo in chiaro, senza stack trace.
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = apply_overrides(load_config(args.config), args)
        handlers = {
            "init-db": cmd_init_db,
            "tools": cmd_tools,
            "ask": cmd_ask,
            "chat": cmd_chat,
            "scenarios": cmd_scenarios,
            "tokens": cmd_tokens,
        }
        result = handlers[args.command](cfg, args)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[red]Errore:[/red] {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
