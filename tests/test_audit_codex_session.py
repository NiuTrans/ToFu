from __future__ import annotations

import json

import pytest

from audit_codex_session import audit_session, main


pytestmark = pytest.mark.unit


def _write_session(path):
    rows = [
        {'timestamp': '2026-08-11T00:00:00.000Z', 'type': 'session_meta',
         'payload': {}},
        {'timestamp': '2026-08-11T00:00:01.000Z', 'type': 'response_item',
         'payload': {'type': 'custom_tool_call', 'call_id': 'c1',
                     'name': 'exec',
                     'input': 'await tools.exec_command({}); '
                              'await tools.apply_patch("")'}},
        {'timestamp': '2026-08-11T00:00:12.000Z', 'type': 'response_item',
         'payload': {'type': 'custom_tool_call_output', 'call_id': 'c1',
                     'output': 'Script running with cell ID 7'}},
        {'timestamp': '2026-08-11T00:00:13.000Z', 'type': 'response_item',
         'payload': {'type': 'function_call', 'call_id': 'c2', 'name': 'wait'}},
        {'timestamp': '2026-08-11T00:00:20.000Z', 'type': 'response_item',
         'payload': {'type': 'function_call_output', 'call_id': 'c2',
                     'output': 'completed'}},
        {'timestamp': '2026-08-11T00:00:21.000Z', 'type': 'event_msg',
         'payload': {'type': 'token_count', 'info': {
             'last_token_usage': {'input_tokens': 1000, 'total_tokens': 1100},
             'total_token_usage': {
                 'input_tokens': 1000, 'cached_input_tokens': 800,
                 'cache_write_input_tokens': 0, 'output_tokens': 100,
                 'reasoning_output_tokens': 20, 'total_tokens': 1100,
             }}}},
        {'timestamp': '2026-08-11T00:00:22.000Z', 'type': 'compacted',
         'payload': {}},
        {'timestamp': '2026-08-11T00:00:23.000Z', 'type': 'event_msg',
         'payload': {'type': 'task_complete', 'duration_ms': 22000}},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n',
                    encoding='utf-8')


def test_audit_session_counts_turns_cache_and_wait_amplification(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    _write_session(path)

    result = audit_session(path)

    assert result['model_turns'] == 1
    assert result['events'] == 8
    assert result['events_by_type']['response_item'] == 4
    assert result['response_items_by_type']['function_call'] == 1
    assert result['tool_calls'] == 2
    assert result['protocol_events_per_tool_call'] == 4
    assert result['nested_tool_calls_by_name'] == {
        'exec_command': 1, 'apply_patch': 1}
    assert result['yielded_execs'] == 1
    assert result['wait_calls'] == 1
    assert result['avoidable_wait_rounds'] == 1
    assert result['compactions'] == 1
    assert result['cache_hit_ratio'] == pytest.approx(0.8)
    assert result['tokens']['uncached_input_tokens'] == 200
    assert result['task_duration_s'] == 22


def test_cli_supports_shadow_cost_and_budget_failure(tmp_path, capsys):
    path = tmp_path / 'rollout.jsonl'
    _write_session(path)

    code = main([
        str(path), '--json', '--max-model-turns', '0',
        '--uncached-input-per-million', '5',
        '--cached-input-per-million', '0.5',
        '--output-per-million', '30',
    ])
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload['shadow_cost'] == pytest.approx(0.0044)


def test_session_id_in_completed_output_is_not_counted_as_yield(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    rows = [
        {'timestamp': '2026-08-11T00:00:00.000Z', 'type': 'response_item',
         'payload': {'type': 'custom_tool_call', 'call_id': 'c1',
                     'name': 'exec', 'input': ''}},
        {'timestamp': '2026-08-11T00:00:01.000Z', 'type': 'response_item',
         'payload': {'type': 'custom_tool_call_output', 'call_id': 'c1',
                     'output': {'session_id': 42, 'exit_code': 0}}},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n',
                    encoding='utf-8')

    assert audit_session(path)['yielded_execs'] == 0
