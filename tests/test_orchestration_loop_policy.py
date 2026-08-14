"""Shared safety policy for Endpoint and generic orchestration loops."""

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


def test_endpoint_and_graph_runtime_consume_the_same_loop_policy():
    policy = (ROOT / 'lib/orchestration/loop_policy.py').read_text()
    endpoint = (ROOT / 'lib/tasks_pkg/endpoint/_run.py').read_text()
    replan = (ROOT / 'lib/tasks_pkg/endpoint/_replan.py').read_text()
    graph = (ROOT / 'lib/orchestration_loop_runtime.py').read_text()

    assert MAX_REPLANS == 3
    assert DEFAULT_MAX_ITERATIONS == 10
    assert DEFAULT_EXECUTOR_MAX_ITERATIONS == 12
    assert policy.count('MAX_REPLANS = 3') == 1
    assert policy.count('MAX_ZERO_DELIVERABLE_TURNS = 2') == 1
    for source in (endpoint, graph):
        assert 'advance_zero_deliverable_streak(' in source
        assert 'should_inject_zero_deliverable(' in source
    assert 'MAX_ITERATIONS = loop_policy.DEFAULT_MAX_ITERATIONS' in replan
    assert 'MAX_REPLANS = 3' not in replan + graph
    assert 'MAX_ZERO_DELIVERABLE_TURNS = 2' not in replan + graph


def test_all_default_execution_entry_points_consume_the_shared_policy():
    engine = (ROOT / 'lib/orchestration_engine.py').read_text()
    builtins = (
        ROOT / 'lib/orchestration/_builtin_definitions.py').read_text()
    chat = (ROOT / 'lib/orchestration_endpoint_runner.py').read_text()
    controls = (ROOT / 'lib/orchestration/_control_specs.py').read_text()

    for source in (engine, builtins, chat, controls):
        assert 'DEFAULT_EXECUTOR_MAX_ITERATIONS' in source
    for source in (builtins, chat, controls):
        assert 'DEFAULT_MAX_ITERATIONS' in source
    assert '_DEFAULT_MAX_ITERATIONS = 12' not in engine
    assert "or 12" not in chat
