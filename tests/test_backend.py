"""Test dei servizi applicativi, senza MCP e senza modello."""

from __future__ import annotations

import sqlite3

import pytest

from devops_agent.backend import services
from devops_agent.backend.database import init_db
from devops_agent.backend.models import DomainError


def test_seed_is_deterministic(tmp_path):
    rows = []
    for name in ("a.db", "b.db"):
        conn = sqlite3.connect(init_db(tmp_path / name, seed=42))
        rows.append(conn.execute("SELECT * FROM operational_request ORDER BY id").fetchall())
        conn.close()
    assert rows[0] == rows[1]
    assert len(rows[0]) == 30


def test_only_one_critical_open_request(conn):
    result = services.list_requests(conn, status="open", priority="critical")
    assert [r.id for r in result.items] == [1]


def test_list_requests_orders_by_priority_and_reports_truncation(conn):
    result = services.list_requests(conn, limit=5)
    ranks = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    assert [ranks[r.priority] for r in result.items] == sorted(
        (ranks[r.priority] for r in result.items), reverse=True
    )
    assert result.truncated and result.total_matching == 30


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"limit": 0}, "INVALID_ARGUMENT"),
        ({"limit": 51}, "INVALID_ARGUMENT"),
        ({"team_id": 99}, "NOT_FOUND"),
        ({"team_id": 1, "unassigned_only": True}, "INVALID_ARGUMENT"),
    ],
)
def test_list_requests_rejects_invalid_filters(conn, kwargs, code):
    with pytest.raises(DomainError) as exc:
        services.list_requests(conn, **kwargs)
    assert exc.value.code == code


def test_ambiguous_service_name_exists_in_two_environments(conn):
    result = services.list_services(conn, name_contains="billing")
    assert {(s.name, s.environment) for s in result.items} == {
        ("billing-api", "prod"),
        ("billing-api", "staging"),
    }


def test_pipeline_status_returns_last_run(conn):
    status = services.get_pipeline_status(conn, 3, "deploy")
    assert status.pipelines[0].status == "failed"
    assert "readiness probe" in status.pipelines[0].summary


def test_pipeline_status_unknown_pipeline(conn):
    with pytest.raises(DomainError) as exc:
        services.get_pipeline_status(conn, 3, "release")
    assert exc.value.code == "NOT_FOUND" and "build, deploy" in exc.value.message


def test_create_request_writes_history(conn):
    created = services.create_request(
        conn, title="Nuova richiesta di prova", description="d", priority="high", service_id=1, actor="test"
    )
    assert created.status == "open" and created.team_id is None
    assert [(h.event, h.actor) for h in created.history] == [("created", "test")]


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"title": "abc"}, "INVALID_ARGUMENT"),
        ({"priority": "urgentissima"}, "INVALID_ARGUMENT"),
        ({"service_id": 999}, "NOT_FOUND"),
        ({"description": "x" * 2001}, "INVALID_ARGUMENT"),
    ],
)
def test_create_request_validation(conn, kwargs, code):
    args = {"title": "Titolo valido", "description": "", "priority": "low", "service_id": 1, "actor": "t"}
    count = conn.execute("SELECT COUNT(*) FROM operational_request").fetchone()[0]
    with pytest.raises(DomainError) as exc:
        services.create_request(conn, **{**args, **kwargs})
    assert exc.value.code == code
    assert conn.execute("SELECT COUNT(*) FROM operational_request").fetchone()[0] == count


def test_assign_open_request_moves_it_in_progress(conn):
    updated = services.assign_request(conn, request_id=2, team_id=4, actor="test")
    assert (updated.team_id, updated.status) == (4, "in_progress")
    assert [h.event for h in updated.history][-2:] == ["assigned", "status_changed"]


@pytest.mark.parametrize(
    "request_id, team_id, code",
    [
        (9999, 1, "NOT_FOUND"),
        (2, 99, "NOT_FOUND"),
        (6, 1, "CONFLICT"),  # risolta
        (3, 4, "CONFLICT"),  # già assegnata allo stesso team
    ],
)
def test_assign_request_rules(conn, request_id, team_id, code):
    with pytest.raises(DomainError) as exc:
        services.assign_request(conn, request_id=request_id, team_id=team_id, actor="t")
    assert exc.value.code == code


def test_schema_constraints_reject_invalid_rows(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operational_request (service_id, title, status, priority, created_at, "
            "updated_at) VALUES (1, 'Titolo ok', 'aperta', 'low', 'x', 'x')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operational_request (service_id, title, status, priority, created_at, "
            "updated_at) VALUES (999, 'Titolo ok', 'open', 'low', 'x', 'x')"
        )


def test_list_requests_order_by_id_desc(conn):
    ids = [r.id for r in services.list_requests(conn, status="open", order_by="id_desc", limit=50).items]
    assert ids == sorted(ids, reverse=True)
    with pytest.raises(DomainError):
        services.list_requests(conn, order_by="casuale")


def test_resolved_request_leaves_the_open_queue(conn):
    updated = services.update_request_status(
        conn, request_id=2, status="resolved", note="Risolta dal team Cloud Security", actor="t"
    )
    assert updated.status == "resolved"
    last = updated.history[-1]
    assert (last.event, last.old_value, last.new_value, last.note) == (
        "status_changed",
        "open",
        "resolved",
        "Risolta dal team Cloud Security",
    )
    assert 2 not in [r.id for r in services.list_requests(conn, status="open", limit=50).items]


@pytest.mark.parametrize(
    "request_id, status, note, code",
    [
        (9999, "resolved", "nota valida", "NOT_FOUND"),
        (2, "open", "nota valida", "CONFLICT"),  # già open
        (7, "open", "nota valida", "CONFLICT"),  # closed è finale
        (2, "resolved", "ok", "INVALID_ARGUMENT"),  # nota troppo corta
        (2, "archiviata", "nota valida", "INVALID_ARGUMENT"),
    ],
)
def test_update_request_status_rules(conn, request_id, status, note, code):
    with pytest.raises(DomainError) as exc:
        services.update_request_status(conn, request_id=request_id, status=status, note=note, actor="t")
    assert exc.value.code == code


def test_matching_rules_on_seed_data(conn):
    from devops_agent.agent.matching import match_services, match_teams

    svcs = [s.model_dump() for s in services.list_services(conn).items]
    teams = [t.model_dump() for t in services.list_teams(conn).items]
    ids = lambda items: [i["id"] for i in items]  # noqa: E731
    assert ids(match_services("payments-api in produzione", svcs)) == [1]
    assert ids(match_services("notification-worker in dev", svcs)) == [8]
    assert ids(match_services("billing-api, fatture duplicate", svcs)) == [6, 7]  # ambiguo: nessuna scelta
    assert ids(match_services("un servizio qualsiasi", svcs)) == []
    assert ids(match_teams("il team che si occupa di certificati e sicurezza", teams)) == [4]
    assert ids(match_teams("al team CI/CD Enablement", teams)) == [2]
