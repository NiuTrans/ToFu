"""Task-level Tool Search disclosure continuity."""

from __future__ import annotations

import json

import pytest

from lib.tools.disclosure_state import (
    TOOL_DISCLOSURE_STATE_MAXIMUM,
    disclosed_entries,
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


def test_ledger_carries_bounded_argument_signature_without_values():
    tool = _tool("mcp__llm__experiment_run", "ids")
    tool["function"]["parameters"] = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "default": ["sentinel-secret"]},
            "dry_run": {"type": "boolean"},
        },
        "required": ["ids"],
    }
    task: dict = {}
    record_search_items(
        task,
        [{
            "name": "mcp__llm__experiment_run",
            "arguments_schema": {"type": "object", "properties": {}},
        }],
        catalog=[tool],
    )

    entries = disclosed_entries(task)
    assert [row["name"] for row in entries] == ["mcp__llm__experiment_run"]
    assert entries[0]["signature"] == {
        "required": ["ids"], "optional": ["dry_run"]}
    assert "sentinel-secret" not in json.dumps(task["_toolDisclosureState"])


def test_argument_signature_is_bounded_and_never_demotes_required():
    schema = {
        "type": "object",
        "properties": {
            f"arg_{index}": {"type": "string"} for index in range(40)},
        "required": [f"arg_{index}" for index in range(20)],
    }
    task: dict = {}
    record_search_items(
        task, [{"name": "tool_wide", "arguments_schema": schema}])

    signature = disclosed_entries(task)[0]["signature"]
    assert len(signature["required"]) == 16
    assert len(signature["optional"]) == 16
    assert "arg_16" not in signature["optional"]
    assert "arg_19" not in signature["optional"]
    assert signature["optional"][0] == "arg_20"


@pytest.mark.parametrize('gateway', ['execute_program', 'execute_tools'])
def test_search_receipt_uses_actual_visible_execution_gateway(monkeypatch, gateway):
    from lib.tasks_pkg.handlers import tool_gateway as handler
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {'id': 'gateway-' + gateway, '_executable_tool_catalog': [_tool('hidden_reader', 'path')],
            '_tool_schema': [_tool('execute_tools', 'calls')],
            'events': [{'type': 'tool_wire_projection', 'toolNames': ['search_tools', gateway]}]}
    first = _run_search(task, 'hidden_reader')
    repeated = _run_search(task, 'hidden_reader')
    assert first['execute_with'] == repeated['execute_with'] == gateway
    assert gateway in first['notice'] and gateway in repeated['notice']
    if gateway == 'execute_program':
        assert 'tools.invoke' in first['notice'] and 'tools.invoke' in repeated['notice']
        assert 'execute_tools' not in first['notice'] + repeated['notice']


def test_search_failure_restores_catalog_with_visible_program_hint(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    def unavailable(*args, **kwargs): raise RuntimeError('injected retrieval failure')
    monkeypatch.setattr(handler, 'search_executable_catalog', unavailable)
    original = _tool('hidden_reader', 'path')
    task = {'id': 'fallback-program', '_executable_tool_catalog': [original],
            'events': [{'type': 'tool_wire_projection',
                        'toolNames': ['search_tools', 'execute_program']}]}
    result = _run_search(task, 'hidden_reader')
    assert result['fail_open'] and result['execute_with'] == 'execute_program'
    assert [item['name'] for item in result['items']] == ['hidden_reader']
    assert 'tools.invoke' in result['notice'] and 'execute_tools' not in result['notice']
    assert task['_executable_tool_catalog'] == [original]


def test_ambiguous_program_search_retains_full_names_and_invocation_hint():
    from lib.tools.gateway import search_executable_catalog
    result = search_executable_catalog([
        _tool('mcp__first__inspect_record', 'id'),
        _tool('mcp__second__inspect_record', 'id')],
        'inspect_record', execution_gateway='execute_program')
    assert result['ambiguous_names'] == ['mcp__first__inspect_record', 'mcp__second__inspect_record']
    assert 'tools.invoke' in result['notice']
    assert 'execute_tools' not in result['notice']


def test_unique_short_name_program_search_uses_visible_invocation_hint():
    from lib.tools.gateway import search_executable_catalog
    result = search_executable_catalog([_tool('mcp__first__inspect_record', 'id')],
        'inspect_record', execution_gateway='execute_program')
    assert result['resolved_name'] == 'mcp__first__inspect_record'
    assert 'uniquely matches' in result['notice']
    assert 'tools.invoke' in result['notice'] and 'execute_tools' not in result['notice']
