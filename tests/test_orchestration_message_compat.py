"""Read-boundary coverage for persisted messages from the retired runner."""

import pytest

from lib.orchestration_message_compat import (
    is_flow_event_type,
    is_flow_turn_kind,
    normalize_flow_message,
)
from lib.tasks_pkg.conv_message_builder._transform import _transform_messages


pytestmark = pytest.mark.unit


def test_flow_event_detection_accepts_current_and_persisted_prefixes():
    assert is_flow_event_type('flow_iteration') is True
    assert is_flow_event_type('endpoint_iteration') is True
    assert is_flow_event_type('round_start') is False


def test_flow_turn_kind_accepts_current_and_persisted_kinds():
    assert is_flow_turn_kind('flow_node') is True
    assert is_flow_turn_kind('autopilot_virtual_user') is True
    assert is_flow_turn_kind('endpoint_planner') is True
    assert is_flow_turn_kind('assistant') is False


def test_legacy_markers_are_consumed_into_canonical_flow_fields():
    normalized = normalize_flow_message({
        'role': 'user',
        '_isEndpointReview': True,
        '_epIteration': 4,
        '_epApproved': False,
        '_epNextPhase': 'planner',
    })

    assert normalized == {
        'role': 'user',
        '_isFlowReview': True,
        '_flowIteration': 4,
        '_flowApproved': False,
        '_flowNextPhase': 'planner',
    }


def test_canonical_value_wins_in_a_partially_migrated_row():
    normalized = normalize_flow_message({
        '_isEndpointPlanner': True,
        '_isFlowPlanner': False,
        '_epPlannerIteration': 1,
        '_flowPlannerIteration': 2,
    })

    assert normalized == {
        '_isFlowPlanner': False,
        '_flowPlannerIteration': 2,
    }


def test_historical_legacy_flow_block_still_collapses_for_model_context():
    transformed = _transform_messages([
        {'role': 'user', 'content': 'first question'},
        {'role': 'assistant', 'content': 'plan', '_isEndpointPlanner': True},
        {'role': 'assistant', 'content': 'answer', '_epIteration': 1},
        {'role': 'user', 'content': 'approved', '_isEndpointReview': True,
         '_epIteration': 1, '_epApproved': True},
        {'role': 'user', 'content': 'follow-up'},
    ], {})

    assert transformed == [
        {'role': 'user', 'content': 'first question'},
        {'role': 'assistant', 'content': 'answer'},
        {'role': 'user', 'content': 'follow-up'},
    ]
