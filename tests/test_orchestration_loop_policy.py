"""Shared safety policy for orchestration loops."""

from pathlib import Path

import pytest

from lib.orchestration.loop_policy import (
    DEFAULT_EXECUTOR_MAX_ITERATIONS,
    DEFAULT_MAX_ITERATIONS,
    MAX_REPLANS,
    MAX_ZERO_DELIVERABLE_TURNS,
    advance_zero_deliverable_streak,
    should_inject_zero_deliverable,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_zero_deliverable_streak_has_one_shared_progression_policy():
    streak = advance_zero_deliverable_streak(
        0, reported=True, state_changing=0)
    assert streak == 1
    assert should_inject_zero_deliverable(streak) is False
    streak = advance_zero_deliverable_streak(
        streak, reported=True, state_changing=0)
    assert streak == MAX_ZERO_DELIVERABLE_TURNS
    assert should_inject_zero_deliverable(streak) is True
    assert advance_zero_deliverable_streak(
        streak, reported=True, state_changing=1) == 0
    assert advance_zero_deliverable_streak(
        streak, reported=False, state_changing=0) == 0


def test_graph_runtime_consumes_the_shared_loop_policy():
    policy = (ROOT / 'lib/orchestration/loop_policy.py').read_text()
    graph = (ROOT / 'lib/orchestration_loop_runtime.py').read_text()

    assert MAX_REPLANS == 3
    assert DEFAULT_MAX_ITERATIONS == 10
    assert DEFAULT_EXECUTOR_MAX_ITERATIONS == 12
    assert policy.count('MAX_REPLANS = 3') == 1
    assert policy.count('MAX_ZERO_DELIVERABLE_TURNS = 2') == 1
    assert 'advance_zero_deliverable_streak(' in graph
    assert 'should_inject_zero_deliverable(' in graph
    assert 'MAX_REPLANS = 3' not in graph
    assert 'MAX_ZERO_DELIVERABLE_TURNS = 2' not in graph


def test_all_default_execution_entry_points_consume_the_shared_policy():
    engine = (ROOT / 'lib/orchestration_engine.py').read_text()
    builtins = (
        ROOT / 'lib/orchestration/_builtin_definitions.py').read_text()
    chat = (ROOT / 'lib/orchestration_chat_flow_runner.py').read_text()
    controls = (ROOT / 'lib/orchestration/_control_specs.py').read_text()

    for source in (engine, builtins, chat, controls):
        assert 'DEFAULT_EXECUTOR_MAX_ITERATIONS' in source
    assert 'DEFAULT_MAX_ITERATIONS' in controls
    assert '_DEFAULT_MAX_ITERATIONS = 12' not in engine
    assert "or 12" not in chat
