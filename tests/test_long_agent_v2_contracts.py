"""Executable contracts for the opt-in long-agent v2 comparison stack."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from lib.tools.result_envelope import model_text_from_tool_result


pytestmark = pytest.mark.unit


def _context_block(block_id: str, content: str, *, required: bool = False,
                   layer: str = "cold_history", stability: str = "turn",
                   permissions: frozenset[str] = frozenset()):
    from lib.tasks_pkg.context_composer import ContextBlock

    return ContextBlock(
        id=block_id, source=f"test.{block_id}", content=content,
        authority="workflow" if required else "evidence",
        placement="tail", stability=stability, lifecycle="task",
        priority=10, required=required, layer=layer,
        required_permissions=permissions,
        recovery_handle=f"artifact:{block_id}",
    )


def test_context_plan_locks_required_blocks_and_is_deterministic():
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context

    task: dict = {}
    request = ComposeRequest(
        model="kimi-k3", global_budget_tokens=100,
        base_context_tokens=100, task=task,
    )
    blocks = [
        _context_block("objective", "must preserve this objective",
                       required=True, layer="objective_constraints",
                       stability="static"),
        _context_block("cold", "recoverable history " * 500),
    ]
    first = render_context(
        [{"role": "user", "content": "do the work"}], blocks, request)
    second = render_context(
        [{"role": "user", "content": "do the work"}], blocks, request)

    assert first.plan is not None
    assert first.plan == second.plan
    entries = {entry.id: entry for entry in first.plan.entries}
    assert entries["objective"].selected is True
    assert entries["cold"].selected is False
    assert entries["cold"].reason == "global_budget_exhausted"
    assert first.plan.overflow_tokens == first.plan.selected_tokens
    assert first.plan.segment_hashes["staticPrefix"] == (
        second.plan.segment_hashes["staticPrefix"])


def test_context_plan_permissions_and_cache_epoch_are_explicit():
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context

    denied = _context_block(
        "private", "secret", permissions=frozenset({"project.read"}))
    result = render_context(
        [{"role": "user", "content": "request"}], [denied],
        ComposeRequest(global_budget_tokens=1_000,
                       granted_permissions=frozenset()),
    )
    assert result.manifest[0]["reason"] == "permission_denied:project.read"
    assert result.plan.entries[0].selected is False

    task: dict = {}
    first = render_context(
        [{"role": "user", "content": "request"}], [],
        ComposeRequest(global_budget_tokens=1_000, task=task,
                       tool_names=frozenset({"read_files"})),
    )
    second = render_context(
        [{"role": "user", "content": "request"}], [],
        ComposeRequest(global_budget_tokens=1_000, task=task,
                       tool_names=frozenset({"read_files", "run_command"})),
    )
    assert first.plan.cache_epoch == 0
    assert second.plan.cache_epoch == 1


def test_context_block_limit_and_tool_manifest_hash_are_hard_boundaries():
    from lib.tasks_pkg.context_composer import ComposeRequest, render_context
    from lib.tasks_pkg.context_composer._render import _count_tokens, _truncate

    text, truncated = _truncate("甲乙丙丁" * 2_000, 10, "")
    assert truncated is True
    assert _count_tokens(text, "") <= 10

    task = {"_tool_schema": [{"type": "function", "function": {
        "name": "read_files", "description": "first",
        "parameters": {"type": "object", "properties": {}},
    }}]}
    first = render_context(
        [{"role": "user", "content": "request"}], [],
        ComposeRequest(global_budget_tokens=1_000, task=task,
                       tool_names=frozenset({"read_files"})),
    )
    task["_tool_schema"][0]["function"]["description"] = "changed"
    second = render_context(
        [{"role": "user", "content": "request"}], [],
        ComposeRequest(global_budget_tokens=1_000, task=task,
                       tool_names=frozenset({"read_files"})),
    )
    assert first.plan.segment_hashes["toolManifest"] != (
        second.plan.segment_hashes["toolManifest"])
    assert second.plan.cache_epoch == first.plan.cache_epoch + 1


def test_task_state_is_rebuildable_and_does_not_trust_completion_prose():
    from lib.tasks_pkg.context_composer.task_state import (
        derive_task_state_snapshot,
    )

    messages = [
        {"role": "user", "content": "Fix it; tests must pass."},
        {"role": "assistant", "content": "Everything is done."},
        {"role": "assistant", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "run_command",
                         "arguments": '{"command":"pytest -q"}'},
        }]},
        {"role": "tool", "name": "run_command", "status": "success",
         "tool_call_id": "call_1", "content": "3 passed"},
    ]
    snapshot = derive_task_state_snapshot(
        messages, {"_worldVersion": "git:abc", "_observedAtMs": 42})

    assert snapshot.goal.startswith("Fix it")
    assert snapshot.hard_constraints
    assert snapshot.tests == ("requested:pytest -q",)
    assert snapshot.completed_work == ("run_command:3 passed",)
    assert "Everything is done" not in snapshot.to_context_text()
    assert snapshot.world_version == "git:abc"


def test_tool_contract_compiles_search_help_and_validates_execution():
    from lib.tools.contracts import (
        ToolContractError,
        ToolContractV2,
        compile_execution_contract_documents,
        validate_tool_arguments_from_documents,
    )

    contract = ToolContractV2(
        name="read_sample", model_description="Read a sample.",
        detailed_help="Read a bounded sample by id.",
        search_metadata=("sample lookup", "读取样本"),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": 10, "default": 3},
            },
            "required": ["id"], "additionalProperties": False,
        },
        ptc_eligible=True,
        result_recovery="source",
    )
    assert contract.provider_schema()["function"]["description"] == "Read a sample."
    assert contract.search_document()["help"].startswith("Read a bounded")
    assert contract.search_document()["resultRecovery"] == "source"
    assert contract.validate_arguments({"id": "x"}) == {"id": "x", "limit": 3}
    documents = compile_execution_contract_documents(
        [contract.provider_schema()],
        authoritative_documents_by_name={
            contract.name: contract.search_document()})
    assert validate_tool_arguments_from_documents(
        documents, "read_sample", {"id": "x"}) == {
            "id": "x", "limit": 3}
    with pytest.raises(ToolContractError) as missing:
        contract.validate_arguments({})
    assert missing.value.code == "missing_required_arguments"
    with pytest.raises(ToolContractError) as extra:
        contract.validate_arguments({"id": "x", "unsafe": True})
    assert extra.value.code == "unknown_arguments"

    bounded_array = ToolContractV2(
        name="read_batch", model_description="Read a non-empty batch.",
        parameters={
            "type": "object",
            "properties": {
                "ids": {"type": "array", "minItems": 1, "maxItems": 2,
                        "items": {"type": "string", "minLength": 1}},
            },
            "required": ["ids"], "additionalProperties": False,
        },
    )
    with pytest.raises(ToolContractError) as empty:
        bounded_array.validate_arguments({"ids": []})
    assert empty.value.code == "too_few_items"

    bounded_text = ToolContractV2(
        name="caption_sample", model_description="Caption a sample.",
        parameters={
            "type": "object",
            "properties": {
                "caption": {"type": "string", "minLength": 2, "maxLength": 5},
            },
            "required": ["caption"],
        },
    )
    with pytest.raises(ToolContractError) as too_long:
        bounded_text.validate_arguments({"caption": "123456"})
    assert too_long.value.code == "invalid_argument_length"
    assert "(got 6 chars; allowed 2–5)" in str(too_long.value)
    with pytest.raises(ToolContractError) as too_short:
        bounded_text.validate_arguments({"caption": "x"})
    assert too_short.value.code == "invalid_argument_length"
    assert "(got 1 chars; allowed 2–5)" in str(too_short.value)


def test_tool_contract_json_compilation_preserves_request_isolation():
    from lib.tools.contracts import (
        ToolContractV2,
        compile_execution_contract_documents,
    )

    class _ExtensionDefault(dict):
        pass

    shared_enum = ["brief", "full"]
    extension_default = _ExtensionDefault(tags=["source"])
    parameters = {
        "type": "object",
        "properties": {
            "style": {"type": "string", "enum": shared_enum},
            "fallback_style": {"type": "string", "enum": shared_enum},
            "options": {
                "type": "object",
                "properties": {
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "default": extension_default,
            },
        },
    }
    contract = ToolContractV2(
        name="clone_contract", model_description="Clone a contract.",
        parameters=parameters)

    provider = contract.provider_schema()
    provider_properties = provider["function"]["parameters"]["properties"]
    assert provider_properties["style"]["enum"] is provider_properties[
        "fallback_style"]["enum"]
    assert provider_properties["style"]["enum"] is not shared_enum
    provider_default = provider_properties["options"]["default"]
    assert isinstance(provider_default, _ExtensionDefault)
    assert provider_default is not extension_default
    provider_properties["style"]["enum"].append("provider-only")
    assert shared_enum == ["brief", "full"]

    search_document = contract.search_document()
    assert "provider-only" not in search_document[
        "arguments_schema"]["properties"]["style"]["enum"]
    compiled = compile_execution_contract_documents(
        [contract.provider_schema()],
        authoritative_documents_by_name={contract.name: search_document})
    compiled[contract.name]["arguments_schema"]["properties"][
        "style"]["enum"].append("execution-only")
    assert "execution-only" not in search_document[
        "arguments_schema"]["properties"]["style"]["enum"]

    first_default = contract.validate_arguments({})["options"]
    second_default = contract.validate_arguments({})["options"]
    assert type(first_default) is dict
    first_default["tags"].append("first-request")
    assert second_default == {"tags": ["source"]}
    assert extension_default == {"tags": ["source"]}


def test_tool_result_v2_artifactizes_structural_and_token_truncation(monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget

    stored: list[str] = []

    def store(content, **_kwargs):
        stored.append(content)
        return "tool-result:" + "a" * 64

    monkeypatch.setattr(_budget, "_store_tool_result_artifact", store)
    raw = json.dumps(list(range(100)))
    result = json.loads(_budget.budget_tool_result_v2(
        "read_files", raw, user_id=7, model=""))

    assert result["contractVersion"] == "tofu.tool-result/v2"
    assert result["status"] == "partial"
    assert result["truncated"] is True
    assert len(result["items"]) == 64
    assert result["artifactRef"] == "tool-result:" + "a" * 64
    assert stored == [raw]
    assert result["visibleBytes"] == len(
        model_text_from_tool_result(json.dumps(result)).encode("utf-8"))

    huge = "evidence line\n" * 20_000
    visible = _budget.budget_tool_result_v2(
        "read_files", huge, user_id=7, model="")
    assert _budget._model_result_tokens(visible, "") <= 8_000
    assert json.loads(visible)["artifactRef"]


def test_tool_result_v2_delivery_keeps_harness_fields_out_of_messages():
    from lib.tools.result_envelope import (
        ToolResultEnvelopeV2,
        split_tool_result_delivery,
        tool_result_observation,
    )

    raw = 'grep "needle" (*.py) — 1 match:\n\nfile.py:1:needle'
    envelope = ToolResultEnvelopeV2.from_legacy(raw).to_envelope_text()
    delivery = split_tool_result_delivery(envelope)

    assert delivery.model_text == raw
    assert delivery.evidence == {
        "contractVersion": "tofu.tool-result-evidence/v1",
        "resultContractVersion": "tofu.tool-result/v2",
        "status": "ok",
        "projectionKind": "text",
        "truncated": False,
        "rawBytes": len(raw.encode("utf-8")),
        "visibleBytes": len(raw.encode("utf-8")),
        "envelopeBytes": len(envelope.encode("utf-8")),
        "evidenceId": json.loads(envelope)["evidenceId"],
    }
    assert "contractVersion" not in delivery.model_text
    observation = tool_result_observation(
        delivery.model_text, delivery.evidence)
    assert observation is not None
    assert observation["summary"] == raw
    assert observation["evidenceId"] == delivery.evidence["evidenceId"]


def test_tool_result_v2_partial_and_error_projections_are_sparse():
    from lib.tools.result_envelope import (
        ToolResultEnvelopeV2,
        ToolResultRecoveryV2,
        split_tool_result_delivery,
        tool_result_error,
        typed_tool_error,
    )

    partial = ToolResultEnvelopeV2(
        status="partial",
        summary="bounded grep preview",
        truncated=True,
        raw_bytes=20_000,
        evidence_id="ev_partial",
        recovery=ToolResultRecoveryV2(
            kind="source", tool="grep_search",
            arguments={"pattern": "needle", "max_results": 20}),
    ).with_visible_bytes()
    partial_delivery = split_tool_result_delivery(partial.to_envelope_text())
    assert json.loads(partial_delivery.model_text) == {
        "recovery": {
            "arguments": {"max_results": 20, "pattern": "needle"},
            "kind": "source",
            "tool": "grep_search",
        },
        "status": "partial",
        "summary": "bounded grep preview",
        "truncated": True,
    }
    assert "freshness" not in partial_delivery.model_text
    assert "rawBytes" not in partial_delivery.model_text

    error_delivery = split_tool_result_delivery(typed_tool_error(
        "permission_denied", retryable=False,
        next_action="Choose an allowed tool.",
        message="This action is unavailable.",
    ).to_envelope_text())
    assert json.loads(error_delivery.model_text) == {
        "code": "permission_denied",
        "message": "This action is unavailable.",
        "nextAction": "Choose an allowed tool.",
        "retryable": False,
        "status": "error",
    }
    assert tool_result_error(error_delivery.model_text).code == \
        "permission_denied"


def test_grep_producer_truncation_becomes_partial_source_recovery(monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget
    from lib.tasks_pkg.handlers.project import _record_producer_result_metadata
    from lib.tasks_pkg.tool_dispatch import _pipeline

    raw = (
        'grep "needle" (*.py) — 40 matches:\n\nfile.py:1:needle\n'
        '… (output truncated at 20000 chars or 40 matches)')
    round_entry = {"query": "grep_search: needle", "llmRound": 2}
    _record_producer_result_metadata("grep_search", raw, round_entry)
    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "an already-truncated grep string is not a recovery artifact"))
    monkeypatch.setattr(_pipeline, "append_event", lambda *_args, **_kwargs: None)
    task = {
        "id": "grep-partial-task",
        "model": "",
        "convId": "grep-partial-conv",
        "config": {"tools": {"resultEnvelope": "v2"}},
        "_userId": 7,
    }
    visible = _pipeline._settle_tool_result(
        task, "grep_search", "grep_call", {"pattern": "needle"},
        1, round_entry, raw, idempotent_tools=frozenset(), cache={},
        tid="grep-par", round_num=2)

    value = json.loads(visible)
    assert value["status"] == "partial"
    assert value["truncated"] is True
    assert value["recovery"] == {
        "arguments": {"pattern": "needle"},
        "kind": "source",
        "tool": "grep_search",
    }
    assert "artifactRef" not in value
    assert round_entry["toolResultEvidence"]["truncated"] is True
    assert round_entry["toolResultEvidence"]["status"] == "partial"


def test_streaming_grep_truncation_sidecar_survives_prefetch_cache(
        tmp_path, monkeypatch):
    from concurrent.futures import Future
    import time

    import lib.project_mod.tools as project_tools
    import lib.tasks_pkg.compaction._budget as _budget
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline

    raw = (
        'grep "needle" (*.py) — 40 matches:\n\nfile.py:1:needle\n'
        '… (output truncated at 20000 chars or 40 matches)')
    monkeypatch.setattr(
        project_tools, "execute_tool", lambda *_args, **_kwargs: raw)
    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "prefetched producer truncation must recover from its source"))
    arguments = {"pattern": "needle"}
    task = {
        "id": "grep-prefetch-task",
        "convId": "grep-prefetch-conv",
        "_userId": 7,
        "messages": [],
        "toolRounds": [],
        "events": [],
        "events_lock": threading.Lock(),
        "status": "running",
        "aborted": False,
        "model": "test-model",
        "config": {"tools": {"resultEnvelope": "v2"}},
        "_tool_result_cache": {},
    }
    accumulator = StreamingToolAccumulator(
        task, str(tmp_path), project_enabled=True)
    try:
        preexecuted = accumulator._execute_one("grep_search", arguments)
        future = Future()
        future.set_result(preexecuted)
        accumulator._futures["grep-prefetch-call"] = (
            future, "grep_search", arguments, time.time())
        assert accumulator.inject_into_cache(task) == 1
    finally:
        accumulator._pool.shutdown(wait=False)

    cache_key = _make_cache_key("grep_search", arguments)
    cached = task["_tool_result_cache"][cache_key]
    assert len(cached) == 9
    assert cached[8] == {"status": "partial", "truncated": True}

    round_entry = {
        "roundNum": 1,
        "toolCallId": "grep-prefetch-call",
        "toolName": "grep_search",
        "query": "grep_search",
        "status": "searching",
    }
    parsed = [(
        {"id": "grep-prefetch-call"}, "grep_search",
        "grep-prefetch-call", arguments, 1, round_entry, None,
    )]
    messages: list[dict] = []
    execute_tool_pipeline(
        task, parsed, {}, str(tmp_path), True, None, messages, [], 1,
        "test-model")

    model_result = json.loads(next(
        message["content"] for message in messages
        if message.get("role") == "tool"))
    assert model_result["status"] == "partial"
    assert model_result["recovery"]["tool"] == "grep_search"
    assert round_entry["toolResultEvidence"]["truncated"] is True
    assert len(task["_tool_result_cache"][cache_key]) == 7


def test_source_recovery_uses_idle_round_budget_without_artifact(monkeypatch):
    """One replayable read may use 24k; the old universal 8k cap may not."""
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "source-recoverable reads must not write artifacts"))
    raw = ''.join(
        f'line {index}: source evidence value\n' for index in range(1_600))
    assert 8_000 < _budget._result_tokens(raw, "") < 23_000

    visible = _budget.budget_tool_result_v2(
        "read_files", raw, user_id=7, model="",
        tool_arguments={"path": "demo.py"}, recovery_policy="source")
    value = json.loads(visible)

    assert 8_000 < _budget._model_result_tokens(visible, "") <= 24_000
    assert value["status"] == "ok"
    assert value["truncated"] is False
    assert value["artifactRef"] == ""
    assert "recovery" not in value


def test_oversized_source_read_returns_narrow_replay_not_artifact(monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "source-recoverable reads must not write artifacts"))
    arguments = {"reads": [
        {"path": "one.py", "_base": "/private/worktree"},
        {"path": "two.py", "start_line": 1_001, "end_line": 9_999},
        {"path": "three.py"},
    ]}
    raw = ''.join(
        f'line {index}: source evidence value\n' for index in range(4_000))
    visible = _budget.budget_tool_result_v2(
        "read_files", raw, user_id=7, model="", tool_arguments=arguments,
        recovery_policy="source")
    value = json.loads(visible)

    assert _budget._model_result_tokens(visible, "") <= 24_000
    assert value["status"] == "partial" and value["truncated"] is True
    assert value["artifactRef"] == "" and value["cursor"] == ""
    assert value["recovery"]["kind"] == "source"
    assert value["recovery"]["tool"] == "read_files"
    recovery_reads = value["recovery"]["arguments"]["reads"]
    assert [read["path"] for read in recovery_reads] == [
        "one.py", "two.py", "three.py"]
    assert recovery_reads[0] == {
        "path": "one.py", "start_line": 1, "end_line": 200}
    assert recovery_reads[1] == {
        "path": "two.py", "start_line": 1_001, "end_line": 1_200}
    assert all("_base" not in read for read in recovery_reads)
    assert [item["path"] for item in value["items"]] == [
        "one.py", "two.py", "three.py"]


def test_source_recovery_normalizes_reversed_line_range(monkeypatch):
    """A reversed model range reads swapped; recovery must slice that window.

    The read_files handler repairs start=2900,end=1010 into the 1010-2900
    window it actually reads. The recovery builder used to discard end<start
    and narrow to 2900-3499, pointing the model at lines it never received.
    """
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "source-recoverable reads must not write artifacts"))
    raw = ''.join(
        f'line {index}: source evidence value\n' for index in range(4_000))
    visible = _budget.budget_tool_result_v2(
        "read_files", raw, user_id=7, model="",
        tool_arguments={"path": "zh.json", "start_line": 2900,
                        "end_line": 1010},
        recovery_policy="source")
    value = json.loads(visible)

    assert value["status"] == "partial" and value["truncated"] is True
    assert value["recovery"]["kind"] == "source"
    assert value["recovery"]["arguments"] == {
        "path": "zh.json", "start_line": 1010, "end_line": 1609}


def test_tool_result_v2_fits_final_serialized_envelope_across_tool_families(
        monkeypatch):
    """Raw JSON/source escaping must not collapse an 8k budget to ~677 tokens.

    Incident mtcyxfbwqx03h0 observed the same conversation page at roughly
    677 tokens in raw JSON mode and 8k in prose mode. The old first pass fit
    the unescaped summary, then a fixed 512-token recursive fallback handled
    the larger serialized JSON string. This pins the tool-neutral boundary:
    every family gets a near-budget useful prefix plus an executable recovery
    instruction.
    """
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: "tool-result:" + "e" * 64)
    raw = ''.join(
        f'row {index}: value = "quoted\\\\path_{index}"\n'
        for index in range(12_000))
    sentinel = 'SERIALIZED_PREFIX_SENTINEL'
    raw = raw[:5_000] + sentinel + raw[5_000:]

    for tool_name in (
            'get_conversation', 'grep_search', 'fetch_url', 'run_command',
            'read_files'):
        visible = _budget.budget_tool_result_v2(
            tool_name, raw, user_id=7, model='kimi-k3')
        value = json.loads(visible)
        tokens = _budget._model_result_tokens(visible, 'kimi-k3')
        assert 6_000 <= tokens <= _budget.TOOL_RESULT_V2_MAX_TOKENS, (
            tool_name, tokens)
        assert sentinel in value['summary'], tool_name
        assert value['artifactRef'] == 'tool-result:' + 'e' * 64
        assert 'read_tool_artifact' in value['summary']
        assert value['visibleBytes'] == len(
            model_text_from_tool_result(visible).encode('utf-8'))


def test_tool_result_v2_does_not_artifactize_internal_serialization_overhead(
        monkeypatch):
    """Server-only envelope escaping must not consume the model budget."""
    import lib.tasks_pkg.compaction._budget as _budget

    stored = []
    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda content, **_kwargs: (
            stored.append(content) or 'tool-result:' + 's' * 64))
    # Quotes/backslashes are cheap in the raw token stream but require another
    # escaping layer inside ``summary``.
    raw = ('{"path":"C:\\\\work\\\\file","value":"quoted"}\n' * 500)
    assert _budget._result_tokens(raw, 'kimi-k3') <= 7_000

    visible = _budget.budget_tool_result_v2(
        'get_conversation', raw, user_id=7, model='kimi-k3')
    value = json.loads(visible)
    assert _budget._model_result_tokens(visible, 'kimi-k3') <= 8_000
    assert value['status'] == 'ok' and value['truncated'] is False
    assert value['artifactRef'] == ''
    assert stored == []


def test_tool_result_v2_does_not_truncate_structured_data_that_fits(
        monkeypatch):
    """Summary/item allocation is a fallback, never an artificial data cap."""
    import lib.tasks_pkg.compaction._budget as _budget

    stored = []
    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda content, **_kwargs: stored.append(content) or 'unexpected')
    summary = 'alpha beta ' * 2_500
    raw = json.dumps({
        'summary': summary,
        'items': [{'id': 1, 'value': 'small structured evidence'}],
    })
    visible = _budget.budget_tool_result_v2(
        'generic_reader', raw, user_id=7, model='kimi-k3')
    value = json.loads(visible)

    assert _budget._model_result_tokens(visible, 'kimi-k3') <= 8_000
    assert value['status'] == 'ok' and value['truncated'] is False
    assert value['summary'] == summary
    assert value['items'] == [{'id': 1, 'value': 'small structured evidence'}]
    assert stored == []


def test_tool_result_v2_rejects_ok_status_that_claims_truncation():
    from lib.tools.result_envelope import ToolResultEnvelopeV2

    with pytest.raises(ValueError, match='partial status'):
        ToolResultEnvelopeV2(
            status='ok', summary='not complete', truncated=True)


def test_tool_result_v2_batch_read_preserves_every_file_before_preview(
        tmp_path, monkeypatch):
    """A large first file may shrink previews, never erase later file results."""
    import lib.tasks_pkg.compaction._budget as _budget
    from lib.project_mod.read_tools import tool_read_files

    (tmp_path / "first.py").write_text(
        "FIRST_SENTINEL\n" + "first filler line\n" * 12_000)
    (tmp_path / "second.py").write_text("SECOND_SENTINEL\n")
    (tmp_path / "third.py").write_text("THIRD_SENTINEL\n")
    arguments = {"reads": [
        {"path": "first.py"},
        {"path": "second.py"},
        {"path": "third.py"},
    ]}
    projection_items: list[dict] = []
    raw = tool_read_files(
        str(tmp_path), arguments["reads"], result_items=projection_items)

    stored: list[str] = []
    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda content, **_kwargs: (
            stored.append(content) or "tool-result:" + "f" * 64))
    visible = _budget.budget_tool_result_v2(
        "read_files", raw, user_id=7, model="",
        tool_arguments=arguments, projection_items=projection_items)
    value = json.loads(visible)

    assert _budget._model_result_tokens(visible, "") <= 8_000
    assert value["status"] == "partial" and value["truncated"] is True
    assert value["artifactRef"] == "tool-result:" + "f" * 64
    assert stored == [raw]
    assert [item["path"] for item in value["items"]] == [
        "first.py", "second.py", "third.py"]
    assert [item["status"] for item in value["items"]] == [
        "ok", "ok", "ok"]
    assert "FIRST_SENTINEL" in value["items"][0].get("preview", "")
    assert "SECOND_SENTINEL" in value["items"][1].get("preview", "")
    assert "THIRD_SENTINEL" in value["items"][2].get("preview", "")


def test_tool_pipeline_carries_batch_read_projection_to_model_context(
        tmp_path, monkeypatch):
    """The production handler/settlement seam must not drop producer items."""
    import lib.tasks_pkg.compaction._budget as _budget
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tools.result_projection import TOOL_RESULT_PROJECTION_ITEMS_KEY

    (tmp_path / "first.py").write_text(
        "FIRST_PIPELINE_SENTINEL\n" + "pipeline filler\n" * 12_000)
    (tmp_path / "second.py").write_text("SECOND_PIPELINE_SENTINEL\n")
    (tmp_path / "third.py").write_text("THIRD_PIPELINE_SENTINEL\n")
    arguments = {"reads": [
        {"path": "first.py"},
        {"path": "second.py"},
        {"path": "third.py"},
    ]}
    round_entry = {
        "roundNum": 1,
        "toolCallId": "batch-read-call",
        "toolName": "read_files",
        "query": "read_files",
        "status": "searching",
    }
    parsed = [(
        {"id": "batch-read-call"}, "read_files", "batch-read-call",
        arguments, 1, round_entry, None,
    )]
    task = {
        "id": "batch-read-task",
        "convId": "batch-read-conversation",
        "_userId": 7,
        "messages": [],
        "toolRounds": [],
        "events": [],
        "events_lock": threading.Lock(),
        "status": "running",
        "model": "test-model",
        "config": {"tools": {"resultEnvelope": "v2"}},
        "_toolContractDocumentsByName": {
            "read_files": {"resultRecovery": "source"},
        },
    }
    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "the pipeline must honor read_files source recovery"))
    messages: list[dict] = []

    execute_tool_pipeline(
        task, parsed, {}, str(tmp_path), True, None, messages, [], 1,
        "test-model")

    tool_messages = [message for message in messages
                     if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    value = json.loads(tool_messages[0]["content"])
    assert "artifactRef" not in value
    assert value["recovery"]["kind"] == "source"
    assert [item["path"] for item in value["items"]] == [
        "first.py", "second.py", "third.py"]
    assert "SECOND_PIPELINE_SENTINEL" in value["items"][1]["preview"]
    assert "THIRD_PIPELINE_SENTINEL" in value["items"][2]["preview"]
    assert round_entry["toolResultEvidence"]["status"] == "partial"
    assert TOOL_RESULT_PROJECTION_ITEMS_KEY not in round_entry


def test_streaming_prefetch_carries_batch_read_projection_to_model_context(
        tmp_path, monkeypatch):
    """The dominant pre-execution cache path must preserve file boundaries."""
    from concurrent.futures import Future
    import time

    import lib.tasks_pkg.compaction._budget as _budget
    from lib.tasks_pkg.streaming_tool_executor import StreamingToolAccumulator
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline

    (tmp_path / "first.py").write_text(
        "FIRST_PREFETCH_SENTINEL\n" + "prefetch filler\n" * 12_000)
    (tmp_path / "second.py").write_text("SECOND_PREFETCH_SENTINEL\n")
    (tmp_path / "third.py").write_text("THIRD_PREFETCH_SENTINEL\n")
    arguments = {"reads": [
        {"path": "first.py"},
        {"path": "second.py"},
        {"path": "third.py"},
    ]}
    task = {
        "id": "batch-prefetch-task",
        "convId": "batch-prefetch-conversation",
        "_userId": 7,
        "messages": [],
        "toolRounds": [],
        "events": [],
        "events_lock": threading.Lock(),
        "status": "running",
        "aborted": False,
        "model": "test-model",
        "config": {"tools": {"resultEnvelope": "v2"}},
        "_tool_result_cache": {},
    }
    accumulator = StreamingToolAccumulator(
        task, str(tmp_path), project_enabled=True)
    try:
        preexecuted = accumulator._execute_one("read_files", arguments)
        future = Future()
        future.set_result(preexecuted)
        accumulator._futures["batch-prefetch-call"] = (
            future, "read_files", arguments, time.time())
        assert accumulator.inject_into_cache(task) == 1
    finally:
        accumulator._pool.shutdown(wait=False)

    cache_key = _make_cache_key("read_files", arguments)
    cached = task["_tool_result_cache"][cache_key]
    assert type(cached[0]) is str
    assert len(cached) == 8
    assert [item["path"] for item in cached[7]] == [
        "first.py", "second.py", "third.py"]

    round_entry = {
        "roundNum": 1,
        "toolCallId": "batch-prefetch-call",
        "toolName": "read_files",
        "query": "read_files",
        "status": "searching",
    }
    parsed = [(
        {"id": "batch-prefetch-call"}, "read_files",
        "batch-prefetch-call", arguments, 1, round_entry, None,
    )]
    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: "tool-result:" + "s" * 64)
    messages: list[dict] = []
    execute_tool_pipeline(
        task, parsed, {}, str(tmp_path), True, None, messages, [], 1,
        "test-model")

    tool_messages = [message for message in messages
                     if message.get("role") == "tool"]
    assert len(tool_messages) == 1
    value = json.loads(tool_messages[0]["content"])
    assert [item["path"] for item in value["items"]] == [
        "first.py", "second.py", "third.py"]
    assert "SECOND_PREFETCH_SENTINEL" in value["items"][1]["preview"]
    assert "THIRD_PREFETCH_SENTINEL" in value["items"][2]["preview"]
    settled_cache = task["_tool_result_cache"][cache_key]
    assert len(settled_cache) == 7
    assert json.loads(settled_cache[0])["contractVersion"] == \
        "tofu.tool-result/v2"
    assert settled_cache[0] != tool_messages[0]["content"]
    assert round_entry["toolResultEvidence"]["status"] == "partial"


def test_tool_result_v2_round_budget_keeps_batch_file_identities(
        tmp_path, monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget
    from lib.project_mod.read_tools import tool_read_files

    paths = ["one.py", "two.py", "three.py"]
    for index, path in enumerate(paths, 1):
        (tmp_path / path).write_text(
            f"FILE_{index}_SENTINEL\n" + f"value {index}\n" * 8_000)
    arguments = {"reads": [{"path": path} for path in paths]}
    projection_items: list[dict] = []
    raw = tool_read_files(
        str(tmp_path), arguments["reads"], result_items=projection_items)
    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: "tool-result:" + "g" * 64)
    visible = _budget.budget_tool_result_v2(
        "read_files", raw, user_id=8, model="",
        tool_arguments=arguments, projection_items=projection_items)

    values = {
        f"batch_{index}": (visible, "read_files", f"batch_{index}")
        for index in range(5)
    }
    reduced = _budget.enforce_round_aggregate_budget_v2(
        values, user_id=8, model="")

    assert sum(_budget._result_tokens(value[0], "")
               for value in reduced.values()) <= 24_000
    for content, _tool_name, _tool_use_id in reduced.values():
        item_paths = [item["path"] for item in json.loads(content)["items"]]
        assert item_paths == paths


def test_tool_result_v2_is_idempotent_and_preserves_structured_payload():
    import lib.tasks_pkg.compaction._budget as _budget

    structured = json.dumps({
        "status": "ok", "content": "artifact chunk sentinel",
        "nextCursor": "8192",
    })
    visible = _budget.budget_tool_result_v2(
        "read_tool_artifact", structured, user_id=7, model="")
    value = json.loads(visible)
    assert value["items"][0]["content"] == "artifact chunk sentinel"
    assert _budget.budget_tool_result_v2(
        "read_tool_artifact", visible, user_id=7, model="") == visible


def test_tool_result_v2_round_aggregate_is_bounded(monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: "tool-result:" + "b" * 64)
    values = {
        f"call_{index}": (("value " * 8_000), "read_files", f"call_{index}")
        for index in range(5)
    }
    reduced = _budget.enforce_round_aggregate_budget_v2(
        values, user_id=9, model="")
    assert sum(_budget._result_tokens(value[0], "")
               for value in reduced.values()) <= 24_000
    assert any(json.loads(value[0])["truncated"] for value in reduced.values())


def test_source_results_share_round_budget_without_artifact(monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: pytest.fail(
            "source-recoverable reads must not write artifacts"))
    raw = ''.join(
        f'line {index}: source evidence value\n' for index in range(1_600))
    values = {
        f"call_{index}": (
            _budget.budget_tool_result_v2(
                "read_files", raw, user_id=7, model="",
                tool_arguments={"path": f"file_{index}.py"},
                recovery_policy="source"),
            "read_files", f"call_{index}")
        for index in range(3)
    }
    reduced = _budget.enforce_round_aggregate_budget_v2(
        values, user_id=7, model="",
        recovery_by_call_id={
            f"call_{index}": {
                "policy": "source",
                "arguments": {"path": f"file_{index}.py"},
            }
            for index in range(3)
        })

    assert sum(_budget._result_tokens(value[0], "")
               for value in reduced.values()) <= 24_000
    partial = [json.loads(value[0]) for value in reduced.values()
               if json.loads(value[0])["status"] == "partial"]
    assert partial
    assert all(value["artifactRef"] == "" for value in partial)
    assert all(value["recovery"]["kind"] == "source" for value in partial)
    assert all(value["recovery"]["arguments"]["start_line"] == 1
               for value in partial)
    assert all(value["recovery"]["arguments"]["end_line"] == 600
               for value in partial)


def test_tool_result_envelope_reports_its_exact_visible_size():
    from lib.tools.result_envelope import (
        ToolResultEnvelopeV2, typed_tool_error,
    )

    legacy = ToolResultEnvelopeV2.from_legacy("甲 evidence")
    assert legacy.visible_bytes == len(legacy.to_model_text().encode("utf-8"))
    error = typed_tool_error(
        "temporary_failure", retryable=True, next_action="Retry once.")
    assert error.visible_bytes == len(error.to_model_text().encode("utf-8"))


def test_tool_result_v2_round_aggregate_keeps_honest_preview_on_store_failure(
        monkeypatch):
    import lib.tasks_pkg.compaction._budget as _budget

    monkeypatch.setattr(
        _budget, "_store_tool_result_artifact",
        lambda *_args, **_kwargs: "")
    values = {
        f"call_{index}": ((f"distinct-{index} " * 8_000), "read_files",
                           f"call_{index}")
        for index in range(5)
    }
    reduced = _budget.enforce_round_aggregate_budget_v2(
        values, user_id=9, model="", observed_at_ms=123)

    assert sum(_budget._result_tokens(value[0], "")
               for value in reduced.values()) <= 24_000
    partial = [json.loads(value[0]) for value in reduced.values()
               if value[0].startswith("{")]
    unavailable = [value for value in partial
                   if "artifact persistence failed" in value["summary"]]
    assert unavailable
    assert all(value["artifactRef"] == "" for value in unavailable)
    assert all(value["cursor"] == "" for value in unavailable)
    assert all(value["freshness"]["observedAtMs"] == 123
               for value in unavailable)
    assert any("distinct-" in value["summary"] for value in unavailable)


def test_tool_artifact_cas_is_owner_scoped_expiring_and_utf8_safe():
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._artifacts import (
        _tool_result_artifact_prune,
        _tool_result_artifact_put,
        _tool_result_artifact_read,
        _tool_result_artifact_search,
    )
    from lib.storage_sidecar.schema import initialize_schema

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    content = "甲乙丙 evidence 丁戊己"
    put = _tool_result_artifact_put(session, {
        "user_id": 11, "content": content, "media_type": "text/plain",
        "created_at_ms": 100, "expires_at_ms": 1_000,
    })
    repeated = _tool_result_artifact_put(session, {
        "user_id": 11, "content": content, "media_type": "text/plain",
        "created_at_ms": 200, "expires_at_ms": 900,
    })
    assert repeated["expiresAtMs"] == 1_000
    assert _tool_result_artifact_read(session, {
        "user_id": 12, "artifact_ref": put["artifactRef"], "now_ms": 200,
    }) is None

    chunks: list[str] = []
    cursor = "0"
    while cursor is not None:
        row = _tool_result_artifact_read(session, {
            "user_id": 11, "artifact_ref": put["artifactRef"],
            "now_ms": 200, "offset": int(cursor), "limit": 5,
        })
        chunks.append(row["content"])
        cursor = row["nextCursor"]
    assert "".join(chunks) == content
    found = _tool_result_artifact_search(session, {
        "user_id": 11, "artifact_ref": put["artifactRef"],
        "now_ms": 200, "query": "evidence", "limit": 2,
    })
    assert found["items"] and "evidence" in found["items"][0]["text"]
    assert _tool_result_artifact_read(session, {
        "user_id": 11, "artifact_ref": put["artifactRef"], "now_ms": 1_001,
    }) is None
    assert _tool_result_artifact_prune(
        session, {"now_ms": 1_001, "limit": 10})["deleted"] == 1
    connection.close()


def test_expired_tool_artifact_maintenance_is_bounded(monkeypatch):
    from lib.tasks_pkg import event_log

    class Client:
        def __init__(self):
            self.calls = []
            self.results = [
                {"deleted": 512, "hasMore": True},
                {"deleted": 3, "hasMore": False},
            ]

        def maintenance(self, operation, payload, **kwargs):
            self.calls.append((operation, payload, kwargs))
            return self.results.pop(0)

    monkeypatch.setattr(
        event_log._SIDECAR_MAINTENANCE_STOP, "is_set", lambda: False)
    monkeypatch.setattr(
        event_log, "_TOOL_RESULT_ARTIFACT_PRUNE_BATCH_ROWS", 512)
    client = Client()
    result = event_log._prune_tool_result_artifact_backlog(client, 1234)
    assert result == {"deleted": 515, "batches": 2, "remaining": False}
    assert all(row[0] == "tool_result_artifact.prune" for row in client.calls)
    assert all(row[2]["deadline"] == 30 for row in client.calls)


def test_kimi_tool_surface_and_gateway_contracts_are_bounded():
    from lib.tools.gateway import (
        gateway_tool_schemas, local_wire_tools, tool_schema_tokens,
    )

    catalog = [{
        "type": "function",
        "function": {
            "name": f"tool_{index}", "description": "detail " * 500,
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "query " * 100}}},
        },
    } for index in range(30)]
    policy = {f"tool_{index}": ("eager" if index < 12 else "searchable")
              for index in range(30)}
    wire = local_wire_tools(
        catalog, discovery_policy_by_name=policy,
        discovery_catalog_size=30, searchable_count=18,
        schema_budget_tokens=4_000, model="kimi-k3")

    assert tool_schema_tokens(wire, model="kimi-k3") <= 4_000
    assert tool_schema_tokens(gateway_tool_schemas(), model="kimi-k3") <= 522
    names = {tool["function"]["name"] for tool in wire}
    assert {"search_tools", "execute_tools"} <= names


def test_kimi_lean_prompt_and_named_ablations_are_measurable():
    from lib.tasks_pkg.system_prompt_cc import build_static_prompt
    from lib.token_counter import count_text

    kwargs = {
        "cwd": "/workspace", "is_git": True, "model": "kimi-k3",
        "tool_names": {"read_files", "web_search"}, "include_date": False,
    }
    lean = build_static_prompt(**kwargs, profile="lean")
    auto = build_static_prompt(**kwargs, profile="auto")
    assert auto != lean
    assert count_text(auto, model="kimi-k3") > count_text(
        lean, model="kimi-k3")
    assert count_text(lean, model="kimi-k3") < 600
    assert "NEVER generate or guess URLs" in lean
    assert "NEVER generate or guess URLs" not in build_static_prompt(
        **kwargs, profile="lean_no_url")
    assert "# Tools" not in build_static_prompt(
        **kwargs, profile="lean_no_tools")
    assert "# Output" not in build_static_prompt(
        **kwargs, profile="lean_no_output")


def test_adaptive_compaction_uses_positive_expected_value():
    from lib.tasks_pkg.compaction._tokens import _adaptive_compaction_economics

    positive_task = {
        "model": "kimi-k3", "config": {"compaction": {"strategy": "adaptive"}},
        "_adaptiveCompactionInputs": {
            "cacheReadRatio": 0.8, "remainingRoundsMedian": 10,
            "historicalEvidenceLossRate": 0.0,
        },
    }
    positive = _adaptive_compaction_economics(
        [{"role": "user", "content": "small hot tail"}], positive_task,
        total_tokens=120_000, window_threshold=800_000)
    assert positive["shouldTrigger"] is True
    assert positive["projectedNetSavingsUsd"] > 0

    negative_task = {
        "model": "kimi-k3", "config": {"compaction": {"strategy": "adaptive"}},
        "_adaptiveCompactionInputs": {"remainingRoundsMedian": 0},
    }
    negative = _adaptive_compaction_economics(
        [{"role": "user", "content": "small hot tail"}], negative_task,
        total_tokens=120_000, window_threshold=800_000)
    assert negative["shouldTrigger"] is False


def test_adaptive_summary_rejects_missing_goal_constraints_and_pending_work():
    from lib.tasks_pkg.compaction._layer2._compact import (
        _summary_missing_task_state_fields,
    )
    from lib.tasks_pkg.context_composer import TaskStateSnapshotV1

    snapshot = TaskStateSnapshotV1(
        goal="Implement artifact cursor recovery",
        hard_constraints=("Never expose filesystem paths",),
        todos=("run the storage migration test",),
    )
    missing = _summary_missing_task_state_fields(
        "A generic summary with no task state.", snapshot)
    assert missing == ["goal", "hard_constraints[0]", "todos[0]"]
    assert _summary_missing_task_state_fields(
        "Implement artifact cursor recovery. Never expose filesystem paths. "
        "Next, run the storage migration test.", snapshot) == []


def test_adaptive_l2_replaces_lossy_narrative_with_derived_state(monkeypatch):
    import lib.tasks_pkg.compaction._layer2._compact as layer2
    from lib.tasks_pkg.compaction.api import execute_compact_tool

    monkeypatch.setattr(
        layer2, "_generate_query_aware_summary",
        lambda *_args, **_kwargs: "Generic prose that omitted the task.")
    messages = [{"role": "system", "content": "system"}, {
        "role": "user",
        "content": "Implement artifact cursor recovery; never expose paths.",
    }]
    for index in range(6):
        messages.extend((
            {"role": "assistant", "content": "work " + "x" * 2_000},
            {"role": "user", "content": f"continue phase {index}"},
        ))
    meta = {}
    task = {
        "id": "task-adaptive", "convId": "conv-adaptive",
        "config": {"model": "kimi-k3",
                   "compaction": {"strategy": "adaptive"}},
        "_todos": ["run the storage migration test"],
    }
    compact_result = execute_compact_tool(
        messages, task=task, preserve_budget_tokens=500,
        _compaction_skip_archive=True, _result_meta=meta)

    assert meta["compacted"] is True
    assert meta["summaryRejected"] is True
    assert "goal" in meta["missingTaskStateFields"]
    assert "TaskStateSnapshotV1" in compact_result
    assert "artifact cursor recovery" in compact_result
    assert "TaskStateSnapshotV1" in meta["summary_text"]


def test_orchestration_v2_selects_one_explainable_shape(monkeypatch):
    from lib.tasks_pkg import tool_orchestration_policy as policy

    monkeypatch.setattr(policy, "_eligible_programmatic_names",
                        lambda _tools: {"read_files"})
    multi = policy.resolve_tool_orchestration(
        requested_programmatic="on", requested_multi_agent="auto",
        messages=[{"role": "user", "content":
                   "全面并行审计多个独立模块并比较实现"}],
        tools=[], round_num=1, policy_version="v2")
    assert multi["shape"] == "independent_read_only_agents"
    assert multi["programmaticCalling"] == "off"
    assert multi["expectedSavings"]["basis"] == "independent_workstreams"

    verified = policy.resolve_tool_orchestration(
        requested_programmatic="off", requested_multi_agent="off",
        messages=[{"role": "user", "content": "implement the fix and run tests"}],
        tools=[], round_num=1, policy_version="v2")
    assert verified["shape"] == "verified_loop"

    refused = policy.resolve_tool_orchestration(
        requested_programmatic="off", requested_multi_agent="read_only",
        messages=[{"role": "user", "content": "Read this one file."}],
        tools=[], round_num=1, policy_version="v2")
    assert refused["shape"] == "direct_execution"
    assert refused["multiAgentReason"] == "task_not_independently_decomposable"


def test_progress_ledger_requires_world_evidence_or_verification_progress():
    from lib.agent_core.progress_ledger import ProgressLedgerV2

    ledger = ProgressLedgerV2()
    calls = [{"function": {"name": "read_files",
                            "arguments": '{"paths":["a.py"]}'}}]
    assert ledger.observe(calls, world_version="v1")["progress"] is True
    assert ledger.observe(calls, world_version="v1")["noProgressStreak"] == 1
    evidence = ledger.observe(
        calls, world_version="v1", evidence_ids=["ev_new"])
    assert evidence["progress"] is True
    assert evidence["noProgressStreak"] == 0
    assert ledger.observe(
        calls, world_version="v1", verification="passed")["progress"] is True


def test_long_agent_experiments_isolate_arms_and_guard_combined():
    from lib.experiments.builtin_long_agent import (
        COMBINED_REQUIRED_WINNERS, long_agent_spec,
    )

    pilot = long_agent_spec(
        experiment_id="prompt-url-ablation-v1",
        candidate_strategy="prompt_ablate_url")
    assert pilot["enrollmentBps"] == 1_000
    assert pilot["arms"][0]["strategy"]["strategyId"] == "prompt_lean_kimi"
    assert pilot["arms"][1]["strategy"]["strategyId"] == "prompt_ablate_url"
    with pytest.raises(ValueError, match="independently winning"):
        long_agent_spec(
            experiment_id="combined-too-early", candidate_strategy="combined_v2")
    combined = long_agent_spec(
        experiment_id="combined-confirmation-v1", candidate_strategy="combined_v2",
        independently_winning=set(COMBINED_REQUIRED_WINNERS))
    assert combined["arms"][1]["strategy"]["strategyId"] == "combined_v2"


def _v2_manifest():
    from lib.benchmark_contract import build_manifest_v2

    digest = "a" * 64
    return build_manifest_v2(
        run_id="run-v2", harness={"name": "paired-harness", "version": "1",
                                  "commitSha256": digest},
        agent={"name": "codex", "version": "0.149.1",
               "binarySha256": "b" * 64},
        provider_face="yourprovider-chat", provider_slot_id="kimi-slot-fixture",
        thinking="high",
        experiment_arm="control", pair_id="pilot-pair",
        comparison_role="candidate",
        tool_permissions={"profile": "frozen-read-write"},
        prompt_digest="d" * 64, tool_schema_digest="e" * 64,
        dataset_snapshot={"id": "pilot-v1", "sha256": "c" * 64,
                          "frozen": True},
        task_table=[{"taskId": "task-1", "family": "pilot",
                    "dataset": "pilot"}],
        sandbox={"kind": "rootless-qemu", "networkPolicy": "frozen"},
        retry_rule={"maxInfrastructureRetries": 1,
                    "retryableFailureClasses": ["infrastructure"]},
        artifact_limits={"maximumArtifactBytes": 1_000_000,
                         "maximumTaskArtifactBytes": 2_000_000,
                         "maximumRunArtifactBytes": 10_000_000},
        timeout_seconds=600, maximum_infrastructure_failure_rate=0.02,
        environment={"gitCommit": digest},
    )


def test_benchmark_v2_freezes_identity_cost_latency_and_release_gates():
    from lib.benchmark_contract import (
        BenchmarkContractError, BenchmarkRecordV2, acceptance_decision_v2,
        RELEASE_TASK_MATRIX_V2, build_task_record_v2,
    )

    manifest = _v2_manifest()
    assert BenchmarkRecordV2(manifest).to_dict() == manifest
    task = build_task_record_v2(
        run_id="run-v2", dataset="pilot", family="pilot", task_id="task-1",
        agent=manifest["agent"], provider_face="yourprovider-chat",
        provider_slot_id="kimi-slot-fixture", thinking="high",
        experiment_arm="control", oracle={"passed": True, "type": "exact"},
        rounds=[{"round": 1, "usage": {"inputTokens": 10}}],
        context_blocks=[], tool_schemas=[], tool_results=[], compactions=[],
        call_graph=[], retries=[], cost={"agentCostUsd": 0.01},
        latency={"rawWallMs": 100, "oracleReadyMs": 100,
                 "queueMs": 5, "ttftMs": 15, "modelMs": 70, "toolMs": 10,
                 "translationCpuMs": 10,
                 "proxyCpuMs": 20,
                 "codexFavoredCorrectedWallMs": 90},
    )
    assert task["cost"]["simulatorAndJudgeExcluded"] is True

    task_table = []
    candidate = {}
    for (family, dataset), count in RELEASE_TASK_MATRIX_V2.items():
        task_table.extend({
            "taskId": f"{dataset}:{index}", "family": family,
            "dataset": dataset,
        } for index in range(count))
        candidate.setdefault(family, []).extend([True] * count)
    baseline = {name: list(values) for name, values in candidate.items()}
    orchestration_adoption = {
        "contractVersion": "tofu.orchestration-adoption-summary/v1",
        "taskRecords": sum(RELEASE_TASK_MATRIX_V2.values()),
        "tasksWithV2Decisions": sum(RELEASE_TASK_MATRIX_V2.values()),
        "v2Decisions": sum(RELEASE_TASK_MATRIX_V2.values()),
        "programTrajectories": 1,
        "agentTrajectories": 1,
        "adoptedShapes": {
            "ptc_bounded_reduction": 1,
            "independent_read_only_agents": 1,
        },
        "falseAdoptionClaims": 0,
    }
    decision = acceptance_decision_v2(
        candidate_by_family=candidate, baseline_by_family=baseline,
        task_table=task_table,
        candidate_agent_cost_usd=80, baseline_agent_cost_usd=100,
        candidate_p90_oracle_ready_ms=800, baseline_p90_oracle_ready_ms=1_000,
        candidate_critical_incidents=0,
        judge_passes={"claude-opus-5": True, "glm-5.3": True},
        infrastructure_failure_rate=0.01,
        maximum_infrastructure_failure_rate=0.02,
        candidate_orchestration_adoption=orchestration_adoption,
    )
    assert decision["releaseEligible"] is True
    with pytest.raises(BenchmarkContractError):
        acceptance_decision_v2(
            candidate_by_family=candidate, baseline_by_family=baseline,
            task_table=task_table,
            candidate_agent_cost_usd=float("nan"), baseline_agent_cost_usd=100,
            candidate_p90_oracle_ready_ms=800,
            baseline_p90_oracle_ready_ms=1_000,
            candidate_critical_incidents=0,
            judge_passes={"claude-opus-5": True, "glm-5.3": True},
            infrastructure_failure_rate=0.01,
            maximum_infrastructure_failure_rate=0.02,
            candidate_orchestration_adoption=orchestration_adoption)

    missing_actual_program = {
        **orchestration_adoption, "programTrajectories": 0,
    }
    not_adopted = acceptance_decision_v2(
        candidate_by_family=candidate, baseline_by_family=baseline,
        task_table=task_table,
        candidate_agent_cost_usd=80, baseline_agent_cost_usd=100,
        candidate_p90_oracle_ready_ms=800,
        baseline_p90_oracle_ready_ms=1_000,
        candidate_critical_incidents=0,
        judge_passes={"claude-opus-5": True, "glm-5.3": True},
        infrastructure_failure_rate=0.01,
        maximum_infrastructure_failure_rate=0.02,
        candidate_orchestration_adoption=missing_actual_program,
    )
    assert not_adopted["releaseEligible"] is False
    assert not_adopted["gates"]["orchestrationActualAdoptionProven"] is False


def test_release_matrix_shape_is_machine_checked():
    from lib.benchmark_contract import (
        RELEASE_TASK_MATRIX_V2, validate_release_task_matrix_v2,
    )

    tasks = []
    for (family, dataset), count in RELEASE_TASK_MATRIX_V2.items():
        tasks.extend({"taskId": f"{dataset}:{index}", "family": family,
                      "dataset": dataset} for index in range(count))
    result = validate_release_task_matrix_v2(tasks)
    assert result["tasks"] == 1_845


def test_tool_search_v2_frozen_corpus_clears_release_thresholds():
    from evaluations.tool_search import (
        CATALOG, FROZEN_EPISODES_V2, SEARCH_TEXT_BY_NAME,
    )
    from evaluations.tool_search.evaluation import (
        evaluate_retrieval, v2_release_gate,
    )
    from lib.tools.gateway import search_executable_catalog

    report = evaluate_retrieval(
        CATALOG, list(FROZEN_EPISODES_V2), search=search_executable_catalog,
        search_text_by_name=SEARCH_TEXT_BY_NAME,
    )
    assert report["episodes"] >= 1_000
    gate = v2_release_gate(
        report, unauthorized_executions=0,
        end_to_end_accuracy=report["recall_at_1"],
    )
    assert report["recall_at_5"] >= 0.99
    assert gate["releaseEligible"] is True


def test_codex_command_and_proxy_metrics_pin_fairness_controls(tmp_path):
    from evaluations.codex_kimi_proxy.codex_contract import (
        CodexContractError, benchmark_trial_token, build_codex_command,
        validate_proxy_metrics, write_trial_proxy_metrics,
    )

    trial_token = benchmark_trial_token("pair", "run", "task")
    command = build_codex_command(
        binary="./codex", proxy_base_url="http://127.0.0.1:48123",
        prompt="solve", reasoning_effort="high", trial_token=trial_token,
    )
    joined = " ".join(command)
    assert command[1:3] == ["exec", "--ignore-user-config"]
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert "--json" in command
    assert "features.remote_compaction_v2=false" in joined
    assert "model_context_window=272000" in joined
    assert "model_auto_compact_token_limit=244800" in joined
    assert 'model_auto_compact_token_limit_scope="total"' in joined
    assert "tools.web_search=false" in joined
    assert 'model_provider="tofu_kimi_proxy"' in joined
    assert 'model_providers.tofu_kimi_proxy.name=' in joined
    assert 'model_providers.tofu_kimi_proxy.supports_websockets=false' in joined
    assert "X-Tofu-Benchmark-Trial" in joined
    assert trial_token in joined
    with pytest.raises(CodexContractError, match="loopback"):
        build_codex_command(
            binary="./codex", proxy_base_url="https://example.test",
            prompt="solve", reasoning_effort="high",
        )

    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text("\n".join((
        json.dumps({
            "event": "responsesTranslation", "upstreamCalls": 1,
            "translationCpuNs": 20, "proxyCpuNs": 40, "rawWallNs": 100,
            "suppressedNativeToolTypes": ["web_search"],
        }),
        json.dumps({"event": "invalidCompactRequest"}),
    )))
    invalid = validate_proxy_metrics(str(metrics), expected_request_count=1)
    assert invalid["valid"] is False
    assert invalid["compactRequests"] == 1
    assert invalid["suppressedNativeToolTypes"] == ["web_search"]
    assert invalid["codexFavoredCorrectedWallNs"] == 80

    shared = tmp_path / "shared.jsonl"
    other_token = benchmark_trial_token("other")
    shared.write_text("\n".join((
        json.dumps({
            "event": "responsesTranslation", "trialToken": trial_token,
            "upstreamCalls": 1, "invalidTrial": False,
            "status": "completed", "clientDisconnected": False,
            "translationCpuNs": 2, "proxyCpuNs": 4, "rawWallNs": 10,
            "usage": {"input_tokens": 5, "output_tokens": 2},
        }),
        json.dumps({
            "event": "responsesTranslation", "trialToken": other_token,
            "upstreamCalls": 1, "invalidTrial": False,
        }),
    )) + "\n", encoding="utf-8")
    trial_metrics = tmp_path / "trial.jsonl"
    extracted = write_trial_proxy_metrics(
        str(shared), str(trial_metrics), trial_token=trial_token)
    assert extracted["responsesRequests"] == 1
    valid = validate_proxy_metrics(
        str(trial_metrics), expected_request_count=1,
        require_trial_token=True)
    assert valid["valid"] is True
    assert valid["trialToken"] == trial_token


def test_proxy_translation_preserves_tools_thinking_usage_and_failures():
    from evaluations.codex_kimi_proxy.translation import (
        ChatSSETranslator, TranslationError, chat_response_to_responses,
        responses_request_to_chat,
    )

    chat = responses_request_to_chat({
        "model": "kimi-k3", "instructions": "system contract",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "inspect"}]},
            {"type": "function_call", "call_id": "call_a",
             "name": "read_files", "arguments": '{"paths":["a.py"]}'},
            {"type": "function_call_output", "call_id": "call_a",
             "output": "contents"},
        ],
        "tools": [{"type": "function", "name": "read_files",
                   "description": "Read files", "strict": True,
                   "parameters": {"type": "object", "properties": {}}}],
        "tool_choice": {"type": "function", "name": "read_files"},
        "reasoning": {"effort": "xhigh"}, "stream": True,
    })
    assert chat["messages"][0] == {"role": "system", "content": "system contract"}
    assert chat["tools"][0]["function"]["strict"] is True
    assert chat["tool_choice"]["function"]["name"] == "read_files"
    assert chat["reasoning_effort"] == "max"
    assert chat["stream_options"] == {"include_usage": True}
    with pytest.raises(TranslationError, match="kimi-k3"):
        responses_request_to_chat({"model": "other", "input": "x"})

    namespaced = responses_request_to_chat({
        "model": "kimi-k3", "input": "delegate",
        "tools": [{
            "type": "namespace", "name": "agents",
            "description": "Sub-agent control.",
            "tools": [{"type": "function", "name": "spawn_agent",
                       "description": "Spawn one agent.",
                       "parameters": {"type": "object", "properties": {}}}],
        }],
    })
    assert namespaced["tools"][0]["function"]["name"] == "spawn_agent"
    assert namespaced["tools"][0]["function"]["description"].startswith(
        "Namespace agents: Sub-agent control.")

    frozen_search = responses_request_to_chat({
        "model": "kimi-k3", "input": "research",
        "tools": [
            {"type": "web_search"},
            {"type": "function", "name": "web_search",
             "description": "Search the frozen source pack.",
             "parameters": {"type": "object", "properties": {}}},
        ],
    })
    assert [tool["function"]["name"] for tool in frozen_search["tools"]] == [
        "web_search"]

    response = chat_response_to_responses({
        "model": "kimi-k3", "choices": [{"finish_reason": "length",
            "message": {"content": "partial", "tool_calls": []}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3,
                  "prompt_tokens_details": {"cached_tokens": 7}},
    })
    assert response["status"] == "incomplete"
    assert response["usage"]["input_tokens_details"]["cached_tokens"] == 7

    translator = ChatSSETranslator()
    translator.feed({"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "same", "function": {"name": "read_"}}]}}]})
    translator.feed({"choices": [{"delta": {"tool_calls": [{
        "index": 0, "function": {"name": "files", "arguments": "{}"}}, {
        "index": 1, "id": "same",
        "function": {"name": "run_command", "arguments": "{}"}}]},
        "finish_reason": "tool_calls"}]})
    completed = translator.finish(completed=True)
    final = completed[-1]["response"]
    calls = [item for item in final["output"] if item["type"] == "function_call"]
    assert [item["name"] for item in calls] == ["read_files", "run_command"]
    assert [item["call_id"] for item in calls] == ["same", "same_2"]

    malformed = ChatSSETranslator()
    malformed.feed({"choices": [{"delta": {"tool_calls": [{
        "index": 0, "id": "empty", "function": {"arguments": "{}"}}]}}]})
    failed = malformed.finish(completed=True)
    assert failed[-1]["type"] == "response.failed"
    assert failed[-1]["response"]["error"]["code"] == "invalid_function_call"
    with pytest.raises(TranslationError, match="without content"):
        chat_response_to_responses({
            "model": "kimi-k3",
            "choices": [{"finish_reason": "stop", "message": {}}],
        })


def test_loopback_proxy_makes_exactly_one_upstream_call(tmp_path):
    from evaluations.codex_kimi_proxy.codex_contract import benchmark_trial_token
    from evaluations.codex_kimi_proxy.server import CodexKimiProxy, ProxyConfig

    class Upstream(BaseHTTPRequestHandler):
        calls = 0

        def log_message(self, *_args):
            return

        def do_POST(self):  # noqa: N802
            type(self).calls += 1
            length = int(self.headers.get("Content-Length") or 0)
            request = json.loads(self.rfile.read(length))
            assert request["model"] == "kimi-k3"
            payload = json.dumps({
                "id": "chat-1", "model": "kimi-k3",
                "choices": [{"finish_reason": "stop",
                             "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    metrics = tmp_path / "proxy.jsonl"
    trial_metrics_dir = tmp_path / "trial-metrics"
    trial_token = benchmark_trial_token("direct-proxy-test")
    proxy = CodexKimiProxy(("127.0.0.1", 0), ProxyConfig(
        upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
        upstream_api_key="test-key", metrics_jsonl=str(metrics),
        trial_metrics_dir=str(trial_metrics_dir),
        require_trial_header=True))
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    proxy_thread.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{proxy.server_port}/v1/responses",
            headers={"X-Tofu-Benchmark-Trial": trial_token},
            json={"model": "kimi-k3", "input": "hello"}, timeout=5)
        assert response.status_code == 200
        assert response.json()["output"][0]["content"][0]["text"] == "ok"
    finally:
        proxy.shutdown()
        proxy.server_close()
        upstream.shutdown()
        upstream.server_close()
        proxy_thread.join(timeout=2)
        upstream_thread.join(timeout=2)
    rows = [json.loads(line) for line in metrics.read_text().splitlines()]
    assert Upstream.calls == 1
    assert rows[-1]["upstreamCalls"] == 1
    assert rows[-1]["trialToken"] == trial_token
    assert rows[-1]["usage"]["input_tokens"] == 2
    trial_rows = [
        json.loads(line)
        for line in (trial_metrics_dir / f"{trial_token}.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert trial_rows == rows
    assert rows[-1]["proxyCpuNs"] >= rows[-1]["translationCpuNs"]


def _v2_spill_envelope(ref: str) -> str:
    return json.dumps({
        "artifactRef": ref,
        "contractVersion": "tofu.tool-result/v2",
        "cursor": "",
        "error": None,
        "items": [],
        "status": "partial",
        "summary": "spilled oversized result",
        "truncated": True,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_tool_result_v2_spill_registers_artifact_provenance(monkeypatch):
    """A per-result L0 spill must record WHICH call minted the pointer.

    The continuation label can only name the source round + tool if the
    spill site registered the origin — the digest itself carries none.
    """
    from lib.tasks_pkg.tool_dispatch import _pipeline

    ref = "tool-result:" + "c" * 64
    envelope = _v2_spill_envelope(ref)
    monkeypatch.setattr(
        _pipeline, "budget_tool_result_v2", lambda *_a, **_k: envelope)
    task = {"model": "", "convId": "c1", "config": {}, "_userId": 7}
    round_entry = {"query": "web_search: citadel", "llmRound": 9}
    content = _pipeline._settle_tool_result(
        task, "some_big_tool", "tc_1", {"query": "citadel"}, 1, round_entry,
        "raw " * 9_000, idempotent_tools=frozenset(), cache={}, tid="t1",
        round_num=10)
    from lib.tools.result_envelope import model_text_from_tool_result
    assert content == model_text_from_tool_result(envelope)
    assert round_entry["toolResultEvidence"]["artifactRef"] == ref
    assert round_entry["compactionLayer"] == "L0"
    assert task["_artifactProvenance"] == {
        ref: {
            "toolName": "some_big_tool",
            "display": "web_search: citadel",
            "llmRound": 9,
        },
    }


def test_tool_result_v2_complete_result_is_not_stamped_compacted():
    """A complete V2 result must NOT claim L0 compaction.

    The envelope lane re-serializes a small structured result with sorted
    keys and tight separators; that can shrink the model text by a few chars
    while withholding nothing. Stamping ``compactionLayer='L0'`` there
    renders a false COMPACTED pill ("replaced with a placeholder") on a
    result the model fully saw.
    """
    from lib.tasks_pkg.tool_dispatch import _pipeline

    payload = json.dumps({
        "completed": [],
        "mode": "any",
        "note": "An identical await already timed out.",
        "status": "ok",
        "still_running": ["fdca8160"],
        "timed_out": True,
        "wait_suppressed": True,
    }, ensure_ascii=False)
    task = {"model": "", "convId": "c1", "config": {}, "_userId": 7}
    round_entry = {"query": "await_agents", "llmRound": 3}
    content = _pipeline._settle_tool_result(
        task, "await_agents", "tc_1", {}, 1, round_entry, payload,
        idempotent_tools=frozenset(), cache={}, tid="t1", round_num=3)
    assert json.loads(content) == json.loads(payload), (
        'the model-visible text must keep full semantic fidelity')
    assert len(content) < len(payload), (
        'this regression requires normalization to actually shrink chars')
    assert 'compactionLayer' not in round_entry
    assert 'compactedFromChars' not in round_entry


def test_round_aggregate_spill_registers_artifact_provenance(monkeypatch):
    """The round-AGGREGATE lane re-artifacts settled results; it mints a NEW
    pointer and must register its origin too."""
    from lib.tasks_pkg.tool_dispatch import _pipeline

    ref = "tool-result:" + "e" * 64
    envelope = _v2_spill_envelope(ref)
    monkeypatch.setattr(
        _pipeline, "enforce_round_aggregate_budget_v2",
        lambda results, **_k: {
            call_id: (envelope, name, call_id)
            for call_id, (_content, name, _tc) in results.items()
        })
    round_entry = {"query": "read_files: big.log", "llmRound": 3}
    parsed = [(None, "read_files", "call_1", None, 4, round_entry)]
    message = {"role": "tool", "tool_call_id": "call_1",
               "content": "x" * 60_000}
    task = {"model": "", "convId": "c1", "config": {}, "_userId": 7}
    _pipeline._apply_round_aggregate_budget(
        task, parsed, [("call_1", "x" * 60_000, "read_files")], [message])
    from lib.tools.result_envelope import model_text_from_tool_result
    assert message["content"] == model_text_from_tool_result(envelope)
    assert round_entry["toolResultEvidence"]["artifactRef"] == ref
    assert round_entry["compactionLayer"] == "L0"
    assert task["_artifactProvenance"][ref] == {
        "toolName": "read_files",
        "display": "read_files: big.log",
        "llmRound": 3,
    }


def test_spill_provenance_ignores_malformed_input_but_not_programming_errors(
    monkeypatch,
):
    from lib.tasks_pkg.tool_dispatch import _pipeline

    task: dict = {}
    _pipeline._register_spilled_artifact_origin(
        task,
        {"query": "read_files: broken", "llmRound": 2},
        "read_files",
        '{"artifactRef":"tool-result:broken"',
    )
    assert "_artifactProvenance" not in task

    def crash_parser(_content):
        raise RuntimeError('synthetic parser defect')

    monkeypatch.setattr(
        _pipeline,
        'json',
        SimpleNamespace(
            JSONDecodeError=json.JSONDecodeError,
            loads=crash_parser,
        ),
    )
    with pytest.raises(RuntimeError, match='synthetic parser defect'):
        _pipeline._register_spilled_artifact_origin(
            task,
            {"query": "read_files: broken", "llmRound": 2},
            "read_files",
            '{"artifactRef":"tool-result:broken"}',
        )


def test_artifact_continuation_label_names_source_round():
    """The row is BORN with the readable label: source round + source tool
    display replace the bare content-hash digest at round-build time."""
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
    from lib.tool_result_artifacts import register_artifact_provenance

    ref = "tool-result:" + "d" * 64
    task: dict = {}
    register_artifact_provenance(
        task, ref, tool_name="browser_research_page",
        display=("Research website → https://friday.internal.example.com/mcphub-api"
                 "/skill/list?keyword=citadel"),
        llm_round=9, tool_call_id="source_tc_9")

    _, round_entry, event = _build_tool_round_entry(
        "read_tool_artifact", {"artifact_ref": ref, "cursor": 0},
        "tc_9", "{}", 0, False, task=task)
    assert round_entry["query"] == (
        "Read compacted result of R10 · Research website → "
        "https://friday.internal.example.com/mcphub-api/skill/list?keyword=citadel")
    assert event["query"] == round_entry["query"]
    # Structured twin of the flat label: the frontend renders an origin chip
    # (kind + source round) before the source label, so the read-back action
    # and the original call never stack two "Read" verbs.
    assert round_entry["_artifactOrigin"] == {
        "kind": "read",
        "sourceRound": 10,
        "sourceToolCallId": "source_tc_9",
        "source": ("Research website → https://friday.internal.example.com/mcphub-api"
                   "/skill/list?keyword=citadel"),
    }
    assert event["_artifactOrigin"] == round_entry["_artifactOrigin"]
    assert round_entry["parentToolCallId"] == "source_tc_9"
    assert event["parentToolCallId"] == "source_tc_9"

    _, search_entry, _ = _build_tool_round_entry(
        "search_tool_artifact",
        {"artifact_ref": ref, "query": "download url"},
        "tc_10", "{}", 0, False, task=task)
    assert search_entry["query"].startswith(
        "Search compacted result of R10 ·")
    assert search_entry["query"].endswith(": download url")
    assert search_entry["_artifactOrigin"] == {
        "kind": "search",
        "sourceRound": 10,
        "sourceToolCallId": "source_tc_9",
        "source": ("Research website → https://friday.internal.example.com/mcphub-api"
                   "/skill/list?keyword=citadel"),
        "query": "download url",
        "queries": ["download url"],
    }


def test_artifact_continuation_batch_search_names_patterns():
    """Batch searches carry no top-level artifact_ref/query: the row must
    still resolve its origin from the first item's ref and show the actual
    patterns being searched — otherwise a 4-pattern batch renders as the
    opaque 'Search 4 saved tool results'."""
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
    from lib.tool_result_artifacts import register_artifact_provenance

    ref = "tool-result:" + "e" * 64
    task: dict = {}
    register_artifact_provenance(
        task, ref, tool_name="web_search",
        display="2 searches: R2E-Gym Procedural Generation",
        llm_round=4, tool_call_id="source_tc_5")

    args = {"searches": [
        {"artifact_ref": ref, "query": "  procedural   generation  "},
        {"artifact_ref": ref, "query": "hybrid environments"},
        {"artifact_ref": ref, "query": "procedural generation"},
    ]}
    _, entry, event = _build_tool_round_entry(
        "search_tool_artifact", args, "tc_6", "{}", 0, False, task=task)
    assert entry["query"] == (
        "Search compacted result of R5 · 2 searches: R2E-Gym Procedural "
        "Generation: procedural generation · hybrid environments")
    assert entry["_artifactOrigin"] == {
        "kind": "search",
        "sourceRound": 5,
        "sourceToolCallId": "source_tc_5",
        "source": "2 searches: R2E-Gym Procedural Generation",
        "query": "procedural generation",
        "queries": ["procedural generation", "hybrid environments"],
    }
    assert event["_artifactOrigin"] == entry["_artifactOrigin"]


def test_artifact_continuation_label_falls_back_without_provenance():
    """No registered origin (cross-turn read, evicted entry, secondary
    surface with no task) yields a generic label — the content-hash digest
    and cursor offset are machine handles and are never rendered."""
    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry

    args = {"artifact_ref": "tool-result:abcd1234ef", "cursor": "25473"}
    _, entry, _ = _build_tool_round_entry(
        "read_tool_artifact", args, "tc_1", "{}", 0, False, task={})
    assert entry["query"] == "Read saved tool result"
    # No registered origin → no chip meta; the flat label stands alone.
    assert "_artifactOrigin" not in entry
    _, entry2, _ = _build_tool_round_entry(
        "read_tool_artifact", args, "tc_2", "{}", 0, False)
    assert entry2["query"] == "Read saved tool result"
    _, entry3, _ = _build_tool_round_entry(
        "search_tool_artifact", {"artifact_ref": args["artifact_ref"],
                                 "query": "download url"},
        "tc_3", "{}", 0, False, task={})
    assert entry3["query"] == "Search saved tool result: download url"
