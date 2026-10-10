"""Serial-read gateway escalation contracts.

The candidate policy must create exactly one one-request gateway trial after
three successful reviewed reads without changing execution authority. The
complete direct surface returns immediately after the trial, and genuine user
steering is a hard reset boundary.

NOTE (2026-09): the breaker-side integration (handle_tool_loop_circuit_breaker
→ activate_serial_gateway after three receipts) was NEVER merged — no caller
of activate_serial_gateway exists in any mainline commit (verified vs the
75e1df1ea checkpoint sideline). The case asserting it was removed; the latch
and exposure contracts below drive lib/tasks_pkg/programmatic_escalation.py
directly.
"""

from __future__ import annotations

import json

import pytest


pytestmark = pytest.mark.unit


def _tool(name: str) -> dict:
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': name,
            'parameters': {'type': 'object', 'properties': {}},
        },
    }


def _wire_names(tools) -> list[str]:
    return [
        str((tool.get('function') or {}).get('name') or '')
        for tool in (tools or ())
    ]


def test_context_flag_defaults_to_additive_with_serial_gateway_opt_in():
    from lib.context_experiment_flags import (
        context_experiment_arm,
        normalize_context_experiment_flags,
    )

    assert normalize_context_experiment_flags({})['tools'][
        'programmaticExposure'] == 'additive'
    assert normalize_context_experiment_flags({
        'tools': {'programmaticExposure': 'serial_gateway'},
    })['tools']['programmaticExposure'] == 'serial_gateway'
    assert context_experiment_arm({})[
        'programmaticExposure'] == 'additive'
    with pytest.raises(ValueError, match='programmaticExposure'):
        normalize_context_experiment_flags({
            'tools': {'programmaticExposure': 'force_everything'},
        }, strict=True)


def test_gateway_trial_exposes_only_execute_program_and_keeps_input_immutable():
    from lib.tools.gateway import ptc_local_wire_tools

    catalog = [
        _tool('read_files'), _tool('grep_search'), _tool('write_file'),
        _tool('ask_human'),
    ]
    original = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
    additive = ptc_local_wire_tools(
        catalog, tier='program',
        eligible=['read_files', 'grep_search'], exposure='additive')
    gateway_only = ptc_local_wire_tools(
        catalog, tier='program',
        eligible=['read_files', 'grep_search'], exposure='gateway_only')

    assert _wire_names(additive) == [
        'read_files', 'grep_search', 'write_file', 'ask_human',
        'execute_program',
    ]
    assert _wire_names(gateway_only) == ['execute_program']
    assert json.dumps(catalog, ensure_ascii=False, sort_keys=True) == original


def test_task_latch_is_one_shot_and_genuine_user_steering_resets_it():
    from lib.tasks_pkg.programmatic_escalation import (
        activate_serial_gateway,
        resolve_programmatic_exposure,
    )

    messages = [{'role': 'user', 'content': 'inspect the repository'}]
    task = {'_programmaticExposurePolicy': 'serial_gateway'}
    assert activate_serial_gateway(
        task, messages, round_num=2,
        chain=['find_files', 'grep_search', 'read_files']) is True
    assert activate_serial_gateway(
        task, messages, round_num=2,
        chain=['find_files', 'grep_search', 'read_files']) is False

    exposure, reason = resolve_programmatic_exposure(
        task, messages, round_num=3, requested_policy='serial_gateway',
        programmatic_active=True)
    assert (exposure, reason) == ('gateway_only', 'serial_chain_one_shot')

    messages.append({
        'role': 'user', 'content': 'bounded internal context', '_isMeta': True,
    })
    assert resolve_programmatic_exposure(
        task, messages, round_num=4, requested_policy='serial_gateway',
        programmatic_active=True) == ('additive', 'gateway_trial_consumed')

    assert activate_serial_gateway(
        task, messages, round_num=4,
        chain=['find_files', 'grep_search', 'read_files']) is True

    messages.append({
        'role': 'user', 'content': 'stop reading and make the edit now',
        '_isInboxInject': True, '_containsHumanSteer': True,
    })
    assert resolve_programmatic_exposure(
        task, messages, round_num=5, requested_policy='serial_gateway',
        programmatic_active=True) == ('additive', 'genuine_user_steering')
    assert task['_programmaticSerialGatewayEvents'][-1] == {
        'kind': 'reset', 'reason': 'genuine_user_steering', 'beforeRound': 6,
    }


def test_wire_epoch_is_stable_and_private_metadata_never_leaks():
    from lib.llm._sse_core import prepare_request
    from lib.tools.gateway import tool_schema_fingerprint

    tools = [_tool('read_files'), _tool('grep_search'), _tool('write_file')]

    def prepare(exposure: str):
        sink = {}
        plan = prepare_request({
            'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'inspect and fix'}],
            'tools': tools,
            '_programmatic_tool_calling': 'on',
            '_programmatic_tier': 'program',
            '_programmatic_eligible_tools': ['read_files', 'grep_search'],
            '_programmatic_exposure': exposure,
            '_tool_orchestration_decision_sink': sink,
        }, api_key='secret', base_url='https://example.test/v1',
           api_protocol='openai')
        return plan, sink

    additive, additive_sink = prepare('additive')
    escalated, escalated_sink = prepare('gateway_only')
    repeated, repeated_sink = prepare('gateway_only')

    assert {'read_files', 'grep_search'} <= set(_wire_names(additive.body['tools']))
    assert not ({'read_files', 'grep_search'}
                & set(_wire_names(escalated.body['tools'])))
    assert _wire_names(escalated.body['tools']) == ['execute_program']
    assert tool_schema_fingerprint(escalated.body['tools']) == (
        tool_schema_fingerprint(repeated.body['tools']))
    assert additive_sink['programmaticHiddenDirectToolCount'] == 0
    assert escalated_sink['programmaticHiddenDirectToolCount'] == 3
    assert repeated_sink['programmaticHiddenDirectToolCount'] == 3
    assert additive_sink['programmaticExposure'] == 'additive'
    assert escalated_sink['programmaticExposure'] == 'gateway_only'
    assert not [key for key in escalated.body if key.startswith('_')]
