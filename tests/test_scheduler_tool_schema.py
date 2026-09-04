"""Scheduler model schemas — bounded cost and executable mode contracts."""

from __future__ import annotations

from jsonschema import Draft7Validator
import pytest

from lib.scheduler.tool_defs import SCHEDULE_TOOL_CREATE, TIMER_TOOL_CREATE
from lib.tools.gateway import sanitize_wire_tools, tool_schema_tokens


pytestmark = pytest.mark.unit


def _function(tool: dict) -> dict:
    return tool["function"]


def test_schedule_create_keeps_cadence_execution_and_cost_contracts():
    schema = _function(SCHEDULE_TOOL_CREATE)
    desc = schema["description"].lower()
    props = schema["parameters"]["properties"]
    command = props["command"]["description"].lower()
    predicate = props["condition_command"]["description"].lower()
    tools_config = props["tools_config"]["description"]

    assert "durable" in desc and "max 100" in desc
    assert "local-time five-field cron" in desc
    assert "minute hour day month weekday" in desc
    assert "once:yyyy-mm-dd hh:mm" in desc and "auto-disable" in desc
    assert "cron repeats" in desc and "max_executions=1" in desc
    assert "approximate times" in desc and ":00/:30" in desc
    assert "off-minute" in desc and "honor an exact" in desc
    assert "command=shell" in desc and "python=code" in desc
    assert "disabled by deployment" in desc
    assert "prompt=one llm call without tools" in desc
    assert "agent=independent polls" in desc and "full-tool turn" in desc
    assert "target conversation" in desc

    assert "trigger and action" in command
    assert "pure-code agent" in command and "empty string" in command
    assert "agent-only" in predicate and "exit 0" in predicate
    assert "condition_regex" in predicate and "zero-llm" in predicate
    assert "hybrid" in predicate and "auto-promotes" in predicate
    assert "deterministic" in predicate
    for key in (
        "searchMode", "fetchEnabled", "projectPath", "codeExecEnabled",
        "browserEnabled", "memoryEnabled", "imageGenEnabled", "model",
    ):
        assert key in tools_config
    assert "inherit" in tools_config.lower()
    assert props["target_conv_id"]["description"].lower().startswith(
        "required for agent")
    assert props["task_type"]["enum"] == [
        "command", "python", "prompt", "agent"]
    assert schema["parameters"]["required"] == [
        "name", "schedule", "command"]
    assert props["max_runtime"]["default"] == 300
    assert props["max_executions"]["default"] == 0
    assert tool_schema_tokens([SCHEDULE_TOOL_CREATE]) <= 600


def test_timer_create_keeps_durability_safety_and_poll_modes():
    schema = _function(TIMER_TOOL_CREATE)
    desc = schema["description"].lower()
    params = schema["parameters"]
    props = params["properties"]
    instruction = props["check_instruction"]["description"].lower()
    continuation = props["continuation_message"]["description"].lower()
    evidence = props["check_command"]["description"].lower()
    predicate = props["condition_command"]["description"].lower()

    assert "durable single-shot" in desc and "return immediately" in desc
    assert "do not wait or poll manually" in desc and "server restarts" in desc
    assert "timer_manage" in desc and "fresh authoritative turn" in desc
    assert "auto-disables" in desc
    assert "self-resolving external" in desc and "human-only" in desc
    assert "restarting/redeploying this tofu server" in desc
    assert "stop and ask the user" in desc and "verify after they act" in desc
    assert "independent" in desc and "no cross-poll history" in desc
    assert "same search, shell, and file tools" in desc

    assert "optional" in instruction and "tool-capable poll llm" in instruction
    assert "ready/error" in instruction and "omit only with" in instruction
    assert "required user message" in continuation and "injected" in continuation
    assert "full tool-capable turn" in continuation and "next action" in continuation
    assert "evidence command" in evidence and "informs but does not decide" in evidence
    assert "decisive shell predicate" in predicate and "exit 0" in predicate
    assert "condition_regex" in predicate and "zero-llm" in predicate
    assert "cheapest" in predicate and "hybrid" in predicate
    assert "auto-promotes" in predicate and "deterministic" in predicate
    assert props["poll_interval"]["default"] == 60
    assert "minimum 10" in props["poll_interval"]["description"].lower()
    assert props["max_polls"]["default"] == 120
    assert "0=unlimited" in props["max_polls"]["description"].lower()
    assert params["required"] == ["continuation_message"]
    assert params["anyOf"] == [
        {
            "properties": {"check_instruction": {"type": "string"}},
            "required": ["check_instruction"],
        },
        {
            "properties": {"condition_command": {"type": "string"}},
            "required": ["condition_command"],
        },
    ]
    assert params["type"] == "object"
    wire = [TIMER_TOOL_CREATE]
    assert sanitize_wire_tools(wire) is wire
    kimi_parameters = sanitize_wire_tools(
        wire, model='kimi-k3')[0]['function']['parameters']
    assert kimi_parameters['type'] == 'object'
    assert 'anyOf' not in kimi_parameters
    assert tool_schema_tokens([TIMER_TOOL_CREATE]) <= 450
    assert tool_schema_tokens(
        [SCHEDULE_TOOL_CREATE, TIMER_TOOL_CREATE]) <= 1000


def test_timer_schema_accepts_llm_hybrid_or_code_only_but_not_no_condition():
    params = _function(TIMER_TOOL_CREATE)["parameters"]
    Draft7Validator.check_schema(params)
    validator = Draft7Validator(params)

    assert validator.is_valid({
        "check_instruction": "Check whether CI finished",
        "continuation_message": "Inspect the result",
    })
    assert validator.is_valid({
        "check_instruction": "Check whether CI finished",
        "condition_command": "test -f /tmp/ready",
        "continuation_message": "Inspect the result",
    })
    assert validator.is_valid({
        "condition_command": "test -f /tmp/ready",
        "continuation_message": "Inspect the result",
    })
    assert not validator.is_valid({"continuation_message": "Inspect result"})


def test_timer_adapter_executes_code_only_schema_path(monkeypatch):
    from lib.scheduler.executor._timer import _execute_timer_create

    captured = {}

    def _create_timer(**kwargs):
        captured.update(kwargs)
        return {
            "id": "tmr_code", "check_command": "",
            "condition_kind": "code", "condition_command": "test -f ready",
            "poll_interval": 60, "max_polls": 120,
        }

    monkeypatch.setattr("lib.scheduler.timer.create_timer", _create_timer)
    monkeypatch.setattr("lib.scheduler.timer.start_timer_loop", lambda *_a, **_k: None)

    result = _execute_timer_create({
        "_user_id": 7,
        "_source_conv_id": "conv-code",
        "continuation_message": "Inspect the completed output",
        "condition_command": "test -f ready",
    })

    assert "tmr_code" in result
    assert captured["check_instruction"] == ""
    assert captured["condition_command"] == "test -f ready"
    assert captured["continuation_message"] == "Inspect the completed output"
