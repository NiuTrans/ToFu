"""Real gateway child settlement preserves operation-scoped recovery evidence."""
import asyncio
from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.progress_evidence import nonretryable_failure_signature
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tasks_pkg.executor._finalize import _finalize_tool_round
from lib.tasks_pkg.handlers.tool_gateway import handle_execute_tools
from lib.tasks_pkg.tool_display import _build_tool_round_entry
from lib.tools.contracts import ToolContractV2
from lib.tools.result_envelope import (
    split_tool_result_delivery, tool_result_error, typed_tool_error,
)
from tests.tasks_pkg.test_tool_outcome_finality import observations as observations, task_for

pytestmark = pytest.mark.unit


def projected_error(text):
    from lib.tools.result_projection_text import load_text_field
    from lib.tools.result_surfaces_generated import CONTRACT
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        value = load_text_field(text, CONTRACT['errorProjectionText']['bodyField'])
        assert value is not None
        return value


def run_batch(outcomes, mode="v2", *, url_prefix="https://example.test",
              error_prefix="source", through_pipeline=False):
    executed = []

    def handler(task, tc, name, call_id, arguments, number, row, *rest):
        index = int(arguments["url"].rsplit("/", 1)[1])
        executed.append(index)
        outcome = outcomes[index]
        if outcome == "success":
            return call_id, f"completed observation {index}", False
        if outcome == "legacy":
            _finalize_tool_round(task, number, row, [{"content": "opaque failure"}],
                                 status="error")
            return call_id, "opaque failure", False
        return call_id, typed_tool_error(
            f"{error_prefix}_{index}_unavailable",
            retryable=outcome == "retryable",
            message=f"original failure {index}",
            next_action=f"Inspect alternative source {index}.",
        ).to_envelope_text(), False

    task = task_for(handler, mode)
    task.update(convId="", _transientRuntime=True, _suppressEvents=True, toolRounds=[])
    parameters = {"type": "object", "properties": {"url": {"type": "string"}},
                  "required": ["url"]}
    task["_executable_tool_catalog"] = [{
        "type": "function", "function": {"name": "fetch_url", "parameters": parameters},
    }]
    task["_toolContractDocumentsByName"] = {
        "fetch_url": ToolContractV2(
            name="fetch_url", parameters=parameters, permission="read",
            model_description="Read a declared source.").search_document(),
    }
    arguments = {"calls": [
        {"name": "fetch_url", "arguments": {"url": f"{url_prefix}/{index}"}}
        for index in range(len(outcomes))
    ], "execution": "parallel"}
    call = {"id": "batch", "type": "function", "function": {
        "name": "execute_tools", "arguments": json.dumps(arguments)}}
    _, row, _ = _build_tool_round_entry(
        "execute_tools", arguments, call["id"], call["function"]["arguments"], 1, False)
    row["llmRound"] = 0
    task["toolRounds"].append(row)
    if through_pipeline:
        from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
        from lib.tools.gateway import execute_tools_schema
        from lib.tools.result_value import TrustedResultText
        gateway = execute_tools_schema(include_program=False)
        task["_toolContractDocumentsByName"]["execute_tools"] = ToolContractV2(
            name="execute_tools", parameters=gateway["function"]["parameters"],
            permission="read", model_description="Run declared read calls.").search_document()
        task["_tool_env"] = SimpleNamespace(resolve=lambda name:
            handle_execute_tools if name == "execute_tools" else handler)
        messages = [{"role": "assistant", "content": "", "tool_calls": [call]}]
        captured = []
        execute_tool_pipeline(
            task, [(call, "execute_tools", call["id"], arguments, 1, row, None)],
            {"autoApply": True}, None, False, [gateway], messages, [], 0, "test-model",
            internal_result_sink=SimpleNamespace(
                capture=lambda call_id, canonical: captured.append(canonical)))
        text = messages[-1]["content"]
        return task, call, text, TrustedResultText(text, canonical=captured[0]), executed
    _, content, _ = handle_execute_tools(
        task, call, "execute_tools", call["id"], arguments, 1, row,
        {"autoApply": True}, None, False)
    delivered = budget_tool_result_v2("execute_tools", content, user_id=0) if mode == "v2" else content
    return task, call, split_tool_result_delivery(delivered).model_text, content, executed


@pytest.mark.parametrize("mode", ["legacy", "v2"])
def test_all_explicit_nonretryable_children_remain_classified_after_delivery(mode, observations):
    task, call, model_text, content, executed = run_batch(["permanent", "permanent"], mode)
    assert sorted(executed) == [0, 1]
    assert [row["status"] for row in task["toolRounds"]] == ["error", "error", "error"]
    error = tool_result_error(model_text)
    assert error is not None, "batch delivery erased every child's nonretryable classification"
    assert error.code == "tool_batch_failed" and error.retryable is False
    assert nonretryable_failure_signature(call, model_text)
    assert nonretryable_failure_signature(call, content) == nonretryable_failure_signature(call, model_text)
    for index in range(2):
        assert f"original failure {index}" in model_text
        assert f"Inspect alternative source {index}." in model_text
        assert f"source_{index}_unavailable" in content.canonical.text()
    assert len(content.canonical.children) == 2
    assert "duration" not in model_text and "raw_output_bytes" not in model_text


