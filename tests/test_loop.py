"""Test del ciclo agentico con modello ed executor finti (nessun processo esterno)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from devops_agent.agent.logger import RunLogger
from devops_agent.agent.loop import UNEXECUTED_WRITE_NOTE, run_agent
from devops_agent.agent.providers.scripted import ScriptedProvider
from devops_agent.agent.types import Limits, LLMResponse, ProviderError, ToolCall, ToolOutcome, ToolSpec

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string", "minLength": 5}, "priority": {"enum": ["low", "high"]}},
    "required": ["title", "priority"],
}
WRITE = ToolSpec("create_request", "crea", SCHEMA, is_write=True)
READ = ToolSpec(
    "list_requests",
    "elenca",
    {"type": "object", "properties": {"limit": {"type": "integer"}}},
    is_write=False,
)
OK_ARGS = {"title": "Titolo valido", "priority": "high"}


class FakeExecutor:
    def __init__(self, fail: Exception | None = None, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = fail
        self.delay = delay

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        self.calls.append((name, args))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return ToolOutcome(True, json.dumps({"ok": True}))


def answers(*values: bool):
    pending = list(values)

    async def confirm(tool: str, args: dict[str, Any]) -> bool:
        return pending.pop(0) if pending else False

    return confirm


def run(steps, *, executor=None, confirm=None, limits=None, tools=(WRITE, READ), provider=None):
    provider = provider or ScriptedProvider(steps)
    executor = executor or FakeExecutor()
    logger = RunLogger(None)
    result = asyncio.run(
        run_agent(
            "richiesta",
            llm=provider,
            tools=list(tools),
            executor=executor,
            confirm=confirm or answers(True),
            logger=logger,
            system_prompt="sistema",
            limits=limits or Limits(),
        )
    )
    return result, executor, logger, provider


def call(name: str, arguments: Any) -> dict[str, Any]:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def events(logger: RunLogger, kind: str) -> list[dict[str, Any]]:
    return [e for e in logger.events if e["event"] == kind]


def test_final_answer_without_tools():
    result, executor, logger, _ = run([{"text": "ciao"}])
    assert (result.stop_reason, result.answer, executor.calls) == ("final_answer", "ciao", [])
    assert result.tokens_in is None  # il provider non fornisce token: resta null, non 0


def test_assistant_message_precedes_tool_result_with_matching_id():
    _, _, _, provider = run([call("create_request", OK_ARGS), {"text": "fatto"}])
    second_request = provider.received[1]
    assistant, tool = second_request[-2], second_request[-1]
    assert assistant["role"] == "assistant" and tool["role"] == "tool"
    assert tool["tool_call_id"] == assistant["tool_calls"][0].id


def test_invalid_arguments_are_blocked_before_execution_then_corrected():
    result, executor, logger, _ = run(
        [
            call("create_request", {"title": "abc", "priority": "high"}),
            call("create_request", OK_ARGS),
            {"text": "fatto"},
        ]
    )
    assert result.stop_reason == "final_answer"
    assert executor.calls == [("create_request", OK_ARGS)]
    assert events(logger, "tool_invalid_args")[0]["error"].startswith("title:")


def test_malformed_json_arguments():
    result, executor, logger, _ = run([call("create_request", '{"title": "x'), {"text": "ok"}])
    assert executor.calls == [] and len(events(logger, "tool_invalid_args")) == 1
    assert result.stop_reason == "final_answer"


def test_string_arguments_are_parsed():
    _, executor, _, _ = run([call("create_request", json.dumps(OK_ARGS)), {"text": "ok"}])
    assert executor.calls == [("create_request", OK_ARGS)]


def test_unknown_tool_is_rejected_without_server_call():
    result, executor, logger, _ = run([call("delete_everything", {}), {"text": "non posso"}])
    assert executor.calls == []
    assert events(logger, "tool_rejected")[0]["reason"] == "non_disponibile"
    assert result.stop_reason == "final_answer"


def test_denied_confirmation_prevents_execution_and_repeat():
    result, executor, logger, _ = run(
        [call("create_request", OK_ARGS), call("create_request", OK_ARGS), {"text": "annullato"}],
        confirm=answers(False, True),
    )
    assert executor.calls == []
    assert len(events(logger, "confirmation")) == 1  # la ripetizione non viene riproposta all'utente
    assert events(logger, "tool_rejected")[0]["reason"] == "gia_rifiutata"
    assert result.stop_reason == "final_answer"


def test_read_tools_do_not_ask_confirmation():
    _, executor, logger, _ = run([call("list_requests", {"limit": 5}), {"text": "ok"}], confirm=answers())
    assert executor.calls == [("list_requests", {"limit": 5})] and events(logger, "confirmation") == []


def test_tool_exception_becomes_error_result():
    result, _, logger, provider = run(
        [call("list_requests", {}), {"text": "errore gestito"}], executor=FakeExecutor(fail=ConnectionError())
    )
    tool_message = provider.received[1][-1]
    assert json.loads(tool_message["content"])["error"] == "TOOL_FALLITO"
    assert result.stop_reason == "final_answer" and not result.executed[0].ok


def test_tool_timeout():
    result, _, logger, _ = run(
        [call("list_requests", {}), {"text": "timeout"}],
        executor=FakeExecutor(delay=0.5),
        limits=Limits(tool_timeout_s=0.05),
    )
    assert events(logger, "tool_result")[0]["error_code"] == "TIMEOUT"
    assert result.stop_reason == "final_answer"


def test_max_iterations():
    steps = [call("list_requests", {"limit": i}) for i in range(1, 10)]
    result, _, _, _ = run(steps, limits=Limits(max_iterations=3))
    assert (result.stop_reason, result.iterations) == ("max_iterations", 3)


def test_loop_detection():
    steps = [call("list_requests", {"limit": 5})] * 5
    result, executor, _, _ = run(steps)
    assert result.stop_reason == "loop_detected" and len(executor.calls) == 2


def test_repeated_invalid_arguments_stop_the_run():
    steps = [call("create_request", {"priority": "high"})] * 5
    result, executor, _, _ = run(steps, limits=Limits(max_invalid_args=3))
    assert result.stop_reason == "invalid_arguments" and executor.calls == []


def test_too_many_tool_calls_in_one_turn():
    many = {"tool_calls": [{"name": "list_requests", "arguments": {"limit": i}} for i in range(1, 8)]}
    _, executor, _, _ = run([many, {"text": "ok"}], limits=Limits(max_tool_calls_per_turn=3))
    assert len(executor.calls) == 3


def test_empty_response():
    result, _, _, _ = run([{"text": "   "}])
    assert result.stop_reason == "empty_response"


class _CountingProvider:
    name, model = "fake", "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def generate(self, messages, tools):
        if self.fail:
            raise ProviderError("irraggiungibile")
        return LLMResponse(None, [ToolCall("c", "list_requests", {"limit": len(messages)})], 9000, 2000)

    async def aclose(self):
        return None


def test_token_budget():
    result, _, _, _ = run([], provider=_CountingProvider(), limits=Limits(max_total_tokens=15000))
    assert result.stop_reason == "token_budget" and result.tokens_in == 18000


def test_provider_error():
    result, _, logger, _ = run([], provider=_CountingProvider(fail=True))
    assert result.stop_reason == "provider_error" and len(events(logger, "llm_error")) == 1


def test_log_contains_required_fields():
    _, _, logger, _ = run([call("create_request", OK_ARGS), {"text": "fatto"}])
    kinds = [e["event"] for e in logger.events]
    assert kinds == [
        "run_start",
        "llm_request",
        "llm_response",
        "confirmation",
        "tool_call",
        "tool_result",
        "llm_request",
        "llm_response",
        "stop",
    ]
    assert events(logger, "llm_request")[0]["new_messages"][0]["role"] == "system"
    assert events(logger, "tool_call")[0]["arguments"] == OK_ARGS
    assert "tokens_in" in events(logger, "llm_response")[0]


ASSIGN = ToolSpec(
    "assign_request",
    "assegna",
    {
        "type": "object",
        "properties": {"request_id": {"type": "integer"}, "team_id": {"type": "integer"}},
        "required": ["request_id", "team_id"],
    },
    is_write=True,
)
LIST_TEAMS = ToolSpec("list_teams", "team", {"type": "object", "properties": {}}, is_write=False)


class TeamsExecutor(FakeExecutor):
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "list_teams":
            return ToolOutcome(True, json.dumps({"items": [{"id": 4, "name": "Cloud Security"}]}))
        if name == "list_requests":
            return ToolOutcome(True, json.dumps({"items": [{"id": 7, "service_id": 3}]}))
        return ToolOutcome(True, "{}")


def run_assign(steps, request="assegna la richiesta 2", confirm=None):
    executor = TeamsExecutor()
    logger = RunLogger(None)
    result = asyncio.run(
        run_agent(
            request,
            llm=ScriptedProvider(steps),
            tools=[ASSIGN, LIST_TEAMS, READ],
            executor=executor,
            confirm=confirm or answers(True, True),
            logger=logger,
            system_prompt="s",
        )
    )
    return result, executor, logger


def test_invented_id_is_rejected_before_confirmation():
    # team 9 non compare né nella richiesta né in un risultato: è inventato
    _, executor, logger = run_assign(
        [call("assign_request", {"request_id": 2, "team_id": 9}), {"text": "ok"}]
    )
    # nessuna scrittura: l'unica chiamata è la lettura dei team fatta dall'harness
    assert executor.calls == [("list_teams", {})]
    assert events(logger, "confirmation") == []
    assert events(logger, "tool_ungrounded")[0]["fields"] == ["team_id"]


def test_ids_from_user_text_and_tool_results_are_accepted():
    _, executor, logger = run_assign(
        [call("list_teams", {}), call("assign_request", {"request_id": 2, "team_id": 4}), {"text": "ok"}]
    )
    assert ("assign_request", {"request_id": 2, "team_id": 4}) in executor.calls
    assert len(events(logger, "confirmation")) == 1


def test_ids_are_typed_by_source_tool():
    # l'id 7 di list_requests è una richiesta: non vale come team_id
    _, executor, logger = run_assign(
        [call("list_requests", {}), call("assign_request", {"request_id": 2, "team_id": 7}), {"text": "ok"}]
    )
    assert ("assign_request", {"request_id": 2, "team_id": 7}) not in executor.calls
    assert events(logger, "tool_ungrounded")


def test_ungrounded_error_tells_the_model_what_to_call():
    # Senza list_teams tra i tool visibili l'harness non può cercare: resta l'istruzione testuale.
    executor = TeamsExecutor()
    result = asyncio.run(
        run_agent(
            "assegna la richiesta 2",
            llm=ScriptedProvider([call("assign_request", {"request_id": 2, "team_id": 9}), {"text": "ok"}]),
            tools=[ASSIGN],
            executor=executor,
            confirm=answers(True),
            logger=RunLogger(None),
            system_prompt="s",
        )
    )
    payload = json.loads(next(m for m in result.messages if m["role"] == "tool")["content"])
    assert payload["error"] == "ID_NON_VERIFICATO" and "candidati" not in payload
    assert "chiama list_teams" in payload["message"] and "Non chiedere all'utente" in payload["message"]
    assert executor.calls == []


def test_ungrounded_write_gets_candidates_and_retry_is_accepted():
    # Sessione reale: il modello ripeteva la stessa scrittura con un id non verificato fino al
    # blocco per ripetizione. Ora l'harness legge i team e li mette nell'errore come candidati.
    result, executor, logger = run_assign(
        [
            call("assign_request", {"request_id": 2, "team_id": 4}),
            call("assign_request", {"request_id": 2, "team_id": 4}),
            {"text": "fatto"},
        ],
        request="assegna la richiesta 2 al team sicurezza",
    )
    payload = json.loads(next(m for m in result.messages if m["role"] == "tool")["content"])
    assert payload["candidati"]["team_id"] == [{"id": 4, "name": "Cloud Security", "specialty": None}]
    assert events(logger, "harness_lookup")[0]["field"] == "team_id"
    assert executor.calls == [("list_teams", {}), ("assign_request", {"request_id": 2, "team_id": 4})]
    assert len(events(logger, "confirmation")) == 1 and result.stop_reason == "final_answer"


CREATE = ToolSpec(
    "create_request",
    "crea",
    {
        "type": "object",
        "properties": {"title": {"type": "string"}, "service_id": {"type": "integer"}},
        "required": ["title", "service_id"],
    },
    is_write=True,
)


def test_only_one_write_per_turn():
    # Riproduce la sessione reale: assign e create pianificati insieme nella stessa risposta.
    both = {
        "tool_calls": [
            {"name": "assign_request", "arguments": {"request_id": 2, "team_id": 4}},
            {"name": "create_request", "arguments": {"title": "Duplicato", "service_id": 2}},
        ]
    }
    executor = TeamsExecutor()
    logger = RunLogger(None)
    asyncio.run(
        run_agent(
            "la richiesta 2 è risolta, assegnala al team 4",
            llm=ScriptedProvider([both, {"text": "ok"}]),
            tools=[ASSIGN, CREATE, LIST_TEAMS],
            executor=executor,
            confirm=answers(True, True),
            logger=logger,
            system_prompt="s",
        )
    )
    assert [name for name, _ in executor.calls] == ["assign_request"]
    assert len(events(logger, "confirmation")) == 1
    assert events(logger, "tool_rejected")[0]["reason"] == "scrittura_multipla"


def test_unknown_parameters_are_rejected():
    # Nella sessione reale il modello ha passato order_by quando il tool non lo prevedeva:
    # il server lo ignorava e il modello presentava un ordinamento mai applicato.
    result, executor, logger, _ = run(
        [call("list_requests", {"limit": 5, "order_by": "id desc"}), {"text": "ok"}]
    )
    assert executor.calls == []
    assert "parametri non previsti: order_by" in events(logger, "tool_invalid_args")[0]["error"]


def test_foreign_key_ids_in_results_do_not_ground_a_write():
    # list_requests restituisce il service_id di ogni richiesta: non basta per usarlo in una scrittura.
    executor = TeamsExecutor()
    logger = RunLogger(None)
    asyncio.run(
        run_agent(
            "apri una richiesta per payments-api in produzione",
            llm=ScriptedProvider(
                [
                    call("list_requests", {}),
                    call("create_request", {"title": "Build lenta", "service_id": 3}),
                    {"text": "ok"},
                ]
            ),
            tools=[CREATE, READ],
            executor=executor,
            confirm=answers(True),
            logger=logger,
            system_prompt="s",
        )
    )
    assert ("create_request", {"title": "Build lenta", "service_id": 3}) not in executor.calls
    assert events(logger, "tool_ungrounded")[0]["fields"] == ["service_id"]


def test_trim_history_cuts_on_user_turns():
    from devops_agent.agent.session import trim_history

    msgs = []
    for i in range(5):
        msgs += [
            {"role": "user", "content": f"domanda {i}"},
            {"role": "assistant", "content": None, "tool_calls": [ToolCall(f"c{i}", "list_requests", {})]},
            {"role": "tool", "tool_call_id": f"c{i}", "name": "list_requests", "content": "{}"},
            {"role": "assistant", "content": f"risposta {i}", "tool_calls": []},
        ]
    trimmed = trim_history(msgs, 2)
    assert trimmed[0] == {"role": "user", "content": "domanda 3"} and len(trimmed) == 8
    assert trim_history(msgs, 0) == [] and trim_history(msgs, 10) == msgs


BOUNDED_READ = ToolSpec(
    "list_requests",
    "elenca",
    {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
    is_write=False,
)
BOUNDED_WRITE = ToolSpec(
    "create_request",
    "crea",
    {
        "type": "object",
        "properties": {"title": {"type": "string"}, "priority": {"type": "integer", "maximum": 3}},
        "required": ["title"],
    },
    is_write=True,
)


def test_read_parameters_out_of_bounds_are_clamped():
    # Sessione reale: il modello chiedeva limit=100 (massimo 50) e poi chiedeva all'utente cosa fare.
    result, executor, logger, provider = run(
        [call("list_requests", {"limit": 100}), {"text": "ok"}], tools=(BOUNDED_READ,)
    )
    assert executor.calls == [("list_requests", {"limit": 50})]
    assert events(logger, "tool_args_adjusted")[0]["adjustments"] == ["limit 100 → 50 (massimo consentito)"]
    tool_message = json.loads(provider.received[1][-1]["content"])
    assert "limit 100 → 50" in tool_message["nota_harness"] and tool_message["risultato"] == {"ok": True}
    assert result.stop_reason == "final_answer"


def test_write_parameters_out_of_bounds_are_rejected_not_clamped():
    _, executor, logger, provider = run(
        [call("create_request", {"title": "Titolo", "priority": 9}), {"text": "ok"}], tools=(BOUNDED_WRITE,)
    )
    assert executor.calls == [] and events(logger, "tool_args_adjusted") == []
    payload = json.loads(provider.received[1][-1]["content"])
    assert payload["error"] == "PARAMETRI_NON_VALIDI" and "richiama subito" in payload["message"]


SERVICES_JSON = json.dumps(
    {
        "items": [
            {"id": 1, "name": "payments-api", "environment": "prod"},
            {"id": 2, "name": "payments-api", "environment": "staging"},
            {"id": 3, "name": "customer-portal", "environment": "prod"},
        ]
    }
)
PIPELINE = ToolSpec(
    "get_pipeline_status",
    "pipeline",
    {"type": "object", "properties": {"service_id": {"type": "integer"}}, "required": ["service_id"]},
    is_write=False,
)
LIST_SERVICES = ToolSpec("list_services", "servizi", {"type": "object", "properties": {}}, is_write=False)


class ServicesExecutor(FakeExecutor):
    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "list_services":
            return ToolOutcome(True, SERVICES_JSON)
        return ToolOutcome(True, "{}")


def run_services(request, steps, tools=(CREATE, LIST_SERVICES), confirm=None):
    executor = ServicesExecutor()
    logger = RunLogger(None)
    provider = ScriptedProvider(steps)
    result = asyncio.run(
        run_agent(
            request,
            llm=provider,
            tools=list(tools),
            executor=executor,
            confirm=confirm or answers(True),
            logger=logger,
            system_prompt="s",
        )
    )
    return result, executor, logger, provider


def test_id_matching_the_only_named_service_is_accepted():
    # TST-05/TST-17 reali: service_id=1 giusto ma mai letto, la richiesta dice "payments-api in produzione".
    _, executor, logger, _ = run_services(
        "apri una richiesta per payments-api in produzione",
        [call("create_request", {"title": "Build lenta", "service_id": 1}), {"text": "ok"}],
    )
    assert ("create_request", {"title": "Build lenta", "service_id": 1}) in executor.calls
    assert events(logger, "id_verified_by_match")[0]["match"] == "payments-api (prod)"
    assert events(logger, "tool_ungrounded") == []


def test_ambiguous_service_is_not_accepted():
    # Senza ambiente, payments-api corrisponde a due servizi: l'harness non sceglie.
    _, executor, logger, _ = run_services(
        "apri una richiesta per payments-api",
        [call("create_request", {"title": "Build lenta", "service_id": 1}), {"text": "quale ambiente?"}],
    )
    assert all(name != "create_request" for name, _ in executor.calls)
    assert events(logger, "harness_nudge") == []


def test_wrong_id_gets_one_nudge_with_the_matching_value():
    result, executor, logger, provider = run_services(
        "apri una richiesta per payments-api in produzione",
        [
            call("create_request", {"title": "Build lenta", "service_id": 2}),
            {"text": "Il servizio non è corretto."},
            call("create_request", {"title": "Build lenta", "service_id": 1}),
            {"text": "fatto"},
        ],
    )
    error = json.loads(next(m for m in result.messages if m["role"] == "tool")["content"])
    assert "service_id=1, cioè payments-api (prod)" in error["message"]
    assert error["candidati"]["service_id"] == [{"id": 1, "name": "payments-api", "environment": "prod"}]
    nudge = next(m for m in provider.received[2] if m["role"] == "system" and "harness" in m["content"])
    assert "service_id=1" in nudge["content"]
    assert ("create_request", {"title": "Build lenta", "service_id": 1}) in executor.calls
    assert len(events(logger, "harness_nudge")) == 1 and result.stop_reason == "final_answer"


def test_nudge_happens_at_most_once():
    result, _, logger, _ = run_services(
        "apri una richiesta per payments-api in produzione",
        [
            call("create_request", {"title": "Build lenta", "service_id": 2}),
            {"text": "no"},
            {"text": "ancora no"},
        ],
    )
    assert len(events(logger, "harness_nudge")) == 1
    assert result.answer.startswith("ancora no") and UNEXECUTED_WRITE_NOTE in result.answer


def test_read_with_invented_service_id_is_corrected():
    # TST-04 reale: get_pipeline_status(service_id=123) per "customer-portal in produzione".
    result, executor, logger, _ = run_services(
        "com'è andato l'ultimo deploy di customer-portal in produzione?",
        [call("get_pipeline_status", {"service_id": 123}), {"text": "fallito"}],
        tools=(PIPELINE, LIST_SERVICES),
    )
    assert ("get_pipeline_status", {"service_id": 3}) in executor.calls
    assert (
        "service_id 123 → 3, cioè customer-portal (prod)"
        in events(logger, "tool_args_adjusted")[0]["adjustments"][0]
    )


def test_read_only_role_is_told_in_the_system_prompt():
    from devops_agent.agent.prompts import build_system_prompt

    viewer = build_system_prompt(organization="o", user="u", role="viewer", can_write=False)
    operator = build_system_prompt(organization="o", user="u", role="operator", can_write=True)
    assert "sola lettura" in viewer and "sola lettura" not in operator


# --- Chiamate scritte come testo (esecuzione 2, TST-07 e TST-18) -----------------------------


def test_tool_call_written_as_text_is_executed_after_confirmation():
    text = '{"name": "create_request", "arguments": {"title": "Titolo valido", "priority": "high"}}'
    result, executor, logger, _ = run([{"text": text}, {"text": "fatto"}], confirm=answers(True))
    assert executor.calls == [("create_request", OK_ARGS)]
    assert len(events(logger, "text_tool_call_parsed")) == 1
    assert len(events(logger, "confirmation")) == 1
    assert result.answer == "fatto"


def test_tool_call_written_as_text_still_needs_confirmation():
    text = '{"name": "create_request", "arguments": {"title": "Titolo valido", "priority": "high"}}'
    result, executor, _, _ = run([{"text": text}, {"text": "non eseguita"}], confirm=answers(False))
    assert executor.calls == []
    assert UNEXECUTED_WRITE_NOTE in result.answer


def test_tool_call_in_fence_or_tags_is_recognised():
    payload = '{"name": "list_requests", "arguments": {"limit": 5}}'
    for text in (f"```json\n{payload}\n```", f"<tool_call>\n{payload}\n</tool_call>"):
        _, executor, _, _ = run([{"text": text}, {"text": "ok"}])
        assert executor.calls == [("list_requests", {"limit": 5})]


def test_text_with_json_inside_an_explanation_is_not_a_tool_call():
    for text in (
        'Ecco il risultato: {"name": "list_requests", "arguments": {}}',
        '{"request_id": 2, "team_id": 4, "status": "in_progress"}',
        '{"name": "drop_database", "arguments": {}}',
    ):
        result, executor, logger, _ = run([{"text": text}])
        assert executor.calls == [] and not events(logger, "text_tool_call_parsed")
        assert result.answer == text


def test_false_success_after_blocked_write_is_annotated():
    # TST-07, ripetizione 1: la scrittura è bloccata, il modello dichiara comunque il successo.
    result, executor, logger, _ = run(
        [call("create_request", {"title": "x", "priority": "high"}), {"text": "Fatto con successo."}],
        limits=Limits(max_invalid_args=5),
    )
    assert executor.calls == []
    assert result.answer == f"Fatto con successo.\n\n{UNEXECUTED_WRITE_NOTE}"
    assert len(events(logger, "answer_annotated")) == 1


def test_successful_write_and_pure_reads_are_not_annotated():
    result, *_ = run([call("create_request", OK_ARGS), {"text": "creata"}])
    assert result.answer == "creata"
    result, *_ = run([call("list_requests", {}), {"text": "elenco"}])
    assert result.answer == "elenco"


# --- Priorità coerente con la richiesta (esecuzione 3, TST-17) --------------------------------

CREATE_P = ToolSpec(
    "create_request",
    "crea",
    {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "service_id": {"type": "integer"},
            "priority": {"enum": ["low", "medium", "high", "critical"]},
        },
        "required": ["title", "service_id", "priority"],
    },
    is_write=True,
)
HIGH_REQUEST = "Apri una richiesta ad alta priorità per payments-api in produzione: build in errore."


def test_priority_different_from_the_requested_one_is_blocked():
    wrong = {"title": "Build in errore", "service_id": 1, "priority": "critical"}
    right = {**wrong, "priority": "high"}
    result, executor, logger, _ = run_services(
        HIGH_REQUEST,
        [call("create_request", wrong), call("create_request", right), {"text": "creata"}],
        tools=(CREATE_P, LIST_SERVICES),
    )
    error = json.loads(next(m for m in result.messages if m["role"] == "tool")["content"])
    assert error["error"] == "PARAMETRO_INCOERENTE" and "priority=high" in error["message"]
    assert ("create_request", wrong) not in executor.calls
    assert ("create_request", right) in executor.calls
    mismatch = events(logger, "tool_arg_mismatch")
    assert len(mismatch) == 1 and mismatch[0]["expected"] == "high"
    assert result.answer == "creata"


def test_priority_mismatch_gets_a_nudge_if_the_model_stops():
    wrong = {"title": "Build in errore", "service_id": 1, "priority": "critical"}
    result, executor, logger, provider = run_services(
        HIGH_REQUEST,
        [
            call("create_request", wrong),
            {"text": "Fatto."},
            call("create_request", {**wrong, "priority": "high"}),
            {"text": "creata"},
        ],
        tools=(CREATE_P, LIST_SERVICES),
    )
    nudge = next(m for m in provider.received[2] if m["role"] == "system" and "harness" in m["content"])
    assert "priorità diversa" in nudge["content"] and "priority=high" in nudge["content"]
    assert ("create_request", {**wrong, "priority": "high"}) in executor.calls


def test_priority_not_stated_is_not_checked():
    args = {"title": "Build in errore", "service_id": 1, "priority": "critical"}
    _, executor, logger, _ = run_services(
        "Apri una richiesta per payments-api in produzione: build in errore.",
        [call("create_request", args), {"text": "creata"}],
        tools=(CREATE_P, LIST_SERVICES),
    )
    assert ("create_request", args) in executor.calls and not events(logger, "tool_arg_mismatch")


def test_mentioned_priority():
    from devops_agent.agent.matching import mentioned_priority

    assert mentioned_priority("Apri una richiesta ad alta priorità per payments-api") == "high"
    assert mentioned_priority("priorità bassa, notification-worker") == "low"
    assert mentioned_priority("Crea una richiesta critica per identity-service") == "critical"
    assert mentioned_priority("high priority ticket") == "high"
    assert mentioned_priority("la latenza è alta in produzione") is None
    assert mentioned_priority("alta priorità, anzi priorità critica") is None
    assert mentioned_priority("Quali richieste critiche sono aperte?") is None


def test_long_list_is_attached_by_the_harness_not_copied_by_the_model():
    items = [
        {"id": i, "title": f"Richiesta {i}", "status": "open" if i % 2 else "closed", "priority": "high"}
        for i in range(1, 31)
    ]

    class ListExecutor(FakeExecutor):
        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return ToolOutcome(True, json.dumps({"count": 30, "total_matching": 30, "items": items}))

    result, _, logger, _ = run(
        [call("list_requests", {}), {"text": "Sono 30 richieste."}], executor=ListExecutor()
    )
    content = json.loads(next(m for m in result.messages if m["role"] == "tool")["content"])
    note = content["nota_harness"]
    assert "30 elementi" in note and "NON scrivere numeri" in note
    assert "| open | 15 | 15 |" in result.answer and "| Totale | 30 | 30 |" in result.answer
    assert result.answer.startswith("Sono 30 richieste.")
    assert "Elenco generato dal sistema dai dati dello strumento: 30 elementi." in result.answer
    assert "| 30 | Richiesta 30 | closed | high |" in result.answer
    assert result.answer.count("| Richiesta ") == 30 and len(events(logger, "list_attached")) == 1


def test_failed_read_is_retried_once_by_the_harness():
    class FlakyExecutor(FakeExecutor):
        async def call_tool(self, name, args):
            self.calls.append((name, args))
            if len(self.calls) == 1:
                return ToolOutcome(False, "errore interno", "TOOL_FALLITO")
            return ToolOutcome(True, json.dumps({"ok": True}))

    result, executor, logger, _ = run([call("list_requests", {}), {"text": "ok"}], executor=FlakyExecutor())
    assert len(executor.calls) == 2 and len(events(logger, "tool_retry")) == 1
    assert next(m for m in result.messages if m["role"] == "tool")["content"] == json.dumps({"ok": True})


def test_persistent_read_failure_tells_the_model_not_to_promise_a_retry():
    result, executor, logger, _ = run(
        [call("list_requests", {}), {"text": "non disponibile"}],
        executor=FakeExecutor(fail=RuntimeError("guasto")),
    )
    error = json.loads(next(m for m in result.messages if m["role"] == "tool")["content"])
    assert len(executor.calls) == 2 and error["error"] == "TOOL_FALLITO"
    assert (
        "ha già ripetuto la chiamata" in error["message"]
        and "non scrivere che riproverai" in error["message"]
    )


def test_failed_write_is_not_retried():
    _, executor, logger, _ = run(
        [call("create_request", OK_ARGS), {"text": "errore"}], executor=FakeExecutor(fail=RuntimeError("x"))
    )
    assert len(executor.calls) == 1 and not events(logger, "tool_retry")


def test_short_list_result_is_unchanged():
    result, *_ = run([call("list_requests", {}), {"text": "ok"}])
    assert next(m for m in result.messages if m["role"] == "tool")["content"] == json.dumps({"ok": True})


# --- Chat del 25/10: filtro inventato e dati ricopiati dalla memoria --------------------------------

LIST_P = ToolSpec(
    "list_requests",
    "elenca",
    {"type": "object", "properties": {"priority": {"type": "string"}, "status": {"type": "string"}}},
    is_write=False,
)


def run_request(request, steps, tools=(LIST_P,), history=None):
    executor = FakeExecutor()
    logger = RunLogger(None)
    result = asyncio.run(
        run_agent(
            request,
            llm=ScriptedProvider(steps),
            tools=list(tools),
            executor=executor,
            confirm=answers(True),
            logger=logger,
            system_prompt="s",
            history=history,
        )
    )
    return result, executor, logger


def test_priority_filter_not_requested_is_dropped_from_reads():
    _, executor, logger = run_request(
        "Mostrami l'elenco delle richieste in coda",
        [call("list_requests", {"status": "open", "priority": "low"}), {"text": "ok"}],
    )
    assert executor.calls == [("list_requests", {"status": "open"})]
    assert "priority=low tolto" in events(logger, "tool_args_adjusted")[0]["adjustments"][0]


def test_priority_filter_requested_is_kept():
    for request, level in [
        ("Mostrami le richieste in coda in priority high", "high"),
        ("Quali richieste critiche sono ancora aperte?", "critical"),
        ("elenca quelle a bassa priorità", "low"),
    ]:
        _, executor, _ = run_request(request, [call("list_requests", {"priority": level}), {"text": "ok"}])
        assert executor.calls == [("list_requests", {"priority": level})]


def test_answer_with_data_but_no_reading_gets_one_nudge():
    result, executor, logger = run_request(
        "elencami tutte le richieste",
        [{"text": "1. ID: 1 …\n2. ID: 5 …"}, call("list_requests", {}), {"text": "Sono 30."}],
    )
    assert len(events(logger, "stale_data_nudge")) == 1 and executor.calls == [("list_requests", {})]
    assert result.answer == "Sono 30."


def test_answer_with_data_and_no_reading_after_the_nudge_is_annotated():
    result, _, logger = run_request(
        "elencami tutte le richieste", [{"text": "ID: 1, ID: 5"}, {"text": "ID: 1, ID: 5"}]
    )
    assert len(events(logger, "stale_data_nudge")) == 1
    assert "non si basa su dati letti in questa richiesta" in result.answer


def test_answer_without_data_is_not_nudged():
    _, _, logger = run_request("che tempo fa?", [{"text": "Non posso aiutarti con il meteo."}])
    assert not events(logger, "stale_data_nudge")


def test_history_for_next_turn_drops_old_tool_results():
    from devops_agent.agent.session import OLD_RESULT, history_for_next_turn

    messages = [
        {"role": "user", "content": "elenco"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "1", "name": "list_requests", "content": '{"items": [1, 2]}'},
        {"role": "assistant", "content": "Ecco l'elenco"},
    ]
    kept = history_for_next_turn(messages, 4)
    assert kept[2]["content"] == OLD_RESULT and kept[3]["content"] == "Ecco l'elenco"
    assert messages[2]["content"] == '{"items": [1, 2]}'  # l'originale non viene modificato


# --- Sessione del 26/10: limite scelto dal modello, filtro tolto ma descritto come applicato --------

LIST_L = ToolSpec(
    "list_requests",
    "elenca",
    {
        "type": "object",
        "properties": {
            "priority": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        },
    },
    is_write=False,
)


def test_limit_not_requested_is_widened_to_the_maximum():
    _, executor, logger = run_request(
        "mostrami l'elenco delle richieste in coda",
        [call("list_requests", {"limit": 10}), {"text": "ok"}],
        tools=(LIST_L,),
    )
    assert executor.calls == [("list_requests", {"limit": 50})]
    assert "limit 10 → 50" in events(logger, "tool_args_adjusted")[0]["adjustments"][0]


def test_limit_requested_by_the_user_is_kept():
    for request in ("mostrami le prime 5 richieste", "dammi 3 richieste aperte"):
        _, executor, _ = run_request(
            request, [call("list_requests", {"limit": 5}), {"text": "ok"}], tools=(LIST_L,)
        )
        assert executor.calls == [("list_requests", {"limit": 5})]


def test_dropped_filter_is_declared_in_the_answer():
    result, executor, _ = run_request(
        "Mostrami l'elenco delle richieste",
        [
            call("list_requests", {"priority": "critical", "limit": 50}),
            {"text": "Ecco le richieste critiche."},
        ],
        tools=(LIST_L,),
    )
    assert executor.calls == [("list_requests", {"limit": 50})]
    assert "il filtro priority=critical" in result.answer and "tutte le priorità" in result.answer
