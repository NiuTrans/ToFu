"""Contract-owned tool attention and presentation-parent edges."""

from __future__ import annotations

import json
import threading

import pytest

from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
from lib.conversation_sync.validation import ContractViolation, decode
from lib.tools.contracts import adapt_legacy_tool_contract


pytestmark = pytest.mark.unit


def _schema(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_conversation_contract_bounds_attention_and_parent_edge():
    value = {
        "toolCallId": "child-1",
        "attentionKind": "routine",
        "parentToolCallId": "parent-1",
    }
    assert decode("TurnToolRound", value) == value
    with pytest.raises(ContractViolation):
        decode("TurnToolRound", {**value, "attentionKind": "hidden"})
    with pytest.raises(ContractViolation):
        decode("TurnToolRound", {**value, "parentToolCallId": "x" * 257})


def test_round_attention_comes_from_request_contract_and_fails_visible():
    read_document = adapt_legacy_tool_contract(
        _schema("observe"), permission="read").search_document()
    write_document = adapt_legacy_tool_contract(
        _schema("mutate"), permission="write",
        idempotency="non_idempotent").search_document()
    task = {"_toolContractDocumentsByName": {
        "observe": read_document,
        "mutate": write_document,
    }}

    _, read_round, read_event = _build_tool_round_entry(
        "observe", {}, "read-1", "{}", 0, False, task=task)
    _, write_round, write_event = _build_tool_round_entry(
        "mutate", {}, "write-1", "{}", 0, False, task=task)
    _, unknown_round, _ = _build_tool_round_entry(
        "future_tool", {}, "unknown-1", "{}", 0, False, task=task)

    assert read_round["attentionKind"] == read_event["attentionKind"] == "routine"
    assert write_round["attentionKind"] == write_event["attentionKind"] == "important"
    assert unknown_round["attentionKind"] == "important"


def test_local_program_child_parent_edge_reaches_round_and_start_event(
    monkeypatch,
):
    schema = _schema("observe")
    document = adapt_legacy_tool_contract(schema).search_document()
    task = {
        "id": "task-attention", "convId": "conv-attention", "model": "test",
        "events": [], "events_lock": threading.Lock(), "toolRounds": [],
        "aborted": False, "_tool_schema": [schema],
        "_executable_tool_catalog": [schema],
        "_toolContractDocumentsByName": {"observe": document},
    }
    assistant = {"content": "", "tool_calls": [{
        "id": "child-1", "type": "function", "source": "execute_program",
        "_presentationParentToolCallId": "program-1",
        "function": {"name": "observe", "arguments": json.dumps({})},
    }]}
    emitted: list[dict] = []
    monkeypatch.setattr(
        "lib.tasks_pkg.tool_dispatch._parse.append_event",
        lambda _task, event: emitted.append(event))

    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert parsed[0][5]["parentToolCallId"] == "program-1"
    start = next(event for event in emitted
                 if event.get("type") == "tool_start")
    assert start["parentToolCallId"] == "program-1"
