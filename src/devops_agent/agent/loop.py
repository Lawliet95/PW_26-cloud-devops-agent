"""Ciclo agentico, scritto senza framework agentici.

Per ogni iterazione:
  1. invia al modello la conversazione e i tool visibili al ruolo;
  2. se la risposta non contiene chiamate a tool, è la risposta finale;
  3. altrimenti, per ogni chiamata: verifica che il tool esista, rifiuta parametri
     non previsti e valida gli argomenti contro lo schema, rileva le ripetizioni;
     per le scritture ammette una sola scrittura per turno, verifica che gli
     identificativi provengano dall'utente, da risultati dei tool o coincidano con
     l'unico servizio/team nominato nella richiesta, e chiede conferma; esegue con timeout e restituisce il risultato (o l'errore) al
     modello, legato al tool_call_id della chiamata.

Condizioni di arresto (stop_reason):
  final_answer        il modello ha risposto senza chiedere tool
  empty_response      il modello non ha prodotto né testo né chiamate
  max_iterations      raggiunto il numero massimo di iterazioni
  token_budget        superato il budget di token della richiesta
  invalid_arguments   troppi argomenti non validi nella stessa richiesta
  loop_detected       stessa chiamata con gli stessi argomenti ripetuta
  provider_error      modello non raggiungibile o risposta inutilizzabile
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import jsonschema

from devops_agent.agent.logger import RunLogger
from devops_agent.agent.matching import (
    PRIORITY_LABELS,
    describe,
    match_candidates,
    mentioned_priority,
    mentioned_priority_levels,
)
from devops_agent.agent.providers.base import LLMProvider
from devops_agent.agent.types import (
    ExecutedCall,
    Limits,
    Message,
    ProviderError,
    RunResult,
    ToolCall,
    ToolOutcome,
    ToolSpec,
)

ConfirmFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


# Al ciclo basta sapere che esiste un metodo call_tool, niente di più. In questo modo non dipendiamo dal
# client MCP e nei test possiamo passare un esecutore finto.
class ToolExecutor(Protocol):
    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome: ...


# Eccezione interna che ci serve per uscire dal ciclo quando il modello insiste con la stessa chiamata.
class _LoopDetected(Exception):
    pass


# Lo stato di lavoro di una richiesta mentre il ciclo gira: iterazioni, token, chiamate già viste, scritture
# fatte, suggerimenti da riproporre ed elenchi da allegare alla risposta.
@dataclass
class _State:
    iteration: int = 0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_reported: bool = False
    invalid_args: int = 0
    seen_calls: dict[str, int] = field(default_factory=dict)
    denied_calls: set[str] = field(default_factory=set)
    executed: list[ExecutedCall] = field(default_factory=list)
    # Correzione suggerita dopo un ID_NON_VERIFICATO con un'unica corrispondenza nella richiesta.
    hint: dict[str, Any] | None = None
    nudged: bool = False
    stale_nudged: bool = False
    dropped_filters: list[str] = field(
        default_factory=list
    )  # filtri proposti dal modello e tolti  # risposta con dati senza alcuna lettura in questa richiesta
    # Scritture proposte dal modello e scritture eseguite con esito positivo.
    write_proposed: bool = False
    write_succeeded: bool = False
    # Elenchi lunghi generati dall'harness, da allegare alla risposta finale.
    attachments: list[str] = field(default_factory=list)


# Eccoci al cuore dell'agente, il ciclo agentico vero e proprio.
# Mandiamo la conversazione al modello; se chiede degli strumenti li controlliamo, li eseguiamo, gli
# restituiamo i risultati e si riparte. Ci fermiamo quando risponde senza chiedere strumenti oppure quando
# scatta una condizione di arresto (iterazioni, token, errori ripetuti, ripetizioni).
# Alla fine otteniamo un RunResult con risposta, motivo dell'arresto e metriche.
async def run_agent(
    user_request: str,
    *,
    llm: LLMProvider,
    tools: list[ToolSpec],
    executor: ToolExecutor,
    confirm: ConfirmFn,
    logger: RunLogger,
    system_prompt: str,
    limits: Limits | None = None,
    history: list[Message] | None = None,
) -> RunResult:
    limits = limits or Limits()
    catalog = {t.name: t for t in tools}
    messages: list[Message] = [
        {"role": "system", "content": system_prompt},
        *(history or []),
        {"role": "user", "content": user_request},
    ]
    st = _State()
    started = time.perf_counter()
    logged_upto = 0  # indice del primo messaggio non ancora registrato nel log

    logger.event(
        "run_start",
        request=user_request,
        provider=llm.name,
        model=llm.model,
        tools=[{"name": t.name, "write": t.is_write} for t in tools],
        history_messages=len(history or []),
        limits=limits.__dict__,
    )

    # Tutte le uscite dal ciclo passano da qui: scriviamo nel log perché ci siamo fermati, con iterazioni,
    # token e durata, e confezioniamo il risultato.
    def stop(reason: str, answer: str) -> RunResult:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.event(
            "stop",
            reason=reason,
            iterations=st.iteration,
            llm_calls=st.llm_calls,
            tokens_in=st.tokens_in if st.tokens_reported else None,
            tokens_out=st.tokens_out if st.tokens_reported else None,
            tool_calls=len(st.executed),
            duration_ms=duration_ms,
            answer=answer,
        )
        return RunResult(
            answer=answer,
            stop_reason=reason,
            iterations=st.iteration,
            llm_calls=st.llm_calls,
            tokens_in=st.tokens_in if st.tokens_reported else None,
            tokens_out=st.tokens_out if st.tokens_reported else None,
            duration_ms=duration_ms,
            executed=st.executed,
            messages=messages[1:],
        )

    while st.iteration < limits.max_iterations:
        st.iteration += 1
        logger.event(
            "llm_request",
            iteration=st.iteration,
            total_messages=len(messages),
            new_messages=[_loggable(m) for m in messages[logged_upto:]],
        )
        logged_upto = len(messages)

        t0 = time.perf_counter()
        try:
            resp = await llm.generate(messages, tools)
        except ProviderError as exc:
            logger.event("llm_error", iteration=st.iteration, error=str(exc))
            return stop("provider_error", "Il modello non è al momento raggiungibile. Riprova più tardi.")
        st.llm_calls += 1
        if resp.tokens_in is not None or resp.tokens_out is not None:
            st.tokens_reported = True
            st.tokens_in += resp.tokens_in or 0
            st.tokens_out += resp.tokens_out or 0
        logger.event(
            "llm_response",
            iteration=st.iteration,
            text=resp.text,
            tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments} for c in resp.tool_calls],
            tokens_in=resp.tokens_in,
            tokens_out=resp.tokens_out,
            finish_reason=resp.finish_reason,
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

        if not resp.tool_calls:
            # Alcuni modelli piccoli scrivono la chiamata come testo JSON invece di usare il campo
            # tool_calls. Se l'intero testo è una chiamata a un tool del catalogo, la si tratta come
            # tale: passa comunque da validazione, verifica degli id e conferma.
            parsed = _text_tool_calls(resp.text, catalog, st.iteration)
            if parsed:
                logger.event(
                    "text_tool_call_parsed",
                    iteration=st.iteration,
                    tool_calls=[{"id": c.id, "name": c.name, "arguments": c.arguments} for c in parsed],
                )
                resp.tool_calls = parsed
                resp.text = None

        # Il messaggio dell'assistente precede sempre i risultati dei tool che ha chiesto.
        messages.append({"role": "assistant", "content": resp.text, "tool_calls": resp.tool_calls})
        logged_upto = len(messages)

        if not resp.tool_calls:
            if st.hint and not st.nudged:
                # Il modello ha chiuso il turno a testo dopo un errore correggibile in modo univoco:
                # una sola volta per esecuzione gli si ripropone la correzione. La scrittura che ne
                # segue passa comunque da verifica degli id e conferma dell'utente.
                st.nudged = True
                logger.event("harness_nudge", iteration=st.iteration, hint=st.hint)
                messages.append({"role": "system", "content": _nudge_message(st.hint)})
                continue
            if not (resp.text or "").strip():
                return stop("empty_response", "Il modello non ha prodotto una risposta.")
            if not st.executed and len(_cited_ids(resp.text or "")) >= 2:
                # La risposta riporta dati (identificativi di richieste) ma in questa richiesta non è stato
                # letto nulla: in chat il modello ricopia risposte precedenti, che possono essere superate
                # o parziali. Una volta gli si chiede di rileggere; se insiste, lo dice il sistema.
                if not st.stale_nudged:
                    st.stale_nudged = True
                    logger.event("stale_data_nudge", iteration=st.iteration)
                    messages.append({"role": "system", "content": STALE_DATA_NUDGE})
                    continue
                logger.event("answer_annotated", iteration=st.iteration, note=STALE_DATA_NOTE)
                resp.text = f"{(resp.text or '').rstrip()}\n\n{STALE_DATA_NOTE}"
            answer = _annotate_unexecuted_writes(resp.text or "", st, logger)
            if st.dropped_filters:
                # Il modello descrive il risultato con il filtro che aveva chiesto, anche se l'harness lo ha
                # tolto (sessione del 26/10: «10 di 31 richieste con priorità critica»): lo dice il sistema.
                values = ", ".join(sorted({n.split(" ", 1)[0] for n in st.dropped_filters}))
                note = (
                    f"Nota del sistema: il filtro {values}, proposto dall'assistente ma non chiesto, è stato "
                    "tolto dalla ricerca: i risultati comprendono tutte le priorità."
                )
                logger.event("answer_annotated", iteration=st.iteration, note=note)
                answer = f"{answer.rstrip()}\n\n{note}"
            if st.attachments:
                answer = answer.rstrip() + "\n\n" + "\n\n".join(st.attachments)
            return stop("final_answer", answer)

        if st.tokens_reported and st.tokens_in + st.tokens_out > limits.max_total_tokens:
            return stop("token_budget", "Richiesta interrotta: superato il budget di token previsto.")

        writes_this_turn = 0
        for index, call in enumerate(resp.tool_calls):
            is_write = call.name in catalog and catalog[call.name].is_write
            st.write_proposed = st.write_proposed or is_write
            if index >= limits.max_tool_calls_per_turn:
                content = _error(
                    "TROPPE_CHIAMATE", f"al massimo {limits.max_tool_calls_per_turn} chiamate per turno"
                )
            elif is_write and writes_this_turn >= 1:
                # Le scritture vanno decise una alla volta, dopo aver visto l'esito della precedente:
                # una seconda scrittura pianificata nello stesso turno non tiene conto di quell'esito.
                logger.event(
                    "tool_rejected", iteration=st.iteration, tool=call.name, reason="scrittura_multipla"
                )
                content = _error(
                    "UNA_SCRITTURA_PER_TURNO",
                    "nello stesso turno è già stata proposta un'operazione di scrittura: leggi prima il suo "
                    "esito e proponi questa solo se è ancora necessaria",
                )
            else:
                writes_this_turn += int(is_write)
                try:
                    content = await _handle_call(
                        call, st, catalog, executor, confirm, logger, limits, messages
                    )
                except _LoopDetected:
                    return stop(
                        "loop_detected", "Richiesta interrotta: l'agente ripeteva la stessa operazione."
                    )
            messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content})

        if st.invalid_args >= limits.max_invalid_args:
            return stop("invalid_arguments", "Richiesta interrotta: parametri non validi ripetuti.")

    return stop("max_iterations", "Richiesta interrotta: raggiunto il limite di iterazioni.")


# Qui gestiamo una singola chiamata a uno strumento chiesta dal modello.
# I controlli seguono un ordine preciso: lo strumento esiste, i parametri sono validi, non si tratta di una
# ripetizione; per le scritture verifichiamo anche id e priorità e chiediamo conferma all'utente. Solo a quel
# punto eseguiamo.
# Al modello torna un testo: il risultato oppure un errore che gli spiega cosa correggere.
async def _handle_call(
    call: ToolCall,
    st: _State,
    catalog: dict[str, ToolSpec],
    executor: ToolExecutor,
    confirm: ConfirmFn,
    logger: RunLogger,
    limits: Limits,
    messages: list[Message],
) -> str:
    # 1. Tool inesistente o non visibile al ruolo: nessuna chiamata al server.
    tool = catalog.get(call.name)
    if tool is None:
        logger.event("tool_rejected", iteration=st.iteration, tool=call.name, reason="non_disponibile")
        return _error(
            "TOOL_NON_DISPONIBILE",
            f"lo strumento '{call.name}' non è disponibile. Disponibili: {', '.join(catalog)}",
        )

    # 2. Parsing e validazione degli argomenti contro lo schema dichiarato dal server.
    adjustments: list[str] = []
    try:
        args = _parse_arguments(call.arguments)
        _reject_unknown_parameters(args, tool.input_schema)
        if not tool.is_write:
            # Solo per le letture: un valore numerico fuori dai limiti dello schema (es. limit=100
            # con massimo 50) viene ricondotto al limite invece di fallire. Le scritture restano
            # esatte: ciò che l'utente conferma è ciò che viene eseguito.
            args, adjustments = _clamp_to_bounds(args, tool.input_schema)
        jsonschema.validate(args, tool.input_schema)
    except (ValueError, jsonschema.ValidationError) as exc:
        st.invalid_args += 1
        message = exc.message if isinstance(exc, jsonschema.ValidationError) else str(exc)
        path = "/".join(str(p) for p in getattr(exc, "absolute_path", []))
        detail = f"{path}: {message}" if path else message
        logger.event(
            "tool_invalid_args",
            iteration=st.iteration,
            tool=call.name,
            arguments=call.arguments,
            error=detail,
        )
        return _error(
            "PARAMETRI_NON_VALIDI",
            f"{detail[:400]}. Correggi i parametri e richiama subito lo strumento: non chiedere "
            "all'utente di farlo.",
        )
    if not tool.is_write:
        args, id_notes = await _correct_read_ids(args, catalog, executor, logger, st, limits, messages)
        adjustments += id_notes
        args, filter_notes = _drop_unrequested_filters(args, messages)
        adjustments += filter_notes
        st.dropped_filters += [n for n in filter_notes if n.startswith("priority=")]
        args, limit_notes = _widen_unrequested_limit(args, tool.input_schema, messages)
        adjustments += limit_notes
    if adjustments:
        logger.event("tool_args_adjusted", iteration=st.iteration, tool=tool.name, adjustments=adjustments)

    # 3. Ripetizioni: stessa chiamata con gli stessi argomenti.
    key = hashlib.sha256(f"{tool.name}|{json.dumps(args, sort_keys=True)}".encode()).hexdigest()
    st.seen_calls[key] = st.seen_calls.get(key, 0) + 1
    if st.seen_calls[key] > limits.max_repeated_calls:
        logger.event("loop_detected", iteration=st.iteration, tool=tool.name, arguments=args)
        raise _LoopDetected

    # 4. Identificativi verificati: una scrittura può usare solo id che compaiono nella
    #    richiesta dell'utente o nei risultati dei tool, mai id inventati dal modello.
    if tool.is_write:
        if st.hint and st.hint["tool"] == tool.name:
            st.hint = None  # il modello ha ripreso la scrittura: il suggerimento è stato usato
        ungrounded = _ungrounded_ids(args, messages)
        if ungrounded:
            candidates = await _lookup_candidates(ungrounded, catalog, executor, logger, st, limits)
            matches = {
                f: _match_in_request(f, messages, candidates[f]) for f in ungrounded if f in candidates
            }
            # Un id è verificato anche se coincide con l'unico servizio/team che la richiesta nomina.
            verified = [
                f for f in ungrounded if len(matches.get(f, [])) == 1 and matches[f][0]["id"] == args[f]
            ]
            for f in verified:
                logger.event(
                    "id_verified_by_match",
                    iteration=st.iteration,
                    tool=tool.name,
                    field=f,
                    value=args[f],
                    match=describe(f, matches[f][0]),
                )
            ungrounded = [f for f in ungrounded if f not in verified]
        if ungrounded:
            st.invalid_args += 1
            logger.event(
                "tool_ungrounded", iteration=st.iteration, tool=tool.name, arguments=args, fields=ungrounded
            )
            unique = {f: matches[f][0] for f in ungrounded if len(matches.get(f, [])) == 1}
            shown = {
                f: (matches[f] if matches.get(f) else candidates[f]) for f in ungrounded if f in candidates
            }
            message = _ungrounded_message(tool.name, args, ungrounded, shown, unique)
            if len(unique) == len(ungrounded):
                st.hint = {
                    "tool": tool.name,
                    "fields": {
                        f: {"id": item["id"], "label": describe(f, item)} for f, item in unique.items()
                    },
                }
            if shown:
                return json.dumps(
                    {"error": "ID_NON_VERIFICATO", "message": message, "candidati": shown}, ensure_ascii=False
                )
            return _error("ID_NON_VERIFICATO", message)

        # Priorità coerente con la richiesta: se l'utente ne ha indicata una in modo esplicito,
        # la scrittura deve usare quella. L'harness non la cambia da sé (scritture esatte): blocca
        # e indica il valore, come per gli id.
        asked = mentioned_priority(_last_user_text(messages))
        if "priority" in args and asked and args["priority"] != asked:
            st.invalid_args += 1
            logger.event(
                "tool_arg_mismatch",
                iteration=st.iteration,
                tool=tool.name,
                field="priority",
                value=args["priority"],
                expected=asked,
            )
            st.hint = {
                "tool": tool.name,
                "reason": "per una priorità diversa da quella indicata dall'utente",
                "fields": {"priority": {"id": asked, "label": PRIORITY_LABELS[asked]}},
            }
            return _error(
                "PARAMETRO_INCOERENTE",
                f"Operazione NON eseguita: priority={args['priority']} non corrisponde alla priorità "
                f"indicata dall'utente ({PRIORITY_LABELS[asked]}, cioè priority={asked}). Richiama subito "
                f"{tool.name} con priority={asked} e gli altri parametri invariati: non chiedere conferma "
                "all'utente, la chiederà il sistema.",
            )

    # 5. Human-in-the-loop sui parametri esatti che verranno eseguiti.
    if tool.is_write:
        if key in st.denied_calls:
            logger.event("tool_rejected", iteration=st.iteration, tool=tool.name, reason="gia_rifiutata")
            return _error("OPERAZIONE_RIFIUTATA", "operazione già rifiutata dall'utente: non riproporla")
        approved = await confirm(tool.name, args)
        logger.event(
            "confirmation", iteration=st.iteration, tool=tool.name, arguments=args, approved=approved
        )
        if not approved:
            st.denied_calls.add(key)
            return _error(
                "OPERAZIONE_RIFIUTATA",
                "l'utente ha negato la conferma: l'operazione non è stata eseguita. "
                "Non riproporla e informa l'utente.",
            )

    # 6. Esecuzione con timeout; ogni errore diventa un risultato leggibile dal modello.
    outcome = await _execute(tool, args, executor, logger, st, limits)
    retried = False
    if not outcome.ok and not tool.is_write and outcome.error_code in TRANSIENT_ERRORS:
        # Le letture sono idempotenti: un guasto temporaneo si ritenta una volta, qui e non dal
        # modello. Le scritture no: un timeout non dice se l'operazione è avvenuta.
        logger.event("tool_retry", iteration=st.iteration, tool=tool.name, error_code=outcome.error_code)
        outcome = await _execute(tool, args, executor, logger, st, limits)
        retried = True
    st.executed.append(ExecutedCall(tool.name, args, outcome.ok, outcome.error_code))
    if tool.is_write and outcome.ok:
        st.write_succeeded = True

    if not outcome.ok:
        message = outcome.content
        if outcome.error_code in TRANSIENT_ERRORS:
            message += (
                ". L'harness ha già ripetuto la chiamata una volta, senza successo. " if retried else ". "
            ) + (
                "Non chiamare di nuovo lo strumento e non scrivere che riproverai: comunica all'utente "
                "che il servizio non è al momento disponibile e suggerisci di riprovare più tardi."
            )
        return _error(outcome.error_code or "TOOL_FALLITO", message)
    notes = ["parametri adattati ai limiti: " + "; ".join(adjustments)] if adjustments else []
    data = _as_json(outcome.content)
    table = _long_list_table(data)
    if table:
        st.attachments.append(table)
        note = _long_list_note(data)
        notes.append(note)
        logger.event("list_attached", iteration=st.iteration, tool=tool.name, note=note)
    if notes:
        return json.dumps({"nota_harness": " ".join(notes), "risultato": data}, ensure_ascii=False)
    return outcome.content


TRANSIENT_ERRORS = {"TOOL_FALLITO", "TIMEOUT"}


# È il punto in cui lo strumento viene eseguito davvero, attraverso il server MCP e con un timeout. Se
# qualcosa va storto (timeout, server caduto) evitiamo le eccezioni e restituiamo un esito di errore.
async def _execute(
    tool: ToolSpec,
    args: dict[str, Any],
    executor: ToolExecutor,
    logger: RunLogger,
    st: _State,
    limits: Limits,
) -> ToolOutcome:
    logger.event("tool_call", iteration=st.iteration, tool=tool.name, arguments=args)
    t0 = time.perf_counter()
    try:
        outcome = await asyncio.wait_for(executor.call_tool(tool.name, args), timeout=limits.tool_timeout_s)
    except TimeoutError:
        outcome = ToolOutcome(False, f"nessuna risposta entro {limits.tool_timeout_s:.0f} s", "TIMEOUT")
    except Exception as exc:  # trasporto MCP interrotto, server terminato, ecc.
        outcome = ToolOutcome(
            False, f"errore di comunicazione con il server: {type(exc).__name__}", "TOOL_FALLITO"
        )
    logger.event(
        "tool_result",
        iteration=st.iteration,
        tool=tool.name,
        ok=outcome.ok,
        error_code=outcome.error_code,
        result=outcome.content,
        duration_ms=int((time.perf_counter() - t0) * 1000),
    )
    return outcome


LONG_LIST = 10  # oltre questa soglia i modelli piccoli omettono o ripetono elementi
_COLUMNS = [
    ("id", "ID"),
    ("title", "Titolo"),
    ("status", "Stato"),
    ("priority", "Priorità"),
    ("service_name", "Servizio"),
    ("environment", "Ambiente"),
    ("team_name", "Team"),
]


# Il risultato è un elenco lungo, cioè con più di 10 elementi? Se sì ne ritorniamo gli elementi, altrimenti
# None.
def _long_items(data: Any) -> list[dict[str, Any]] | None:
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    items = [i for i in data["items"] if isinstance(i, dict)]
    return items if len(items) > LONG_LIST else None


# La tabella con l'elenco completo da allegare alla risposta la costruiamo noi a partire dai dati dello
# strumento: quando la ricopiava il modello, sbagliava.
def _long_list_table(data: Any) -> str | None:
    """Tabella Markdown generata dal risultato del tool, allegata alla risposta finale.

    Esecuzioni 4 e 5, TST-11: chiesto di ricopiare 30 richieste, il modello ne elencava 23, 26 o
    28, ripetendone alcune e sbagliando attributi, anche con l'istruzione di riportarle tutte.
    L'elenco lungo lo scrive quindi l'harness dai dati; il modello ne scrive solo la sintesi.
    """
    items = _long_items(data)
    if not items:
        return None
    cols = [(k, label) for k, label in _COLUMNS if any(k in i for i in items)]
    if not cols:
        return None
    rows = [
        "| " + " | ".join(label for _, label in cols) + " |",
        "|" + "---|" * len(cols),
    ]
    for item in items:
        cells = [str(item.get(k) if item.get(k) is not None else "—").replace("|", "/") for k, _ in cols]
        rows.append("| " + " | ".join(cells) + " |")
    count, total = len(items), data.get("total_matching", len(items))
    head = f"Elenco generato dal sistema dai dati dello strumento: {count} elementi"
    head += f" su {total} corrispondenti." if total > count else "."
    return head + "\n\n" + _summary(items) + "\n\n" + "\n".join(rows)


# Piccolo conteggio: per ogni valore di un campo, quanti elementi lo hanno (per esempio quanti open).
def _counts(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[str(i.get(key))] = out.get(str(i.get(key)), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# Riepilogo stato × priorità con i totali, ricavato sempre dai dati e mai dal modello.
def _summary(items: list[dict[str, Any]]) -> str:
    """Riepilogo numerico calcolato dall'harness.

    Esecuzione 6, TST-11: con i soli conteggi per stato e per priorità il modello ha scritto anche la
    ripartizione incrociata stato × priorità, inventandola (12 celle sbagliate su 16, con i totali di
    riga giusti). I numeri li scrive quindi l'harness; al modello resta una frase senza numeri.
    """
    if not items or not all("status" in i and "priority" in i for i in items):
        return ""
    statuses = list(_counts(items, "status"))
    priorities = [
        p for p in ("critical", "high", "medium", "low") if any(i.get("priority") == p for i in items)
    ]
    rows = [
        "| Stato | " + " | ".join(priorities) + " | Totale |",
        "|" + "---|" * (len(priorities) + 2),
    ]
    for st in statuses:
        cells = [sum(1 for i in items if i["status"] == st and i["priority"] == p) for p in priorities]
        rows.append(f"| {st} | " + " | ".join(str(c) for c in cells) + f" | {sum(cells)} |")
    totals = [sum(1 for i in items if i["priority"] == p) for p in priorities]
    rows.append("| Totale | " + " | ".join(str(c) for c in totals) + f" | {len(items)} |")
    return "Riepilogo per stato e priorità:\n\n" + "\n".join(rows)


# Davanti a un elenco lungo diamo al modello una nota: l'elenco lo allega il sistema, a lui tocca solo una
# frase di introduzione, senza numeri.
def _long_list_note(data: Any) -> str:
    items = _long_items(data) or []
    total = data.get("total_matching", len(items))
    partial = f" su {total} corrispondenti (elenco troncato)" if total > len(items) else ""
    return (
        f"Il risultato contiene {len(items)} elementi{partial}. Il sistema allega alla tua risposta il "
        "riepilogo dei conteggi e l'elenco completo, calcolati dai dati: NON ricopiarli e NON scrivere "
        "numeri o conteggi. Scrivi solo una frase di introduzione, senza cifre."
    )


# Tentiamo di leggere il testo come JSON; se non lo è, lo restituiamo così com'è.
def _as_json(content: str) -> Any:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content


# I parametri numerici fuori dai limiti dello schema vengono riportati dentro (limit=100 diventa 50). Lo
# applichiamo soltanto alle letture; in uscita abbiamo gli argomenti corretti e le note.
def _clamp_to_bounds(args: dict[str, Any], schema: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Riconduce i parametri numerici ai limiti minimum/maximum dichiarati nello schema."""
    properties = schema.get("properties") or {}
    clamped = dict(args)
    notes: list[str] = []
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, dict) or isinstance(value, bool) or not isinstance(value, int | float):
            continue
        low, high = spec.get("minimum"), spec.get("maximum")
        if high is not None and value > high:
            clamped[name] = high
            notes.append(f"{name} {value} → {high} (massimo consentito)")
        elif low is not None and value < low:
            clamped[name] = low
            notes.append(f"{name} {value} → {low} (minimo consentito)")
    return clamped, notes


