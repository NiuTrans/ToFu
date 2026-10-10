"""Usage-free upstream failures stay auditable without invalidating a trial.

tb21 v11 evidence: one ``upstream_error`` row among dozens of completed
chatCompletions invalidated the whole trial's proxy metrics even though the
CLI/proxy token accounting reconciled exactly. The validator must tolerate
rows that provably carry no billable usage, count them for audit, and keep
failing closed on anything accounting-relevant (usage-bearing failures,
client disconnects, invalid trials, model-pin violations).

Run: pytest tests/test_opencode_proxy_metrics_tolerance.py -m unit
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from evaluations.opencode_proxy.metrics import (
    _usage_free_upstream_failure,
    validate_opencode_proxy_metrics,
)

_TOKEN = "ab" * 32


def _completion(status="completed", *, usage=None, disconnected=False,
                finish=None, invalid=False):
    return {
        "event": "chatCompletion",
        "trialToken": _TOKEN,
        "model": "glm-5.3-m17",
        "status": status,
        "clientDisconnected": disconnected,
        "invalidTrial": invalid,
        "finishReasons": finish if finish is not None else (
            [] if status != "completed" else ["stop"]),
        "upstreamCalls": 1,
        "usage": usage if usage is not None else (
            {} if status != "completed" else {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            }),
    }


def _write_rows(tmp_path, rows):
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return str(path)


@pytest.mark.unit
class TestUsageFreeUpstreamFailure:
    def test_v11_shape_is_usage_free(self):
        assert _usage_free_upstream_failure(_completion(
            "upstream_error")) is True

    def test_completed_rows_are_never_failures(self):
        assert _usage_free_upstream_failure(_completion("completed")) is False

    def test_usage_bearing_failure_is_not_usage_free(self):
        assert _usage_free_upstream_failure(_completion(
            "upstream_error",
            usage={"prompt_tokens": 5, "completion_tokens": 1,
                   "total_tokens": 6})) is False

    def test_disconnected_row_is_not_usage_free(self):
        assert _usage_free_upstream_failure(_completion(
            "upstream_error", disconnected=True)) is False

    def test_finish_reason_row_is_not_usage_free(self):
        assert _usage_free_upstream_failure(_completion(
            "upstream_error", finish=["stop"])) is False


@pytest.mark.unit
class TestValidateToleratesUsageFreeFailures:
    def test_mixed_completed_and_upstream_error_is_valid(self, tmp_path):
        rows = [_completion("completed") for _ in range(3)]
        rows.insert(1, _completion("upstream_error"))
        rows.insert(2, _completion("upstream_error"))
        result = validate_opencode_proxy_metrics(
            _write_rows(tmp_path, rows), expected_trial_token=_TOKEN)
        assert result["valid"] is True
        assert result["chatCompletions"] == 5
        assert result["accountedChatCompletions"] == 3
        assert result["upstreamFailures"] == 2
        assert result["upstreamFailureStatuses"] == ["upstream_error"]
        # Usage totals still cover every row (failures contribute zero).
        assert result["usage"]["prompt_tokens"] == 300
        assert result["usage"]["completion_tokens"] == 60

    def test_no_completed_rows_is_invalid(self, tmp_path):
        result = validate_opencode_proxy_metrics(
            _write_rows(tmp_path, [_completion("upstream_error")]),
            expected_trial_token=_TOKEN)
        assert result["valid"] is False

    def test_usage_bearing_failure_stays_invalid(self, tmp_path):
        rows = [
            _completion("completed"),
            _completion("upstream_error", usage={
                "prompt_tokens": 5, "completion_tokens": 1,
                "total_tokens": 6}),
        ]
        result = validate_opencode_proxy_metrics(
            _write_rows(tmp_path, rows), expected_trial_token=_TOKEN)
        assert result["valid"] is False
        assert result["upstreamFailures"] == 0

    def test_disconnected_row_stays_invalid(self, tmp_path):
        rows = [
            _completion("completed"),
            _completion("completed", disconnected=True),
        ]
        result = validate_opencode_proxy_metrics(
            _write_rows(tmp_path, rows), expected_trial_token=_TOKEN)
        assert result["valid"] is False

    def test_invalid_trial_row_stays_invalid(self, tmp_path):
        rows = [
            _completion("completed"),
            _completion("upstream_error", invalid=True),
        ]
        result = validate_opencode_proxy_metrics(
            _write_rows(tmp_path, rows), expected_trial_token=_TOKEN)
        assert result["valid"] is False

    def test_model_pin_violation_stays_invalid(self, tmp_path):
        rows = [
            _completion("completed"),
            {"event": "modelPinViolation", "trialToken": _TOKEN},
        ]
        result = validate_opencode_proxy_metrics(
            _write_rows(tmp_path, rows), expected_trial_token=_TOKEN)
        assert result["valid"] is False
        assert result["violations"] == 1
