"""Release reporting derives gates from evidence and never averages blind judges."""

from __future__ import annotations

from copy import deepcopy

import pytest

from evaluations.long_agent_release.report import (
    _arm_summary,
    _judge_summary,
)


pytestmark = pytest.mark.unit


def _judges(order: str) -> list[dict]:
    return [
        {"judge": name, "passed": True, "blind": True, "order": order}
        for name in ("claude-opus-5", "glm-5.3")
    ]


def test_both_blind_judges_must_pass_every_subjective_task():
    records = [
        {"taskId": "research-1", "family": "frozen_research",
         "judges": _judges("AB")},
        {"taskId": "writing-1", "family": "long_writing",
         "judges": _judges("BA")},
    ]

    passed = _judge_summary(records)

    assert passed["allPass"] is True
    assert passed["orderCounts"] == {"AB": 2, "BA": 2}
    disagreed = deepcopy(records)
    disagreed[1]["judges"][1]["passed"] = False
    result = _judge_summary(disagreed)
    assert result["allPass"] is False
    assert result["perJudge"]["claude-opus-5"]["allPass"] is True
    assert result["perJudge"]["glm-5.3"]["allPass"] is False


def test_arm_summary_reports_cost_latency_tokens_search_and_critical_failures():
    record = {
        "taskId": "task-1",
        "family": "integrated_multi_tool",
        "oracle": {"passed": False},
        "rounds": [{"usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cache_read_tokens": 40,
            "reasoning_tokens": 5,
        }}],
        "contextBlocks": [{"tokenCount": 12}],
        "toolSchemas": [{"schemaTokens": 9}],
        "toolResults": [
            {"toolName": "search_tools", "resultTokens": 7},
            {"toolName": "execute_tools", "resultTokens": 8},
        ],
        "compactions": [{"evidenceRetained": False}],
        "retries": [{"code": "qemu_start", "failureClass": "infrastructure"}],
        "incidents": [{
            "severity": "error", "code": "false_completion",
        }],
        "cost": {"agentCostUsd": 0.25},
        "latency": {
            "oracleReadyMs": 100,
            "codexFavoredCorrectedWallMs": 90,
            "ttftMs": 10,
            "modelMs": 70,
            "toolMs": 20,
        },
    }

    summary = _arm_summary([record])

    assert summary["successes"] == 0
    assert summary["agentCostPerSuccessUsd"] is None
    assert summary["usage"] == {
        "inputTokens": 100,
        "outputTokens": 20,
        "cacheReadTokens": 40,
        "cacheWriteTokens": 0,
        "reasoningTokens": 5,
        "rounds": 1,
    }
    assert summary["context"] == {"blocks": 1, "tokens": 12}
    assert summary["toolSearch"]["taskAdoptionRate"] == 1
    assert summary["compactions"]["evidenceRetentionFailures"] == 1
    assert summary["failures"]["criticalIncidents"] == 1
    assert summary["failures"]["infrastructureRetries"] == 1
    assert summary["latency"]["formalCodexFavoredP90Ms"] == 90