# Tool da cui ricavare ciascun identificativo, e tipo dell'"id" restituito da ogni tool.
ID_SOURCES = {
    "service_id": "list_services",
    "team_id": "list_teams",
    "request_id": "list_requests o get_request",
}
_ID_TYPE_BY_TOOL = {
    "list_services": "service_id",
    "list_teams": "team_id",
    "list_requests": "request_id",
    "get_request": "request_id",
    "create_request": "request_id",
    "assign_request": "request_id",
    "update_request_status": "request_id",
}
# Campi *_id che un tool restituisce come entità principale (non come riferimento).
# Gli altri *_id nei risultati (es. il service_id di ogni richiesta in list_requests)
# sono chiavi esterne: non bastano a verificare un id, perché il modello potrebbe
# prendere quello di un'altra richiesta. È l'errore osservato con il servizio 2 (staging).
_PRIMARY_ID_FIELDS = {"get_pipeline_status": {"service_id"}}
_NUMBER_RE = re.compile(r"(?<!\w)\d+(?!\w)")


# Ricerca automatica degli id validi: (tool di lettura, argomenti, campi da riportare).
# Solo elenchi piccoli e completi; per request_id il modello deve cercare da sé.
_LOOKUPS: dict[str, tuple[str, dict[str, Any], tuple[str, ...]]] = {
    "service_id": ("list_services", {}, ("id", "name", "environment")),
    "team_id": ("list_teams", {}, ("id", "name", "specialty")),
}


