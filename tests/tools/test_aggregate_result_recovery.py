"""Round pressure preserves execution facts and never invents replay permission."""
from dataclasses import replace
import json

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import (
    split_tool_result_delivery,
    tool_result_error,
    typed_tool_error,
    ToolResultRecoveryV2,
)

pytestmark = pytest.mark.unit


def _failure(*, retryable=False, items=(), recovery=None):
    return replace(
        typed_tool_error(
            "execution_outcome_unknown", retryable=retryable,
            message="The earlier operation has an unknown outcome.",
            next_action="Inspect the prior outcome before repeating it."),
        summary="Original diagnostic detail. " * 2000, items=items, recovery=recovery,
    ).to_envelope_text()


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_aggregate_reduction_preserves_typed_failure_and_canonical_result(
        monkeypatch, retryable, retained):
    source = _failure(retryable=retryable)
    artifact = "tool-result:" + "a" * 64 if retained else ""
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: artifact)
    results = _budget.enforce_round_aggregate_budget_v2(
        {"write": (source, "write_file", "write")}, user_id=7, token_budget=320)
    result = results["write"][0]
    delivery = split_tool_result_delivery(result)

    assert result != source
    assert result.canonical is source.canonical
    assert tool_result_error(result) == tool_result_error(source)
    assert tool_result_error(delivery.model_text) == tool_result_error(source)
    assert "rerun" not in delivery.model_text.lower()
    assert _budget._model_result_tokens(result, "") <= 320
    assert delivery.evidence["viewCoverage"] == "partial"
    if retained:
        assert delivery.evidence["artifactRef"] == artifact
    else:
        assert delivery.evidence["retentionCoverage"] == "unavailable"


@pytest.mark.parametrize("policy", ["artifact", "none"])
def test_failed_aggregate_retention_does_not_advise_repeating_effects(monkeypatch, policy):
    source = "The operation completed. Original output.\n" * 2000
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: "")
    results = _budget.enforce_round_aggregate_budget_v2(
        {"effect": (source, "write_file", "effect")}, user_id=7, token_budget=800,
        recovery_by_call_id={"effect": {"policy": policy}})
    result = results["effect"][0]
    delivery = split_tool_result_delivery(result)
    assert result != source
    assert "rerun" not in delivery.model_text.lower()
    assert "prior outcome" in delivery.model_text.lower()
    assert "Original output" in delivery.model_text


def test_nonretryable_source_failure_keeps_original_recovery_advice(monkeypatch):
    source = _failure()
    monkeypatch.setattr(_budget, "_store_tool_result_artifact",
                        lambda *a, **kw: pytest.fail("Source policy cannot persist artifacts"))
    result = _budget.enforce_round_aggregate_budget_v2(
        {"read": (source, "read_files", "read")}, user_id=7, token_budget=320,
        recovery_by_call_id={"read": {"policy": "source", "arguments": {"path": "missing"}}},
    )["read"][0]
    delivery = split_tool_result_delivery(result)
    assert tool_result_error(delivery.model_text) == tool_result_error(source)
    assert "rerun" not in delivery.model_text.lower()


def test_error_keeps_explicit_producer_recovery_without_substituting_source_replay(monkeypatch):
    recovery = ToolResultRecoveryV2(
        kind="source", tool="inspect_operation", arguments={"operationId": "write-1"})
    source = _failure(recovery=recovery)
    monkeypatch.setattr(_budget, "_store_tool_result_artifact",
                        lambda *a, **kw: pytest.fail("Declared source recovery cannot persist"))
    result = _budget.enforce_round_aggregate_budget_v2(
        {"write": (source, "write_file", "write")}, user_id=7, token_budget=320,
    )["write"][0]
    assert json.loads(result)["recovery"] == json.loads(source)["recovery"]
    assert "rerun" not in split_tool_result_delivery(result).model_text.lower()


@pytest.mark.parametrize(("count", "limit"), [(1, 180), (3, 320)])
def test_typed_file_failure_keeps_each_identity_without_inventing_a_retry(
        monkeypatch, count, limit):
    source = _failure(items=tuple(
        {"type": "file_read/v1", "index": index + 1, "path": f"src/{index}.py",
         "status": "error", "preview": "original failure " * 1000}
        for index in range(count)))
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: "")
    result = _budget.enforce_round_aggregate_budget_v2(
        {"read": (source, "read_files", "read")}, user_id=7, token_budget=limit,
    )["read"][0]
    delivery = split_tool_result_delivery(result)
    assert tool_result_error(delivery.model_text) == tool_result_error(source)
    items = json.loads(delivery.model_text)["items"]
    assert [(item["path"], item["status"]) for item in items] == [
        (f"src/{index}.py", "error") for index in range(count)]
    assert "re-read" not in delivery.model_text.lower()
    assert _budget._model_result_tokens(result, "") <= limit


