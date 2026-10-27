"""Esecuzione degli scenari di test e generazione del report.

Ogni esecuzione parte da un database nuovo (stesso seed), avvia un server MCP
dedicato e salva il log JSONL completo. Il report Markdown riporta per ogni
scenario richiesta, esito atteso, esito ottenuto e percorso del log: sono le
evidenze richieste dalla consegna, da accompagnare con gli screenshot della CLI.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from devops_agent.agent.session import AgentSession, history_for_next_turn
from devops_agent.agent.types import Message
from devops_agent.backend.database import init_db
from devops_agent.config import Config
from devops_agent.evaluation.scenarios import Scenario, evaluate, load_scenarios, snapshot


# Esito di una singola esecuzione di uno scenario.
@dataclass
class RunRecord:
    scenario_id: str
    repetition: int
    passed: bool
    failures: list[str]
    stop_reason: str
    iterations: int
    llm_calls: int
    tool_calls: list[str]
    tokens_in: int | None
    tokens_out: int | None
    duration_ms: int
    answer: str
    log: str


# Tutti i risultati di una sessione di test, raccolti insieme.
@dataclass
class Summary:
    provider: str
    model: str
    started_at: str
    repeat: int
    results_dir: str
    records: list[RunRecord] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # scenari non pertinenti per il provider

    # True se ogni esecuzione è stata superata.
    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.records)


# Eseguiamo uno scenario una volta: database nuovo, eventuali turni precedenti della chat, poi la richiesta
# vera, e infine il confronto con l'esito atteso.
async def run_one(cfg: Config, scenario: Scenario, repetition: int, out_dir: Path) -> RunRecord:
    db_path = init_db(out_dir / "db" / f"{scenario.id}-{repetition}.db", seed=42)
    run_cfg = replace(cfg, agent=replace(cfg.agent, role=scenario.role))
    limits = replace(cfg.limits, **scenario.limits)
    pending = list(scenario.confirmations)

    # Alle conferme rispondiamo con quanto previsto dallo scenario.
    async def scripted_confirm(tool: str, args: dict[str, Any]) -> bool:
        # Le risposte dell'utente sono predefinite; una conferma non prevista viene negata.
        return pending.pop(0) if pending else False

    before = snapshot(db_path, scenario.expect.db)
    session = AgentSession(
        run_cfg, db_path=db_path, fault_tools=scenario.fault_tools, limits=limits, script=scenario.script
    )
    async with session:
        history: list[Message] = []
        for turn, previous in enumerate(scenario.history, 1):
            turn_logger = session.new_logger(
                out_dir / "logs", scenario=f"{scenario.id}-turno{turn}", repetition=repetition
            )
            earlier = await session.ask(
                previous, confirm=scripted_confirm, logger=turn_logger, history=history
            )
            history = history_for_next_turn(earlier.messages, cfg.agent.history_turns)
        logger = session.new_logger(out_dir / "logs", scenario=scenario.id, repetition=repetition)
        result = await session.ask(scenario.request, confirm=scripted_confirm, logger=logger, history=history)
    after = snapshot(db_path, scenario.expect.db)
    failures = evaluate(scenario, result, logger.events, before, after, cfg.model.provider)

    return RunRecord(
        scenario_id=scenario.id,
        repetition=repetition,
        passed=not failures,
        failures=failures,
        stop_reason=result.stop_reason,
        iterations=result.iterations,
        llm_calls=result.llm_calls,
        tool_calls=[f"{c.name}{'' if c.ok else ' ✗'}" for c in result.executed],
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        duration_ms=result.duration_ms,
        answer=result.answer,
        log=str(logger.path),
    )


# Giriamo tutti gli scenari (o solo quelli scelti) per il numero di ripetizioni richiesto, saltiamo quelli che
# non hanno senso con il provider attuale e salviamo results.json e report.md.
async def run_scenarios(
    cfg: Config,
    *,
    scenario_dir: Path,
    only: list[str] | None = None,
    repeat: int = 1,
    report_path: Path | None = None,
    results_root: Path | None = None,
    console: Console | None = None,
) -> Summary:
    console = console or Console(quiet=True)
    scenarios = load_scenarios(scenario_dir, only)
    skipped = [s.id for s in scenarios if s.providers and cfg.model.provider not in s.providers]
    scenarios = [s for s in scenarios if s.id not in skipped]
    if skipped:
        console.print(f"Scenari esclusi per il provider {cfg.model.provider}: {', '.join(skipped)}")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (results_root or Path(cfg.paths.logs) / "scenarios") / f"{stamp}-{cfg.model.provider}"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = Summary(
        cfg.model.provider,
        cfg.model.model if cfg.model.provider != "scripted" else "script",
        stamp,
        repeat,
        str(out_dir),
        skipped=skipped,
    )

    for scenario in scenarios:
        for rep in range(1, repeat + 1):
            record = await run_one(cfg, scenario, rep, out_dir)
            summary.records.append(record)
            mark = "[green]PASS[/green]" if record.passed else "[red]FAIL[/red]"
            console.print(f"{mark} {scenario.id} #{rep} {scenario.title} · {record.stop_reason}")
            for failure in record.failures:
                console.print(f"     [red]- {failure}[/red]")

    (out_dir / "results.json").write_text(
        json.dumps(asdict(summary), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = render_report(summary, scenarios)
    target = report_path or out_dir / "report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    passed = sum(r.passed for r in summary.records)
    console.print(
        f"\n{passed}/{len(summary.records)} esecuzioni superate · risultati in {out_dir} · report in {target}"
    )
    return summary


# Media che ignora i valori mancanti; se non ne resta nessuno scriviamo n/d.
def _mean(values: list[int | None]) -> str:
    known = [v for v in values if v is not None]
    return f"{statistics.mean(known):.0f}" if known else "n/d"


# Rendiamo il testo sicuro per una cella di tabella Markdown.
def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


# Il report in Markdown: una riga per scenario e, sotto, il dettaglio dei fallimenti.
def render_report(summary: Summary, scenarios: list[Scenario]) -> str:
    by_id: dict[str, list[RunRecord]] = {}
    for r in summary.records:
        by_id.setdefault(r.scenario_id, []).append(r)

    lines = [
        "# Report dei test funzionali",
        "",
        f"- Data esecuzione (UTC): {summary.started_at}",
        f"- Provider e modello: `{summary.provider}` / `{summary.model}`",
        f"- Ripetizioni per scenario: {summary.repeat}",
        f"- Esecuzioni superate: {sum(r.passed for r in summary.records)} su {len(summary.records)}",
        f"- Cartella dei risultati: `{summary.results_dir}`",
        "",
    ]
    if summary.skipped:
        lines += [
            f"> Scenari esclusi per il provider `{summary.provider}`: {', '.join(summary.skipped)}. Sono "
            "regressioni che riproducono risposte registrate e si eseguono con il provider *scripted*.",
            "",
        ]
    if summary.provider == "scripted":
        lines += [
            "> Esecuzione con provider *scripted*: verifica l'harness con risposte predefinite, "
            "non misura le capacità di un modello. Token non disponibili per costruzione.",
            "",
        ]
    lines += [
        "| ID | Scenario | Categoria | Esito | Arresto | Tool eseguiti | Token in (media) "
        "| Token out (media) | Durata media (ms) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sc in scenarios:
        runs = by_id.get(sc.id, [])
        ok = sum(r.passed for r in runs)
        stops = ", ".join(sorted({r.stop_reason for r in runs}))
        lines.append(
            f"| {sc.id} | {_cell(sc.title)} | {sc.category} | {'PASS' if ok == len(runs) else 'FAIL'} "
            f"{ok}/{len(runs)} | {stops} | {_cell(', '.join(runs[0].tool_calls) or '—')} | "
            f"{_mean([r.tokens_in for r in runs])} | {_mean([r.tokens_out for r in runs])} | "
            f"{_mean([r.duration_ms for r in runs])} |"
        )
    lines += ["", "## Dettaglio degli scenari", ""]
    for sc in scenarios:
        runs = by_id.get(sc.id, [])
        lines += [
            f"### {sc.id} · {sc.title}",
            "",
            f"- **Richiesta** (ruolo `{sc.role}`): {sc.request}",
            f"- **Esito atteso**: {sc.expected}",
        ]
        if sc.fault_tools:
            lines.append(f"- **Guasto simulato** su: {', '.join(sc.fault_tools)}")
        if sc.confirmations:
            lines.append(
                "- **Risposte alle conferme**: " + ", ".join("sì" if c else "no" for c in sc.confirmations)
            )
        for r in runs:
            lines += [
                f"- **Esito ottenuto** (esecuzione {r.repetition}): {'PASS' if r.passed else 'FAIL'} · "
                f"arresto `{r.stop_reason}` · iterazioni {r.iterations} · "
                f"tool: {', '.join(r.tool_calls) or 'nessuno'}",
                f"  - Risposta: {_cell(r.answer)[:600]}",
            ]
            lines += [f"  - Aspettativa non rispettata: {f}" for f in r.failures]
            lines.append(f"  - Log: `{r.log}`")
        lines.append("")
    return "\n".join(lines)
