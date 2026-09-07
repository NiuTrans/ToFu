"""Task-level Tool Search disclosure continuity."""

from __future__ import annotations

import json

import pytest

from lib.tools.disclosure_state import (
    TOOL_DISCLOSURE_STATE_MAXIMUM,
    disclosed_names_for_catalog,
    record_search_items,
)

pytestmark = pytest.mark.unit


def _tool(name: str, field: str = "doc") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Read {field}",
            "parameters": {
                "type": "object",
                "properties": {field: {"type": "string"}},
                "required": [field],
            },
        },
    }


def _run_search(task: dict, query: str) -> dict:
    from lib.tasks_pkg.handlers import tool_gateway as handler

    _call_id, content, aborted = handler.handle_search_tools(
        task, {}, "search_tools", "call-search", {"query": query},
        1, {"llmRound": 1}, {}, None, False,
    )
    assert aborted is False
    return json.loads(content)


def test_later_search_suppresses_same_schema_but_revised_schema_returns(
    monkeypatch,
):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, "_finalize", lambda *args, **kwargs: None)
    original = _tool("mcp__xuecheng__get_doc_meta", "doc")
    task = {
        "id": "task-disclosure",
        "_executable_tool_catalog": [original],
        "_executableToolNamespaceByName": {},
    }

    first = _run_search(task, "xuecheng document metadata")
    assert [row["name"] for row in first["items"]] == [
        "mcp__xuecheng__get_doc_meta"
    ]

    second = _run_search(task, "document metadata owner")
    assert second["items"] == []
    assert disclosed_names_for_catalog(task, [original]) == {
        "mcp__xuecheng__get_doc_meta"
    }

    revised = _tool("mcp__xuecheng__get_doc_meta", "document_id")
    task["_executable_tool_catalog"] = [revised]
    third = _run_search(task, "xuecheng document metadata")
    assert [row["name"] for row in third["items"]] == [
        "mcp__xuecheng__get_doc_meta"
    ]


def test_state_is_bounded_without_raw_schema():
    task: dict = {}
    for index in range(TOOL_DISCLOSURE_STATE_MAXIMUM + 12):
        record_search_items(task, [{
            "name": f"tool_{index}",
            "arguments_schema": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        }])

    state = task["_toolDisclosureState"]
    assert len(state["entries"]) == TOOL_DISCLOSURE_STATE_MAXIMUM
    assert state["entries"][0]["name"] == "tool_12"
    assert "arguments_schema" not in json.dumps(state)


def test_record_uses_catalog_identity_when_result_schema_is_compacted():
    tool = _tool("mcp__xuecheng__get_doc_meta", "doc")
    task: dict = {}
    record_search_items(
        task,
        [{
            "name": "mcp__xuecheng__get_doc_meta",
            "arguments_schema": {"type": "object", "properties": {}},
        }],
        catalog=[tool],
    )

    assert disclosed_names_for_catalog(task, [tool]) == {
        "mcp__xuecheng__get_doc_meta"
    }
