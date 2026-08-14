"""Contracts for the agent-runner → flow tool-usage boundary."""

from pathlib import Path

import pytest

from lib.orchestration_tool_usage import (
    OrchestrationToolUsage,
    classify_orchestration_tool_usage,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_tool_names_project_to_detached_state_and_exploration_counts():
    source = ['write_file', 'read_file', 'write_file']
    usage = classify_orchestration_tool_usage({'tool_names': source})
    source.append('apply_diff')

    assert usage == OrchestrationToolUsage(
        state_changing_tools=('write_file', 'write_file'),
        exploratory_tools=('read_file',),
        reported=True,
    )
    legacy = usage.engine_tuple()
    assert legacy == (2, 1, ['write_file', 'write_file'], True)
    legacy[2].append('mutated')
    assert usage.state_changing_tools == ('write_file', 'write_file')


def test_subagent_tool_log_and_single_string_share_one_projection():
    logged = classify_orchestration_tool_usage({
        'tool_log': [
            {'tool': 'apply_diff'},
            {'toolName': 'web_search'},
            'code_exec',
        ],
    })
    named = classify_orchestration_tool_usage({'tool_names': 'write_file'})

    assert logged.state_changing_tools == ('apply_diff', 'code_exec')
    assert logged.exploratory_tools == ('web_search',)
    assert named.engine_tuple() == (1, 0, ['write_file'], True)


def test_malformed_telemetry_fails_safe_without_false_runner_support():
    malformed = classify_orchestration_tool_usage({
        'tool_log': [None, 3, {}, {'tool': []}, {'toolName': '  '}],
    })

    assert malformed.engine_tuple() == (0, 0, [], True)
    assert classify_orchestration_tool_usage({}).reported is False
    assert classify_orchestration_tool_usage(None).reported is False
    assert classify_orchestration_tool_usage({'tool_names': []}).reported is True


def test_tool_names_stays_authoritative_when_both_shapes_are_present():
    usage = classify_orchestration_tool_usage({
        'tool_names': [],
        'tool_log': [{'tool': 'write_file'}],
    })
    fallback = classify_orchestration_tool_usage({
        'tool_names': None,
        'tool_log': [{'tool': 'write_file'}],
    })

    assert usage.engine_tuple() == (0, 0, [], True)
    assert fallback.engine_tuple() == (1, 0, ['write_file'], True)


def test_flow_executor_delegates_runner_shape_compatibility():
    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()

    assert 'normalize_orchestration_agent_result(' in engine
    assert ').tool_usage.engine_tuple()' in engine
    assert "res.get('tool_log')" not in engine
    assert "res.get('tool_names')" not in engine