@pytest.mark.parametrize("mode", ["legacy", "v2"])
def test_outer_pipeline_retains_batch_failure_without_reexecuting_children(mode, observations):
    task, call, text, content, executed = run_batch(
        ["permanent", "permanent"], mode, through_pipeline=True)
    assert sorted(executed) == [0, 1]
    assert tool_result_error(text).code == "tool_batch_failed"
    assert nonretryable_failure_signature(call, text)
    assert content.canonical.execution_status == "error"
    assert len(content.canonical.children) == 2
    assert [row["status"] for row in task["toolRounds"]] == ["error", "error", "error"]
    assert len(observations[1]) == 3


@pytest.mark.parametrize("outcomes", [
    ["permanent", "success"],
    ["permanent", "retryable"],
    ["retryable", "retryable"],
    ["permanent", "legacy"],
])
def test_mixed_success_retryable_or_unknown_batches_do_not_trip_terminal_failure(
        outcomes, observations):
    _, call, model_text, _, executed = run_batch(outcomes)
    assert sorted(executed) == [0, 1]
    assert nonretryable_failure_signature(call, model_text) is None
    if "success" in outcomes:
        assert "completed observation 1" in model_text


def test_gateway_failure_signature_ignores_only_declared_child_correlation_ids(observations):
    _, call, text, _, _ = run_batch(["permanent", "permanent"])
    arguments = json.loads(call["function"]["arguments"])
    arguments["calls"][0]["call_id"] = "first-response"
    arguments["calls"][0]["arguments"]["call_id"] = "business-value"
    call["function"]["arguments"] = json.dumps(arguments)
    original = deepcopy(call)
    same = deepcopy(call)
    arguments["calls"][0]["call_id"] = "another-response"
    same["function"]["arguments"] = json.dumps(arguments)
    assert nonretryable_failure_signature(call, text) == nonretryable_failure_signature(same, text)
    assert call == original
    arguments["calls"][0]["arguments"]["call_id"] = "corrected-business-value"
    same["function"]["arguments"] = json.dumps(arguments)
    assert nonretryable_failure_signature(call, text) != nonretryable_failure_signature(same, text)
    # A source tool's same-named input is business data, not gateway metadata.
    arguments = json.loads(call["function"]["arguments"])
    arguments["calls"][0]["call_id"] = "new-input"
    call["function"]["name"] = same["function"]["name"] = "source_tool"
    same["function"]["arguments"] = json.dumps(arguments)
    assert nonretryable_failure_signature(call, text) != nonretryable_failure_signature(same, text)


def test_native_progress_receives_failure_and_resets_for_a_recoverable_batch(observations):
    from lib.agent_core.native_process import ProgressLimits, ProgressRequest
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.native_progress import NativeGatewayProgress

    task, call, text, _, _ = run_batch(["permanent", "permanent"])
    gateway = SimpleNamespace(_closed=False, owner=1, task_id=task["id"], task=task)
    progress = NativeGatewayProgress(gateway, ProgressLimits(2, 3), 4096)
    principal = PrincipalContext.user(subject_id="test", owner_user_id=1)
    progress.record(0, call, text)
    observed = asyncio.run(progress.observe(
        ProgressRequest(principal, task["id"], 0, 1, threading.Event())))
    assert observed["resultEvidenceComplete"]
    assert observed["nonretryableFailureSignatures"] == [nonretryable_failure_signature(call, text)]
    _, call, text, _, _ = run_batch(["permanent", "retryable"])
    progress.record(1, call, text)
    observed = asyncio.run(progress.observe(
        ProgressRequest(principal, task["id"], 1, 1, threading.Event())))
    assert observed["resultEvidenceComplete"]
    assert observed["nonretryableFailureSignatures"] == []


def test_changed_child_error_cause_starts_a_new_recovery_attempt(observations):
    _, call, first, _, _ = run_batch(["permanent", "permanent"])
    _, same_call, changed, _, _ = run_batch(
        ["permanent", "permanent"], error_prefix="different_condition")
    assert call == same_call
    assert nonretryable_failure_signature(call, first) != nonretryable_failure_signature(call, changed)


@pytest.mark.parametrize("items", [
    [], [{}], [{"failureCodes": []}], [{"failureCodes": ["missing-child"]}],
    [{"failureCodes": [1, 2]}], [{"failureCodes": ["", "unknown"]}],
])
def test_incomplete_failure_identity_cannot_claim_an_unchanged_batch(items, observations):
    _, call, text, _, executed = run_batch(["permanent", "permanent"])
    result = projected_error(text)
    result["items"] = items
    assert tool_result_error(result).retryable is False
    assert nonretryable_failure_signature(call, result) is None
    assert sorted(executed) == [0, 1]


