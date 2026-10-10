"""No harness success from transport status alone; preserve partial output."""
from __future__ import annotations

import json

import pytest

from lib.agent_core.run_contract import project_agent_result
from tofu_agent.models import AgentResult

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('finish,expected', [
    ('stop', True), ('done', True), ('completed', True), ('end_turn', True),
    ('', False), ('abnormal_stop', False), ('empty_stop', False),
    ('premature_close', False), ('length', False), ('max_tokens', False),
    ('content_filter', False), ('incomplete', False), ('aborted', False),
    ('tool_calls', False), ('tool_use', False), ('function_call', False),
    ('unknown', False),
])
def test_sdk_and_cli_use_same_run_verdict(finish, expected, monkeypatch, tmp_path, capsys):
    from tofu_agent.cli import main
    from tests.tofu_agent.test_tofu_agent_run_cli import _FakeRuntime

    result = AgentResult.from_payload({
        'status': 'done', 'finish_reason': finish,
        'content': 'preserved partial work', 'usage': {'prompt_tokens': 31},
    })
    monkeypatch.setattr('tofu_agent.cli._runtime', lambda args: _FakeRuntime(result))
    rc = main(['--env-file', str(tmp_path / 'absent.env'), 'run', '--task', 'x'])
    assert result.ok is expected
    assert (rc == 0) is expected
    doc = json.loads(capsys.readouterr().out)
    assert doc['ok'] is expected
    assert doc['content'] == 'preserved partial work'
    assert doc['usage']['prompt_tokens'] == 31
    assert doc['finish_reason'] == finish


@pytest.mark.parametrize('overrides', [
    {'error': {'kind': 'abnormal_stop', 'retryable': True}},
    {'error': 'malformed error envelope'},
    {'stream_state': 'empty_response'},
    {'stream_state': 'malformed_stream'},
    {'stream_state': 'unknown'},
    {'stream_state': 'provider_future_state'},
    {'status': 'running'},
    {'status': 'error'},
    {'finish_reason': 'abnormal_stop', 'finishReason': 'stop'},
    {'finish_reason': '', 'finishReason': 'stop'},
])
def test_positive_finish_cannot_override_negative_evidence(overrides):
    result = AgentResult.from_payload({'status': 'done', 'finish_reason': 'stop', **overrides})
    assert not result.ok


def test_missing_finish_is_never_defaulted_to_stop():
    result = AgentResult.from_payload({'status': 'done'})
    assert result.finish_reason == ''
    assert not result.ok


def test_completed_tool_history_is_not_pending_tool_work():
    assert AgentResult.from_payload({
        'status': 'done', 'finish_reason': 'stop', 'stream_state': 'provider_finished',
        'tool_calls': [{'id': 'historical-call'}],
    }).ok


def test_real_abnormal_shape_emits_nonzero_and_preserves_error(monkeypatch, tmp_path, capsys):
    from tofu_agent.cli import main
    from tests.tofu_agent.test_tofu_agent_run_cli import _FakeRuntime

    result = AgentResult.from_payload({
        'status': 'done', 'finish_reason': 'abnormal_stop',
        'error': {'kind': 'abnormal_stop', 'retryable': True,
                  'detail': 'recovery exhausted', 'raw': 'bucket=empty_stop'},
    })
    monkeypatch.setattr('tofu_agent.cli._runtime', lambda args: _FakeRuntime(result))
    assert main(['--env-file', str(tmp_path / 'absent.env'), 'run', '--task', 'x']) == 6
    captured = capsys.readouterr()
    assert not json.loads(captured.out)['ok']
    assert 'bucket=empty_stop' in captured.err
    assert 'finish_reason=abnormal_stop' in captured.err


def test_parser_receipts_bounded_and_whitelisted_in_export():
    task = {
        'id': 'task', 'status': 'done', 'finishReason': 'abnormal_stop',
        'streamState': 'empty_response',
        'apiRounds': [{'round': n, 'usage': {
            'trace_id': f'trace-{n}', '_stream_terminal': {
                'state': 'empty_response', 'finish_seen': True, 'done_seen': True,
                'content_chars': 0, 'transport_bytes': 90, 'malformed_frames': 0,
                'api_key': 'private-sentinel', 'request_body': 'private-prompt',
            },
        }} for n in range(40)],
    }
    projected = project_agent_result(task, model='model')
    rows = projected['stream_diagnostics']
    assert len(rows) == 16
    assert rows[0]['round'] == 24
    assert rows[-1]['trace_id'] == 'trace-39'
    assert rows[-1]['finish_seen'] is True
    assert projected['stream_state'] == 'empty_response'
    assert 'private-' not in json.dumps(projected)
    assert not AgentResult.from_payload(projected).ok
    rows[0]['content_chars'] = 999
    assert task['apiRounds'][24]['usage']['_stream_terminal']['content_chars'] == 0
