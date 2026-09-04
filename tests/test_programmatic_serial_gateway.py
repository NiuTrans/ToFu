"""Serial-read gateway escalation contracts.

The candidate policy must create exactly one one-request gateway trial after
three successful reviewed reads without changing execution authority. The
complete direct surface returns immediately after the trial, and genuine user
steering is a hard reset boundary.
"""

from __future__ import annotations

import json
import threading

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


def test_gateway_trial_exposes_only_execute_tools_and_keeps_input_immutable():
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
        'execute_tools',
    ]
    assert _wire_names(gateway_only) == ['execute_tools']
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


def _append_read_round(task: dict, messages: list, round_num: int, *,
                       status: str = 'done') -> None:
    call_id = f'read-{round_num}'
    args = json.dumps({
        'path': f'lib/module_{round_num}.py',
        'start_line': 1,
        'end_line': 20,
    })
    content = (
        f'File: lib/module_{round_num}.py (lines 1-20 of 80)\n'
        f'──\nsource evidence {round_num}'
    )
    messages.extend([
        {
            'role': 'assistant', 'content': '',
            'tool_calls': [{
                'id': call_id, 'type': 'function',
                'function': {'name': 'read_files', 'arguments': args},
            }],
        },
        {'role': 'tool', 'tool_call_id': call_id, 'content': content},
    ])
    task['toolRounds'].append({
        'toolName': 'read_files', 'toolArgs': args,
        'toolContent': content, 'status': status,
        'llmRound': round_num, 'roundNum': round_num,
    })


def _breaker_task() -> dict:
    return {
        'id': 'serial-gateway-task', 'convId': 'serial-gateway-conv',
        'toolRounds': [], 'events': [], 'events_lock': threading.Lock(),
        '_programmaticExposurePolicy': 'serial_gateway',
        '_ptc_local': {'tier': 'program', 'eligible': ['read_files']},
        '_toolOrchestrationDecisions': [{
            'programmaticBackend': 'local',
        }],
        '_tool_schema': [_tool('read_files'), _tool('write_file')],
    }


def test_breaker_activates_only_after_three_successful_receipts():
    from lib.tasks_pkg.orchestrator._round_state import RoundState
    from lib.tasks_pkg.orchestrator._tool_loop_breaker import (
        handle_tool_loop_circuit_breaker,
    )

    task = _breaker_task()
    messages = [{'role': 'user', 'content': 'inspect all relevant modules'}]
    state = RoundState(
        model='kimi-k3', preset='default', thinking_enabled=False)
    for round_num in range(3):
        _append_read_round(task, messages, round_num)
        assert handle_tool_loop_circuit_breaker(
            task, state, messages=messages,
            round_num=round_num, tid='serialgw') is False
    assert task['_programmaticSerialGateway']['activatedAfterRound'] == 3
    assert task['_programmaticSerialGateway']['targetRound'] == 4

    failed = _breaker_task()
    failed_messages = [
        {'role': 'user', 'content': 'inspect all relevant modules'},
    ]
    failed_state = RoundState(
        model='kimi-k3', preset='default', thinking_enabled=False)
    for round_num in range(3):
        _append_read_round(
            failed, failed_messages, round_num,
            status='error' if round_num == 1 else 'done')
        assert handle_tool_loop_circuit_breaker(
            failed, failed_state, messages=failed_messages,
            round_num=round_num, tid='serialgw') is False
    assert '_programmaticSerialGateway' not in failed
    assert '_programmaticAdoptionNudges' not in failed


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
    assert _wire_names(escalated.body['tools']) == ['execute_tools']
    assert tool_schema_fingerprint(escalated.body['tools']) == (
        tool_schema_fingerprint(repeated.body['tools']))
    assert additive_sink['programmaticHiddenDirectToolCount'] == 0
    assert escalated_sink['programmaticHiddenDirectToolCount'] == 3
    assert repeated_sink['programmaticHiddenDirectToolCount'] == 3
    assert additive_sink['programmaticExposure'] == 'additive'
    assert escalated_sink['programmaticExposure'] == 'gateway_only'
    assert not [key for key in escalated.body if key.startswith('_')]