@pytest.mark.parametrize("aggregate_pressure", [False, True])
@pytest.mark.parametrize("corrected", [False, True])
def test_root_breaker_stops_unchanged_batches_but_allows_correction(
        corrected, aggregate_pressure, observations, monkeypatch):
    from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root

    def batch_result(outcomes, **kwargs):
        return run_batch(
            outcomes, through_pipeline=aggregate_pressure,
            error_prefix="classified_failure_" * 3 if aggregate_pressure else "source",
            **kwargs)

    if aggregate_pressure:
        monkeypatch.setattr(
            "lib.tasks_pkg.compaction._budget.ROUND_TOOL_RESULT_V2_MAX_TOKENS", 220)
        monkeypatch.setattr(
            "lib.tasks_pkg.compaction._budget._store_tool_result_artifact",
            lambda *a, **kw: "")
    first = batch_result(["permanent", "permanent"])
    if aggregate_pressure:
        assert projected_error(first[2])["truncated"] is True
        assert nonretryable_failure_signature(first[1], first[2])
    task = first[0]
    request = SimpleNamespace(
        task=task, state=SimpleNamespace(), messages=[], all_search_results_text=[],
        tid=task["id"], cfg={}, project_path=None, project_enabled=False, tool_list=[])
    hooks = root._RootLoopHooks(request)
    hooks.round_context["stream_accumulator"] = SimpleNamespace(announced_tc_map={})
    receipts = {}
    executed = []

    def dispatch(round_num, tools):
        assert round_num <= 4, 'the finite provider script cannot emit another tool round'
        if corrected and round_num == 4:
            return {"role": "assistant", "content": "completed"}, "stop", {}
        outcomes = ["success", "success"] if corrected and round_num == 3 else ["permanent", "permanent"]
        prefix = "https://alternative.test" if corrected and round_num >= 2 else "https://example.test"
        batch = first if round_num == 0 else batch_result(outcomes, url_prefix=prefix)
        _, call, text, content, entered = batch
        executed.extend(entered)
        call = deepcopy(call)
        call["id"] = f"response-{round_num}"
        receipts[round_num] = (call, text, content.canonical)
        message = {"role": "assistant", "content": "", "tool_calls": [call]}
        request.messages.append(message)
        return message, "tool_calls", {}

    def publish(owner, state, messages, searches, *, round_num, **kwargs):
        call, text, canonical = receipts[round_num]
        kwargs['internal_result_sink'].capture(call['id'], canonical)
        messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
        return False

    monkeypatch.setattr(root, "run_tool_dispatch", publish)
    outcome = run_agent_loop(
        abort=AbortSignal.never(), round_tools=None, dispatch=dispatch,
        execute_tools=hooks.execute_tools,
        max_consecutive_no_progress_rounds=0,
        max_consecutive_nonretryable_failure_rounds=3,
        on_nonretryable_failure_state=hooks.observe_nonretryable_failure_state,
    )
    assert any("correct the arguments" in str(message.get("content")) for message in request.messages)
    if corrected:
        assert outcome.completed and outcome.exit_reason == "completed"
        assert outcome.consecutive_nonretryable_failure_rounds == 0
        assert len(executed) == 8
    else:
        assert outcome.halted and outcome.exit_reason == "nonretryable_tool_failure"
        assert outcome.consecutive_nonretryable_failure_rounds == 3
        assert len(executed) == 6


def test_delivery_budget_preserves_typed_failure_when_child_details_spill(observations, monkeypatch):
    _, call, _, content, executed = run_batch(["permanent", "permanent"])
    monkeypatch.setattr("lib.tasks_pkg.compaction._budget.TOOL_RESULT_V2_MAX_TOKENS", 160)
    delivered = budget_tool_result_v2("execute_tools", content, user_id=0)
    delivery = split_tool_result_delivery(delivered)
    assert delivery.evidence["truncated"]
    assert tool_result_error(delivery.model_text).retryable is False
    retained_items = projected_error(delivery.model_text).get("items", [])
    if retained_items:
        assert nonretryable_failure_signature(call, delivery.model_text) == nonretryable_failure_signature(call, content)
    else:
        # A bounded view can retain the failure while losing its cause identity.
        # Missing evidence cannot prove a repeated, unchanged failure.
        assert nonretryable_failure_signature(call, delivery.model_text) is None
    assert "rerun" not in delivery.model_text.lower()
    assert sorted(executed) == [0, 1]
    assert len(delivered.canonical.children) == 2


def test_same_occurrence_replays_typed_failure_without_rerunning_children(observations):
    task, call, text, content, executed = run_batch(["permanent", "permanent"])
    _, replay, _ = handle_execute_tools(
        task, call, "execute_tools", call["id"], json.loads(call["function"]["arguments"]),
        1, task["toolRounds"][0], {"autoApply": True}, None, False)
    assert replay == content and replay.canonical is content.canonical
    assert tool_result_error(split_tool_result_delivery(replay).model_text).retryable is False
    assert sorted(executed) == [0, 1]
