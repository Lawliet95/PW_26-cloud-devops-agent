"""Definizione e valutazione degli scenari di test (file TOML in tests/scenarios)."""

from __future__ import annotations

import sqlite3
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devops_agent.agent.types import RunResult


# Un controllo sul database: la query da lanciare e il valore (o la variazione) che ci aspettiamo.
@dataclass
class DbCheck:
    sql: str
    equals: Any = None
    delta: int | None = None


# Tutto ciò che uno scenario si aspetta: come si ferma l'agente, quali strumenti usa, quante conferme, cosa
# deve o non deve comparire in risposta e nel log, cosa cambia nel database.
@dataclass
class Expectation:
    stop_reason: list[str]
    tools_called: list[str] = field(default_factory=list)
    tools_not_called: list[str] = field(default_factory=list)
    tool_failed: list[str] = field(default_factory=list)
    max_tool_calls: int | None = None
    confirmations: int | None = None
    answer_any: list[str] = field(default_factory=list)  # almeno uno dei frammenti
    answer_all: list[str] = field(default_factory=list)  # tutti i frammenti
    answer_none: list[str] = field(default_factory=list)  # nessuno dei frammenti
    events_present: list[str] = field(default_factory=list)  # eventi che devono comparire nel log
    # Eventi verificati solo con il provider scripted: il percorso di correzione che lo script
    # riproduce. Con un modello reale conta l'esito (DB, conferme), non la strada per arrivarci.
    events_present_scripted: list[str] = field(default_factory=list)
    db: list[DbCheck] = field(default_factory=list)


# Uno scenario, così come lo leggiamo dal file TOML.
@dataclass
class Scenario:
    id: str
    title: str
    category: str
    request: str
    expected: str  # esito atteso in linguaggio naturale, per il report
    expect: Expectation
    role: str = "operator"
    confirmations: list[bool] = field(default_factory=list)
    fault_tools: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    script: list[dict[str, Any]] = field(default_factory=list)
    # Provider per cui lo scenario ha senso (vuoto = tutti). Uno scenario di regressione che
    # riproduce una risposta registrata vale solo con il provider scripted.
    providers: list[str] = field(default_factory=list)
    # Richieste dei turni precedenti di una chat, eseguite nella stessa sessione prima di `request`.
    # Le aspettative valgono solo per l'ultimo turno; lo script copre tutti i turni, in ordine.
    history: list[str] = field(default_factory=list)
    path: Path | None = None


# Da file TOML a Scenario.
def load_scenario(path: Path) -> Scenario:
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    exp = dict(raw.pop("expect"))
    stop = exp.pop("stop_reason", "final_answer")
    db = [DbCheck(**c) for c in exp.pop("db", [])]
    expectation = Expectation(stop_reason=[stop] if isinstance(stop, str) else list(stop), db=db, **exp)
    return Scenario(expect=expectation, path=path, **raw)


# Carichiamo tutti gli scenari della cartella, controlliamo che gli ID non si ripetano e, se ci viene chiesto,
# filtriamo quelli scelti.
def load_scenarios(directory: Path, only: list[str] | None = None) -> list[Scenario]:
    scenarios = [load_scenario(p) for p in sorted(directory.glob("*.toml"))]
    ids = [s.id for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("ID di scenario duplicati")
    if only:
        missing = set(only) - set(ids)
        if missing:
            raise ValueError(f"scenari non trovati: {', '.join(sorted(missing))}")
        scenarios = [s for s in scenarios if s.id in only]
    return scenarios


# Lanciamo le query di controllo sul database e ne raccogliamo i valori.
def snapshot(db_path: Path, checks: list[DbCheck]) -> list[Any]:
    conn = sqlite3.connect(db_path)
    try:
        return [conn.execute(c.sql).fetchone()[0] for c in checks]
    finally:
        conn.close()


# Mettiamo a confronto risultato e aspettative, ottenendo l'elenco di quelle non rispettate: lista vuota
# significa scenario superato.
def evaluate(
    scenario: Scenario,
    result: RunResult,
    events: list[dict[str, Any]],
    db_before: list[Any],
    db_after: list[Any],
    provider: str | None = None,
) -> list[str]:
    """Restituisce l'elenco delle aspettative non rispettate (vuoto = PASS)."""
    exp = scenario.expect
    failures: list[str] = []
    executed = [c.name for c in result.executed]
    confirmed_tools = [e["tool"] for e in events if e["event"] == "confirmation"]

    if result.stop_reason not in exp.stop_reason:
        failures.append(f"arresto '{result.stop_reason}', atteso {' o '.join(exp.stop_reason)}")
    for name in exp.tools_called:
        if name not in executed:
            failures.append(f"tool atteso non eseguito: {name}")
    # "Non eseguito" = mai arrivato al server. I tentativi bloccati in conferma
    # si verificano con il numero di conferme atteso.
    for name in exp.tools_not_called:
        if name in executed:
            failures.append(f"tool che non doveva essere eseguito: {name}")
    for name in exp.tool_failed:
        if not any(c.name == name and not c.ok for c in result.executed):
            failures.append(f"atteso un errore gestito su {name}")
    if exp.max_tool_calls is not None and len(executed) > exp.max_tool_calls:
        failures.append(f"{len(executed)} chiamate a tool, massimo atteso {exp.max_tool_calls}")
    expected_confirmations = (
        exp.confirmations if exp.confirmations is not None else len(scenario.confirmations)
    )
    if len(confirmed_tools) != expected_confirmations:
        failures.append(f"{len(confirmed_tools)} richieste di conferma, attese {expected_confirmations}")
    logged = {e["event"] for e in events}
    for kind in exp.events_present:
        if kind not in logged:
            failures.append(f"evento atteso nel log non presente: {kind}")
    if provider == "scripted":
        for kind in exp.events_present_scripted:
            if kind not in logged:
                failures.append(f"evento atteso nel log (scripted) non presente: {kind}")
    if exp.answer_any:
        answer = result.answer.lower()
        if not any(fragment.lower() in answer for fragment in exp.answer_any):
            failures.append(f"la risposta non contiene nessuno di: {', '.join(exp.answer_any)}")
    answer = result.answer.lower()
    missing = [f for f in exp.answer_all if f.lower() not in answer]
    if missing:
        failures.append(f"la risposta non contiene: {', '.join(missing)}")
    forbidden = [f for f in exp.answer_none if f.lower() in answer]
    if forbidden:
        failures.append(f"la risposta contiene frammenti non ammessi: {', '.join(forbidden)}")
    for check, before, after in zip(exp.db, db_before, db_after, strict=True):
        if check.delta is not None and (after - before) != check.delta:
            failures.append(f"DB: variazione {after - before}, attesa {check.delta} · {check.sql}")
        if check.equals is not None and after != check.equals:
            failures.append(f"DB: valore {after!r}, atteso {check.equals!r} · {check.sql}")
    return failures