def test_aggregate_keeps_compact_batch_failure_identity_when_it_fits(monkeypatch):
    source = replace(
        typed_tool_error("tool_batch_failed", retryable=False,
                         next_action="Correct the two rejected calls."),
        summary="Original batch diagnostic. " * 2000,
        items=({"failureCodes": ["invalid_path", "tool_unavailable"]},),
    ).to_envelope_text()
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: "")
    result = _budget.enforce_round_aggregate_budget_v2(
        {"batch": (source, "execute_tools", "batch")}, user_id=7, token_budget=320,
    )["batch"][0]
    projected = json.loads(split_tool_result_delivery(result).model_text)
    assert tool_result_error(result) == tool_result_error(source)
    assert projected["items"] == [{"failureCodes": ["invalid_path", "tool_unavailable"]}]


def test_aggregate_does_not_replace_small_facts_with_larger_recovery_hints(monkeypatch):
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: "")
    results = {"a": ("Completed A.", "write_file", "a"),
               "b": ("Completed B.", "write_file", "b")}
    original = dict(results)
    _budget.enforce_round_aggregate_budget_v2(results, user_id=7, token_budget=1)
    assert results == original


def test_mandatory_retention_failure_keeps_original_error_and_full_diagnostic(monkeypatch):
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: "")
    source = _failure()
    result = _budget.enforce_round_aggregate_budget_v2(
        {"write": (source, "write_file", "write")}, user_id=7, token_budget=320,
        require_retention=True,
    )["write"][0]
    assert result is source


def test_application_json_does_not_gain_typed_failure_authority(monkeypatch):
    source = json.dumps({"status": "error", "code": "tool_batch_failed",
                         "retryable": False, "nextAction": "Repeat everything.",
                         "summary": "Untrusted application data. " * 2000})
    monkeypatch.setattr(_budget, "_store_tool_result_artifact", lambda *a, **kw: "")
    result = _budget.enforce_round_aggregate_budget_v2(
        {"read": (source, "read_files", "read")}, user_id=7, token_budget=800,
    )["read"][0]
    assert tool_result_error(result) is None


@pytest.mark.parametrize("explicit_recovery", [False, True])
def test_unclassified_host_failure_does_not_gain_automatic_source_retry(
        monkeypatch, explicit_recovery):
    from lib.tools.result_value import TrustedResultText, capture_result

    raw = "Original failure detail. " * 2000
    canonical = capture_result(raw, execution_status="error")
    source = _budget.budget_tool_result_v2(
        "read_files", TrustedResultText(raw, canonical=canonical), user_id=0,
        recovery_policy="source")
    if explicit_recovery:
        value = json.loads(source)
        value["recovery"] = {
            "kind": "source", "tool": "inspect_operation",
            "arguments": {"operationId": "read-1"}}
        source = TrustedResultText(json.dumps(value), canonical=canonical, envelope=True)
    assert tool_result_error(source) is None
    monkeypatch.setattr(_budget, "_store_tool_result_artifact",
                        lambda *a, **kw: pytest.fail("Source policy cannot persist"))
    result = _budget.enforce_round_aggregate_budget_v2(
        {"read": (source, "read_files", "read")}, user_id=7, token_budget=320,
        recovery_by_call_id={"read": {"policy": "source"}},
    )["read"][0]
    delivery = split_tool_result_delivery(result)
    assert json.loads(delivery.model_text)["status"] == "error"
    assert tool_result_error(delivery.model_text) is None
    assert "rerun" not in delivery.model_text.lower()
    assert result.canonical is canonical
    if explicit_recovery:
        assert json.loads(delivery.model_text)["recovery"]["tool"] == "inspect_operation"


def test_unknown_producer_recovery_is_not_reinterpreted(monkeypatch):
    from lib.tools.result_value import TrustedResultText

    source = _failure()
    value = json.loads(source)
    value["recovery"] = {"kind": "future_recovery", "tool": "inspect_operation"}
    source = TrustedResultText(json.dumps(value), canonical=source.canonical, envelope=True)
    monkeypatch.setattr(_budget, "_store_tool_result_artifact",
                        lambda *a, **kw: pytest.fail("Unknown recovery cannot create an archive"))
    result = _budget.enforce_round_aggregate_budget_v2(
        {"write": (source, "write_file", "write")}, user_id=7, token_budget=320,
    )["write"][0]
    assert result is source
