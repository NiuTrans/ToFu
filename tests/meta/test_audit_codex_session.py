from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

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
    assert result['nested_tool_calls_by_name'] is None
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


@pytest.mark.parametrize('source', [
    'const tools = []; tools.filter(Boolean).slice(0);',
    'const message = "tools.exec_command({})";',
    'if (false) { await tools.exec_command({}); }',
    'await tools.exec_command({});',
    'for (let i=0; i<10; i++) await tools.exec_command({});',
    'const run = tools.exec_command; await run({});',
    '',
])
def test_source_text_never_becomes_an_executed_nested_call_count(tmp_path, source):
    path = tmp_path / 'rollout.jsonl'
    path.write_text(json.dumps({'type': 'response_item', 'payload': {
        'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'outer', 'input': source}}) + '\n')
    result = audit_session(path)
    assert result['schema_version'] == 2
    assert result['tool_calls_by_name'] == {'exec': 1}
    assert result['nested_tool_calls'] is None
    assert result['nested_tool_calls_by_name'] is None
    assert result['nested_tool_calls_observation'] == 'unavailable'


def test_runtime_receipts_are_counted_without_guessing_nested_invocations(tmp_path):
    def receipt(thread, turn, item_id, kind, **details):
        return {'type': 'event_msg', 'payload': {'type': 'item_completed',
                'thread_id': thread, 'turn_id': turn,
                'item': {'id': item_id, 'type': kind, **details}}}

    rows = [
        receipt('thread-a', 'turn-1', 'item-1', 'CommandExecution', status='completed'),
        receipt('thread-a', 'turn-1', 'item-1', 'CommandExecution', status='completed'),
        receipt('thread-a', 'turn-2', 'item-1', 'CommandExecution', status='failed'),
        receipt('thread-b', 'turn-1', 'item-1', 'CommandExecution', status='completed'),
        receipt('thread-a', 'turn-1', 'item-2', 'FileChange', status='completed'),
        receipt('thread-a', 'turn-1', 'item-3', 'AgentMessage', content=[]),
        receipt('thread-a', 'turn-1', 'item-4', 'FutureRuntimeItem'),
        receipt('thread-a', 'turn-1', '', 'CommandExecution'),
        receipt('thread-a', '', 'item-5', 'CommandExecution'),
        receipt('', 'turn-1', 'item-6', 'CommandExecution'),
        receipt('thread-a', 'turn-1', [], 'CommandExecution'),
        receipt('thread-a', 'turn-1', 'item-7', ''),
        {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': None}},
        {'type': 'event_msg', 'payload': {'type': 'item_started', 'item': {
            'type': 'CommandExecution', 'id': 'in-flight'}}},
    ]
    path = tmp_path / 'rollout.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n')
    result = audit_session(path)
    assert result['runtime_item_completions'] == {
        'distinct_items': 6,
        'by_type': {'CommandExecution': 3, 'FileChange': 1, 'AgentMessage': 1, 'FutureRuntimeItem': 1},
        'duplicate_notifications': 1,
        'unidentified_notifications': 6,
    }
    assert result['tool_calls'] == 0
    assert result['nested_tool_calls'] is None
    assert result['events'] == len(rows)


def test_a_recorded_tool_named_filter_is_still_a_real_call(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    path.write_text(json.dumps({'type': 'response_item', 'payload': {
        'type': 'function_call', 'name': 'filter', 'call_id': 'real-call'}}) + '\n')
    result = audit_session(path)
    assert result['tool_calls_by_name'] == {'filter': 1}
    assert result['nested_tool_calls'] is None


def test_v2_contract_is_machine_readable_and_rejects_invented_nested_counts(tmp_path):
    from jsonschema import Draft202012Validator

    schema_path = Path(__file__).resolve().parents[2] / 'contracts/codex_session_audit_v2.schema.json'
    schema = json.loads(schema_path.read_text())
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    path = tmp_path / 'rollout.jsonl'
    _write_session(path)
    result = audit_session(path)
    validator.validate(result)
    assert not validator.is_valid({**result, 'nested_tool_calls': 0})
    assert not validator.is_valid({**result, 'nested_tool_calls_by_name': {'filter': 1}})
    assert not validator.is_valid({**result, 'schema_version': 1})


def test_plain_cli_distinguishes_unobserved_calls_from_runtime_items(tmp_path, capsys):
    path = tmp_path / 'rollout.jsonl'
    _write_session(path)
    assert main([str(path)]) == 0
    output = capsys.readouterr().out
    assert 'nested calls: unavailable' in output
    assert 'runtime item completions:' in output
    assert "'exec_command': 1" not in output


def test_usage_snapshots_and_invalid_records_keep_their_independent_meanings(tmp_path):
    rows = [
        {'timestamp': '2026-08-11T00:00:00Z', 'type': 'session_meta'},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 100, 'total_tokens': 110},
            'total_token_usage': {'input_tokens': 100, 'cached_input_tokens': 80}}}},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 200, 'total_tokens': 220},
            'total_token_usage': {'input_tokens': 300, 'cached_input_tokens': 250}}}},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'input_tokens': 'invalid', 'total_tokens': 1},
            'total_token_usage': {'input_tokens': 450, 'cached_input_tokens': 400}}}},
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': []}},
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'duration_ms': 6000}},
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'duration_ms': 'invalid'}},
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'duration_ms': 3000}},
        {'timestamp': '2026-08-11T00:00:10Z', 'type': 'compacted'},
    ]
    path = tmp_path / 'rollout.jsonl'
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n\n{broken\n[]\nnull\n')
    result = audit_session(path)
    assert result['events'] == 9
    assert result['invalid_lines'] == 1
    assert result['token_events'] == 4
    assert result['model_turns'] == 2
    assert result['tokens']['input_tokens'] == 450
    assert result['tokens']['uncached_input_tokens'] == 50
    assert result['average_input_tokens_per_turn'] == 150
    assert result['max_input_tokens_per_turn'] == 200
    assert result['task_duration_s'] == 6
    assert result['session_span_s'] == 10


def test_output_observations_keep_duplicate_receipts_unicode_and_unmatched_ids(tmp_path):
    rows = [
        (0, {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'same'}),
        (2, {'type': 'custom_tool_call_output', 'call_id': 'same', 'output': 'Script running with cell ID 7'}),
        (3, {'type': 'custom_tool_call_output', 'call_id': 'same', 'output': {'message': '你好🙂'}}),
        (4, {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'same'}),
        (8, {'type': 'custom_tool_call_output', 'call_id': 'same', 'output': 'done'}),
        (10, {'type': 'function_call_output', 'call_id': 'unmatched', 'output': 'orphan'}),
    ]
    path = tmp_path / 'rollout.jsonl'
    path.write_text('\n'.join(json.dumps({'timestamp': f'2026-08-11T00:00:{second:02d}Z',
                                         'type': 'response_item', 'payload': payload})
                              for second, payload in rows) + '\n')
    result = audit_session(path)
    assert result['tool_calls'] == 2
    assert result['yielded_execs'] == 1
    assert result['max_tool_duration_s'] == 4
    assert result['tool_output_chars'] == len('Script running with cell ID 7{"message":"你好🙂"}doneorphan')
    assert result['max_tool_output_chars'] == len('Script running with cell ID 7')
    assert result['session_span_s'] == 10


@pytest.mark.parametrize('end', [None, '', 'not a timestamp'])
def test_missing_endpoint_does_not_invent_a_session_span(tmp_path, end):
    path = tmp_path / 'rollout.jsonl'
    path.write_text(json.dumps({'timestamp': '2026-08-11T00:00:00Z', 'type': 'session_meta'}) + '\n'
                    + json.dumps({'timestamp': end, 'type': 'unknown'}) + '\n')
    result = audit_session(path)
    assert result['session_span_s'] == 0
    assert result['events_by_type'] == {'session_meta': 1, 'unknown': 1}


def test_audit_is_per_invocation_and_does_not_rewrite_recorded_token_values(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    path.write_text('\n'.join(json.dumps({'type': 'event_msg', 'payload': {
        'type': 'token_count', 'info': {'last_token_usage': {'input_tokens': value, 'total_tokens': 1},
                                       'total_token_usage': {'input_tokens': value}}}})
                              for value in (-5, -2)) + '\n')
    result = audit_session(path)
    assert result['tokens']['input_tokens'] == -2
    assert result['max_input_tokens_per_turn'] == -2
    assert result['average_input_tokens_per_turn'] == -3.5
    path.write_text('')
    empty = audit_session(path)
    assert empty['events'] == empty['model_turns'] == 0
    assert empty['max_input_tokens_per_turn'] == 0
    assert empty['runtime_item_completions']['by_type'] == {}


@pytest.mark.parametrize('flag,limit,code', [
    ('--max-model-turns', '1', 0),
    ('--max-tool-calls', '1', 1),
    ('--max-tool-calls', '2', 0),
    ('--max-wait-rounds', '0', 1),
    ('--max-wait-rounds', '1', 0),
])
def test_observed_metric_thresholds_remain_cli_contracts(tmp_path, capsys, flag, limit, code):
    path = tmp_path / 'rollout.jsonl'
    _write_session(path)
    assert main([str(path), '--json', flag, limit]) == code
    result = json.loads(capsys.readouterr().out)
    assert result['schema_version'] == 2
    assert 'shadow_cost' not in result


def test_read_failure_is_not_a_partial_success_and_closes_the_input(tmp_path, monkeypatch):
    from io import StringIO

    failure = OSError('read interrupted')

    class InterruptedRead(StringIO):
        lines = 0

        def __next__(self):
            if self.lines:
                raise failure
            self.lines += 1
            return super().__next__()

    stream = InterruptedRead('{"type":"session_meta"}\n')
    path = tmp_path / 'rollout.jsonl'
    original_open = Path.open
    monkeypatch.setattr(Path, 'open', lambda self, *args, **kwargs: (
        stream if self == path else original_open(self, *args, **kwargs)))
    with pytest.raises(OSError) as raised:
        audit_session(path)
    assert raised.value is failure
    assert stream.closed


def test_large_payload_history_is_not_retained_in_memory(tmp_path):
    path = tmp_path / 'rollout.jsonl'
    row = json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'content': 'x' * 131072}})
    with path.open('w') as handle:
        for _ in range(240):
            handle.write(row + '\n')
    probe = '''
import importlib.util, json, sys, tracemalloc
sys.path.insert(0, sys.argv[1])
from audit_codex_session import audit_session
tracemalloc.start()
result = audit_session(sys.argv[2])
peak = tracemalloc.get_traced_memory()[1]
bytecode = None if sys.dont_write_bytecode else importlib.util.cache_from_source(
    sys.modules['audit_codex_session'].__file__)
print(json.dumps({'events': result['events'], 'peak_bytes': peak, 'bytecode': bytecode}))
'''
    checkout = Path(__file__).resolve().parents[2]
    process = subprocess.run([sys.executable, '-I', '-B', '-c', probe, str(checkout), str(path)],
                             text=True, capture_output=True, timeout=30, check=True)
    measured = json.loads(process.stdout)
    # -I ignores PYTHONPYCACHEPREFIX, so the probe must not write __pycache__/
    # beside audit_codex_session.py at the checkout root.
    assert measured['bytecode'] is None or not Path(measured['bytecode']).is_relative_to(checkout)
    assert measured['events'] == 240
    # This bounds retained payload history, not host memory or admitted work.
    # The threshold is generous relative to a single record and independent of
    # machine capacity; keeping all 240 decoded payloads fails this contract.
    assert measured['peak_bytes'] < path.stat().st_size / 8
