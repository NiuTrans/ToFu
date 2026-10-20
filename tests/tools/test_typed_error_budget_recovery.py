"""Budgeting preserves recovery policy and counts the final typed-error view."""
from dataclasses import replace

import pytest

from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import split_tool_result_delivery, tool_result_error, typed_tool_error

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("retryable", [False, True])
def test_truncated_typed_error_keeps_owner_advice_without_inventing_replay(retryable, monkeypatch):
    failure = typed_tool_error(
        "owned_failure", retryable=retryable, message="Original failure.",
        next_action="Inspect the prior outcome; continue independent authorized work.")
    source = replace(failure, summary="Original diagnostic detail. " * 1000).to_envelope_text()
    monkeypatch.setattr(_budget, "TOOL_RESULT_V2_MAX_TOKENS", 160)
    result = _budget.budget_tool_result_v2("write_file", source, user_id=0)
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence["truncated"]
    assert tool_result_error(delivery.model_text) == failure.error
    assert "rerun" not in delivery.model_text.lower()
    assert result.canonical.text() == source.canonical.text()


def test_final_error_metadata_is_included_in_the_result_token_budget(monkeypatch):
    failure = typed_tool_error(
        "execution_outcome_unknown", retryable=False,
        message="The operation returned an unknown outcome.",
        next_action="Inspect the prior outcome before retrying. Independent work may continue.")
    source = replace(failure, summary="Detailed diagnostic with quoted \"data\".\n" * 1000).to_envelope_text()
    monkeypatch.setattr(_budget, "TOOL_RESULT_V2_MAX_TOKENS", 160)
    result = _budget.budget_tool_result_v2("write_file", source, user_id=0)
    assert _budget._model_result_tokens(result, "") <= 160
    assert tool_result_error(result) == failure.error


def test_unavailable_generic_result_does_not_authorize_repeating_effects(monkeypatch):
    source = "The operation finished. Original output.\n" * 1000
    monkeypatch.setattr(_budget, "TOOL_RESULT_V2_MAX_TOKENS", 160)
    result = _budget.budget_tool_result_v2("write_file", source, user_id=0)
    delivery = split_tool_result_delivery(result)
    assert delivery.evidence["truncated"]
    assert "rerun" not in delivery.model_text.lower()
    assert "prior outcome" in delivery.model_text.lower()
    assert result.canonical.text() == source
