"""Policy dell'harness e scenari end-to-end con provider scripted e server MCP reale."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from devops_agent.agent.policy import Policy, RemoteTool
from devops_agent.config import Config, PolicyConfig, load_config
from devops_agent.evaluation.runner import run_scenarios

SCENARIO_DIR = Path(__file__).parent / "scenarios"


def _remote(*names: str) -> list[RemoteTool]:
    return [RemoteTool(n, "", {"type": "object"}) for n in names]


def test_policy_default_deny_and_roles():
    viewer = Policy(PolicyConfig(), "viewer")
    tools, excluded = viewer.build_catalog(_remote("get_request", "create_request", "drop_database"))
    assert [t.name for t in tools] == ["get_request"]
    assert excluded == {
        "create_request": "non consentito al ruolo viewer",
        "drop_database": "non classificato in configurazione",
    }
    operator_tools, _ = Policy(PolicyConfig(), "operator").build_catalog(_remote("create_request"))
    assert operator_tools[0].is_write


def test_policy_rejects_inconsistent_configuration():
    with pytest.raises(ValueError):
        Policy(PolicyConfig(read_tools=["x"], write_tools=["x"], roles={"r": ["x"]}), "r")
    with pytest.raises(ValueError):
        Policy(PolicyConfig(), "admin")


def test_example_config_is_valid():
    cfg = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert cfg.agent.role in cfg.policy.roles


def test_all_scenarios_pass_with_scripted_provider(tmp_path):
    base = Config()
    cfg = replace(
        base,
        model=replace(base.model, provider="scripted"),
        paths=replace(base.paths, logs=str(tmp_path / "logs")),
    )
    summary = asyncio.run(run_scenarios(cfg, scenario_dir=SCENARIO_DIR, results_root=tmp_path))
    failed = {r.scenario_id: r.failures for r in summary.records if not r.passed}
    assert not failed, failed
    assert len(summary.records) >= 10
    categories = {r.scenario_id for r in summary.records}
    assert {
        "TST-03",
        "TST-06",
        "TST-08",
        "TST-09",
        "TST-10",
    } <= categories  # anomalie richieste dalla traccia


def test_regression_scenarios_pass_with_recorded_answers(tmp_path):
    # TST-19 e TST-20 riproducono risposte sbagliate registrate nell'esecuzione 2, che il modello attuale
    # non dà più: verificano due difese dell'harness (chiamata scritta come testo, successo dichiarato
    # senza scrittura) con il server MCP reale, solo con le risposte registrate.
    base = Config()
    cfg = replace(
        base,
        model=replace(base.model, provider="scripted"),
        paths=replace(base.paths, logs=str(tmp_path / "logs")),
    )
    summary = asyncio.run(
        run_scenarios(cfg, scenario_dir=SCENARIO_DIR / "regressione", results_root=tmp_path)
    )
    assert {r.scenario_id for r in summary.records} == {"TST-19", "TST-20"}
    assert all(r.passed for r in summary.records), [r.failures for r in summary.records]


def test_model_scenarios_run_with_every_provider():
    from devops_agent.evaluation.scenarios import load_scenarios

    scenarios = load_scenarios(SCENARIO_DIR)
    assert len(scenarios) == 18 and all(not s.providers for s in scenarios)


def _recorded_answer_failures(scenario_id: str, answer: str) -> list[str]:
    """Valuta solo i controlli sulla risposta, con una risposta registrata nell'esecuzione 4."""
    from dataclasses import replace

    from devops_agent.agent.types import RunResult
    from devops_agent.evaluation.scenarios import Expectation, evaluate, load_scenarios

    sc = next(s for s in load_scenarios(SCENARIO_DIR) if s.id == scenario_id)
    only_answer = Expectation(
        stop_reason=["final_answer"],
        answer_any=sc.expect.answer_any,
        answer_all=sc.expect.answer_all,
        answer_none=sc.expect.answer_none,
    )
    result = RunResult(answer, "final_answer", 1, 1, None, None, 0, [], [])
    return evaluate(replace(sc, expect=only_answer), result, [], [], [])


def test_recorded_false_retry_promise_fails_tst10():
    answer = "Mi dispiace, … ho ricevuto un errore interno del server. Riprovo subito."
    assert any("non ammessi" in f for f in _recorded_answer_failures("TST-10", answer))


def test_recorded_wrong_total_fails_tst11():
    answer = "Ecco le prime 200 richieste registrate:\n\n1. Deploy customer-portal in errore su prod"
    failures = _recorded_answer_failures("TST-11", answer)
    assert any("non contiene: 30" in f for f in failures)
    assert any("non ammessi" in f for f in failures)


def test_scripted_only_events_are_ignored_for_a_real_model():
    from devops_agent.agent.types import RunResult
    from devops_agent.evaluation.scenarios import Expectation, Scenario, evaluate

    sc = Scenario(
        id="X",
        title="x",
        category="x",
        request="x",
        expected="x",
        expect=Expectation(stop_reason=["final_answer"], events_present_scripted=["harness_nudge"]),
    )
    result = RunResult("ok", "final_answer", 1, 1, None, None, 0, [], [])
    assert evaluate(sc, result, [], [], [], "ollama") == []
    assert evaluate(sc, result, [], [], [], "scripted") == [
        "evento atteso nel log (scripted) non presente: harness_nudge"
    ]


def test_recorded_correct_unavailability_answer_passes_tst10():
    # Esecuzione 6: risposta corretta rifiutata dal vecchio frammento "non disponibile".
    answer = "Mi dispiace, il servizio non è attualmente disponibile. Ti consiglio di riprovare più tardi."
    assert _recorded_answer_failures("TST-10", answer) == []