# Facciamo al posto del modello la ricerca che avrebbe dovuto fare lui: leggiamo l'elenco dei servizi o dei
# team e ne ricaviamo i valori ammessi, da proporgli come candidati.
async def _lookup_candidates(
    fields: list[str],
    catalog: dict[str, ToolSpec],
    executor: ToolExecutor,
    logger: RunLogger,
    st: _State,
    limits: Limits,
) -> dict[str, list[dict[str, Any]]]:
    """Esegue la lettura che il modello avrebbe dovuto fare e ne restituisce i valori ammessi.

    I modelli piccoli spesso non eseguono l'istruzione "chiama list_services" contenuta in un
    errore e ripetono la stessa scrittura: con i candidati nel messaggio scelgono tra id reali.
    È una lettura, quindi non richiede conferma; la scrittura successiva passa comunque dalla
    verifica degli id e dalla conferma dell'utente.
    """
    found: dict[str, list[dict[str, Any]]] = {}
    for id_field in fields:
        lookup = _LOOKUPS.get(id_field)
        if lookup is None or lookup[0] not in catalog:
            continue
        tool_name, tool_args, keep = lookup
        try:
            outcome = await asyncio.wait_for(
                executor.call_tool(tool_name, tool_args), timeout=limits.tool_timeout_s
            )
        except Exception:  # la ricerca è un aiuto: se fallisce resta l'istruzione testuale
            continue
        if not outcome.ok:
            continue
        items = _as_json(outcome.content)
        items = items.get("items", []) if isinstance(items, dict) else []
        found[id_field] = [{k: item.get(k) for k in keep} for item in items if isinstance(item, dict)]
        logger.event(
            "harness_lookup",
            iteration=st.iteration,
            tool=tool_name,
            field=id_field,
            count=len(found[id_field]),
        )
    return found


