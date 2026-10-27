"""Test del server MCP reale, avviato come processo e interrogato via stdio."""

from __future__ import annotations

import asyncio
import json

from devops_agent.agent.mcp_client import McpToolExecutor, parse_error

EXPECTED_TOOLS = {
    "list_services",
    "list_teams",
    "list_requests",
    "get_request",
    "get_pipeline_status",
    "create_request",
    "assign_request",
    "update_request_status",
}


def _run(db_path, coro_factory, **kwargs):
    async def main():
        async with McpToolExecutor(db_path, actor="pytest", **kwargs) as executor:
            return await coro_factory(executor)

    return asyncio.run(main())


def test_server_exposes_eight_typed_tools(db_path):
    tools = _run(db_path, lambda ex: ex.list_tools())
    by_name = {t.name: t for t in tools}
    assert set(by_name) == EXPECTED_TOOLS
    priority = by_name["create_request"].input_schema["properties"]["priority"]
    assert priority["enum"] == ["low", "medium", "high", "critical"]
    assert by_name["list_requests"].input_schema["properties"]["limit"]["maximum"] == 50
    assert all(t.description for t in tools)


def test_read_tool_returns_compact_json(db_path):
    outcome = _run(db_path, lambda ex: ex.call_tool("get_request", {"request_id": 3}))
    assert outcome.ok
    data = json.loads(outcome.content)
    assert data["team_name"] == "Cloud Security" and "\n" not in outcome.content


def test_domain_error_is_mapped_to_code(db_path):
    outcome = _run(db_path, lambda ex: ex.call_tool("get_request", {"request_id": 9999}))
    assert (outcome.ok, outcome.error_code) == (False, "NOT_FOUND")


def test_server_side_validation_still_applies(db_path):
    outcome = _run(db_path, lambda ex: ex.call_tool("list_requests", {"limit": 500}))
    assert (outcome.ok, outcome.error_code) == (False, "PARAMETRI_NON_VALIDI")


def test_write_tool_records_actor(db_path):
    outcome = _run(
        db_path,
        lambda ex: ex.call_tool(
            "create_request",
            {"title": "Creata dal test MCP", "description": "", "priority": "low", "service_id": 2},
        ),
    )
    assert outcome.ok
    assert json.loads(outcome.content)["history"][0]["actor"] == "pytest"


def test_simulated_fault_is_reported_as_tool_failure(db_path):
    outcome = _run(db_path, lambda ex: ex.call_tool("list_teams", {}), fault_tools=["list_teams"])
    assert (outcome.ok, outcome.error_code) == (False, "TOOL_FALLITO")


def test_parse_error_variants():
    assert parse_error("Unknown tool: x")[0] == "TOOL_NON_DISPONIBILE"
    assert parse_error("Error executing tool t")[0] == "TOOL_FALLITO"
    assert parse_error("Error executing tool t: CONFLICT: già assegnata") == ("CONFLICT", "già assegnata")


def test_update_request_status_through_server(db_path):
    outcome = _run(
        db_path,
        lambda ex: ex.call_tool(
            "update_request_status", {"request_id": 2, "status": "resolved", "note": "Certificato rinnovato"}
        ),
    )
    assert outcome.ok
    data = json.loads(outcome.content)
    assert data["status"] == "resolved"
    assert data["history"][-1]["note"] == "Certificato rinnovato"


def test_order_by_is_exposed_as_enum(db_path):
    tools = {t.name: t for t in _run(db_path, lambda ex: ex.list_tools())}
    assert tools["list_requests"].input_schema["properties"]["order_by"]["enum"] == [
        "priority",
        "newest",
        "oldest",
        "id_desc",
        "id_asc",
    ]