# Messaggio d'errore per un id non verificato: se il valore giusto è uno solo glielo indichiamo, altrimenti
# elenchiamo i candidati e chiediamo di riprovare subito.
def _ungrounded_message(
    tool_name: str,
    args: dict[str, Any],
    fields: list[str],
    candidates: dict[str, list[dict[str, Any]]],
    unique: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Istruzione operativa per il modello: deve ricavare lui l'id, non chiederlo all'utente."""
    unique = unique or {}
    wrong = ", ".join(f"{f}={args[f]}" for f in fields)
    head = (
        f"Operazione NON eseguita: {wrong} non proviene da un risultato degli strumenti né dalla "
        "richiesta dell'utente."
    )
    if unique and len(unique) == len(fields):
        fixes = ", ".join(f"{f}={item['id']}, cioè {describe(f, item)}" for f, item in unique.items())
        return (
            f"{head} L'unico valore coerente con la richiesta dell'utente è {fixes}. Richiama subito "
            f"{tool_name} con questo valore: non chiedere conferma all'utente, la chiederà il sistema."
        )
    if candidates and all(f in candidates for f in fields):
        listed = ", ".join(candidates)
        return (
            f"{head} I valori ammessi sono nel campo 'candidati' ({listed}): scegli quello che corrisponde "
            f"alla richiesta dell'utente, ambiente compreso, e richiama subito {tool_name} con quell'id. "
            "Chiedi un chiarimento all'utente solo se più candidati sono ugualmente plausibili."
        )
    steps = "; ".join(
        f"chiama {ID_SOURCES.get(f, 'lo strumento di elenco adatto')} per trovare il valore corretto di {f}"
        for f in fields
    )
    return (
        f"{head} Azione richiesta ora: {steps}; poi richiama {tool_name} con l'id trovato. "
        "Non chiedere all'utente di farlo e non inventare identificativi."
    )


UNEXECUTED_WRITE_NOTE = (
    "Nota del sistema: in questa richiesta nessuna operazione di scrittura è stata eseguita; "
    "i dati non sono stati modificati."
)

_FENCE = re.compile(r"^```[a-zA-Z]*\s*(.*?)\s*```$", re.DOTALL)
_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


# Certi modelli piccoli scrivono la chiamata come testo JSON invece di usare il campo apposito. Qui la
# riconosciamo, ma solo se tutto il testo è una chiamata a uno strumento esistente; in caso contrario la lista
# che ritorna è vuota.
def _text_tool_calls(text: str | None, catalog: dict[str, ToolSpec], iteration: int) -> list[ToolCall]:
    """Chiamate scritte come testo: accettate solo se il testo è *interamente* una chiamata.

    Forme riconosciute: un oggetto ``{"name": ..., "arguments": {...}}`` (o una lista di oggetti),
    anche dentro un blocco ``` o nei tag ``<tool_call>`` usati da alcuni modelli. Un testo che
    contiene una chiamata in mezzo a una spiegazione non viene interpretato: meglio una risposta
    a testo che un'azione non voluta.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    tagged = _TOOL_CALL_TAG.findall(raw)
    if tagged and not _TOOL_CALL_TAG.sub("", raw).strip():
        chunks = tagged
    else:
        fenced = _FENCE.match(raw)
        chunks = [fenced.group(1) if fenced else raw]
    items: list[Any] = []
    for chunk in chunks:
        try:
            data = json.loads(chunk)
        except json.JSONDecodeError:
            return []
        items.extend(data if isinstance(data, list) else [data])
    calls: list[ToolCall] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict) or set(item) - {"name", "arguments", "parameters"}:
            return []
        name = item.get("name")
        args = item.get("arguments", item.get("parameters", {}))
        if name not in catalog or not isinstance(args, dict):
            return []
        calls.append(ToolCall(id=f"text_call_{iteration}_{i}", name=name, arguments=args))
    return calls


# Se il modello ha proposto delle scritture e nessuna è andata a buon fine, attacchiamo una nota alla
# risposta: l'utente non deve credere a un successo che non c'è stato.
def _annotate_unexecuted_writes(answer: str, st: _State, logger: RunLogger) -> str:
    """Se il modello ha proposto scritture ma nessuna è andata a buon fine, lo dice il sistema.

    I modelli piccoli possono dichiarare riuscita un'operazione bloccata: la nota è deterministica e
    non dipende da come il modello ha formulato la risposta.
    """
    if not st.write_proposed or st.write_succeeded:
        return answer
    logger.event("answer_annotated", iteration=st.iteration, note=UNEXECUTED_WRITE_NOTE)
    return f"{answer.rstrip()}\n\n{UNEXECUTED_WRITE_NOTE}"


STALE_DATA_NUDGE = (
    "[Messaggio automatico dell'harness] La tua risposta riporta richieste e identificativi, ma in questa "
    "richiesta non hai letto nessuno strumento: i dati delle risposte precedenti possono essere superati o "
    "parziali (per esempio filtrati). Chiama ora lo strumento adatto con i filtri chiesti dall'utente in "
    "questo messaggio e rispondi solo con i dati appena letti."
)
STALE_DATA_NOTE = (
    "Nota del sistema: questa risposta non si basa su dati letti in questa richiesta e potrebbe non "
    "essere aggiornata o completa."
)
_CITED_ID = re.compile(r"\b(?:ID|id)\s*[:#]?\s*(\d+)|\brichiest[ae]\s+(?:n\.?\s*)?(\d+)")


# Estraiamo dal testo gli identificativi di richiesta citati ("ID: 5", "richiesta 12").
def _cited_ids(text: str) -> set[str]:
    return {a or b for a, b in _CITED_ID.findall(text)}


# Il filtro di priorità che l'utente non ha chiesto lo togliamo dalle letture. Non c'è rischio: si vedono più
# dati, mai dati diversi. Restituiamo gli argomenti ripuliti insieme alla nota.
def _drop_unrequested_filters(
    args: dict[str, Any], messages: list[Message]
) -> tuple[dict[str, Any], list[str]]:
    """Letture: un filtro di priorità che l'utente non ha chiesto viene tolto.

    Sessione in chat del 25/10: per «mostrami le richieste in coda» il modello aggiungeva da sé
    priority="low" e presentava 2 richieste su 14 come l'elenco completo. Togliere un filtro in lettura
    è sicuro: si mostrano più dati, mai dati diversi.
    """
    value = args.get("priority")
    if not value or value in mentioned_priority_levels(_last_user_text(messages)):
        return args, []
    cleaned = {k: v for k, v in args.items() if k != "priority"}
    return cleaned, [f"priority={value} tolto: l'utente non ha chiesto un filtro di priorità"]


_ASKED_COUNT = re.compile(
    r"\b(?:prim[ei]|ultim[ei]|massimo|max|top)\s+\d+|\b\d+\s+(?:richiest|element|risultat|voci)",
    re.IGNORECASE,
)


# Quando l'utente non dice quanti elementi vuole, alziamo il limite al massimo, così non ci troviamo a
# mostrare 10 richieste su 14. Se invece chiede "le prime 5", restano 5.
def _widen_unrequested_limit(
    args: dict[str, Any], schema: dict[str, Any], messages: list[Message]
) -> tuple[dict[str, Any], list[str]]:
    """Letture: se l'utente non ha indicato quanti elementi vuole, si usa il massimo ammesso.

    Sessione del 26/10: per «mostrami l'elenco delle richieste» il modello sceglieva da sé limit=10 e
    mostrava 10 richieste su 14. Un numero esplicito («le prime 5») viene rispettato.
    """
    prop = schema.get("properties", {}).get("limit", {})
    maximum = prop.get("maximum")
    if not isinstance(maximum, int) or _ASKED_COUNT.search(_last_user_text(messages)):
        return args, []
    current = args.get("limit", prop.get("default"))
    if current == maximum:
        return args, []
    return {**args, "limit": maximum}, [
        f"limit {current} → {maximum}: l'utente non ha indicato un numero di elementi"
    ]


# Ci dà il testo dell'ultimo messaggio scritto dall'utente.
def _last_user_text(messages: list[Message]) -> str:
    return next((m.get("content") or "" for m in reversed(messages) if m["role"] == "user"), "")


# Promemoria per il modello che ha chiuso il turno ignorando la correzione suggerita: gli ricordiamo il valore
# giusto e gli chiediamo di richiamare lo strumento.
def _nudge_message(hint: dict[str, Any]) -> str:
    fixes = ", ".join(f"{f}={v['id']}, cioè {v['label']}" for f, v in hint["fields"].items())
    return (
        f"[Messaggio automatico dell'harness] La tua ultima chiamata a {hint['tool']} è stata bloccata "
        f"{hint.get('reason', 'per un identificativo non verificato')}: l'operazione NON è stata eseguita e la tua ultima "
        f"risposta non è corretta. In base alla richiesta dell'utente il valore corretto è {fixes}. "
        f"Chiama ora lo strumento {hint['tool']} con questo valore e gli altri parametri invariati, "
        "usando la chiamata agli strumenti e non il testo della risposta; l'operazione sarà comunque "
        "confermata dall'utente."
    )


# Cerchiamo tra i candidati il servizio o il team nominato nella richiesta. Partiamo dall'ultimo messaggio e,
# se lì non c'è niente, scorriamo anche i precedenti.
def _match_in_request(
    field: str, messages: list[Message], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Corrispondenze nell'ultima richiesta dell'utente; se non ne contiene, in tutte le precedenti.

    L'ultima richiesta ha la precedenza: in una chat può nominare un servizio diverso da quelli
    dei turni prima. Se nemmeno l'insieme dà una corrispondenza unica, non si decide nulla.
    """
    users = [m["content"] for m in messages if m["role"] == "user" and isinstance(m.get("content"), str)]
    if not users:
        return []
    found = match_candidates(field, users[-1], candidates)
    return found or match_candidates(field, "\n".join(users), candidates)


# Nelle letture (e solo lì) sistemiamo un service_id o team_id inventato dal modello: se la richiesta nomina
# un unico servizio o team coerente, rimpiazziamo l'id con quello corretto.
async def _correct_read_ids(
    args: dict[str, Any],
    catalog: dict[str, ToolSpec],
    executor: ToolExecutor,
    logger: RunLogger,
    st: _State,
    limits: Limits,
    messages: list[Message],
) -> tuple[dict[str, Any], list[str]]:
    """Letture: un service_id/team_id non verificato viene sostituito con l'unico coerente con la richiesta."""
    fields = [f for f in _ungrounded_ids(args, messages) if f in _LOOKUPS]
    if not fields:
        return args, []
    candidates = await _lookup_candidates(fields, catalog, executor, logger, st, limits)
    corrected, notes = dict(args), []
    for f in fields:
        found = _match_in_request(f, messages, candidates.get(f, []))
        if len(found) == 1 and found[0]["id"] != args[f]:
            corrected[f] = found[0]["id"]
            notes.append(
                f"{f} {args[f]} → {found[0]['id']}, cioè {describe(f, found[0])}, in base alla richiesta"
            )
    return corrected, notes


# Andiamo a caccia degli id che non arrivano né dal testo dell'utente né dai risultati degli strumenti giusti:
# sono quelli che il modello si è inventato.
def _ungrounded_ids(args: dict[str, Any], messages: list[Message]) -> list[str]:
    """Campi *_id della chiamata che non compaiono né nei messaggi dell'utente né nei risultati."""
    from_user: set[int] = set()
    from_tools: dict[str, set[int]] = {}
    for m in messages:
        if m["role"] == "user":
            from_user.update(int(n) for n in _NUMBER_RE.findall(m["content"]))
        elif m["role"] == "tool":
            try:
                data = json.loads(m["content"])
            except (json.JSONDecodeError, TypeError):
                continue
            name = m.get("name", "")
            if isinstance(data, dict) and isinstance(data.get("candidati"), dict):
                # candidati letti dall'harness dopo un ID_NON_VERIFICATO: valgono come risultati di lettura
                for field, items in data["candidati"].items():
                    for item in items if isinstance(items, list) else []:
                        if isinstance(item, dict) and isinstance(item.get("id"), int):
                            from_tools.setdefault(field, set()).add(item["id"])
                continue
            _collect_ids(data, _ID_TYPE_BY_TOOL.get(name), _PRIMARY_ID_FIELDS.get(name, set()), from_tools)
    return [
        field
        for field, value in args.items()
        if field.endswith("_id")
        and isinstance(value, int)
        and value not in from_user
        and value not in from_tools.get(field, set())
    ]


# Raccolta ricorsiva degli id contenuti in un risultato. Teniamo separato l'id dell'entità restituita dalle
# chiavi esterne, che per noi non contano come verificate.
def _collect_ids(data: Any, id_type: str | None, primary_fields: set[str], out: dict[str, set[int]]) -> None:
    if isinstance(data, list):
        for item in data:
            _collect_ids(item, id_type, primary_fields, out)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, int) and not isinstance(value, bool):
                if key == "id" and id_type:
                    out.setdefault(id_type, set()).add(value)
                elif key in primary_fields:
                    out.setdefault(key, set()).add(value)
            else:
                _collect_ids(value, id_type, primary_fields, out)


# Parametri che lo strumento non prevede? Lanciamo un errore: il server li ignorerebbe in silenzio e il
# modello sarebbe convinto di averli applicati.
def _reject_unknown_parameters(args: dict[str, Any], schema: dict[str, Any]) -> None:
    """Gli schemi generati non vietano i parametri extra: un parametro inventato dal modello
    verrebbe ignorato in silenzio dal server, e il modello crederebbe di averlo applicato."""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or schema.get("additionalProperties") is True:
        return
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError(
            f"parametri non previsti: {', '.join(unknown)}. Parametri ammessi: {', '.join(properties) or 'nessuno'}"
        )


# Gli argomenti della chiamata diventano un dizionario. Alcuni modelli li spediscono come stringa JSON; se la
# stringa non è JSON valido, solleviamo un errore comprensibile.
def _parse_arguments(raw: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"argomenti non in formato JSON valido ({exc.msg})") from exc
    if not isinstance(value, dict):
        raise ValueError("gli argomenti devono essere un oggetto JSON")
    return value


# Impacchettiamo un errore in JSON, con codice e messaggio, nel formato che il modello sa leggere.
def _error(code: str, message: str) -> str:
    return json.dumps({"error": code, "message": message}, ensure_ascii=False)


# Per scrivere un messaggio nel log ci serve un dizionario: le chiamate agli strumenti sono oggetti e vanno
# convertite.
def _loggable(m: Message) -> dict[str, Any]:
    entry = {k: v for k, v in m.items() if k != "tool_calls"}
    if m.get("tool_calls"):
        entry["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in m["tool_calls"]
        ]
    return entry
