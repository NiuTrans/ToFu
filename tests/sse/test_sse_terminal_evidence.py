"""Parser evidence distinguishes empty upstream output from lost data."""
import json
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm._sse_framer import SSEEvent
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.stream_result import ProviderStreamState
from tests.sse.test_sse_core_parity import _run_async, _run_sync

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('run', [_run_sync, _run_async])
@pytest.mark.parametrize('delta', [{'content': ' \n'}, {'reasoning': 'thinking only'}])
def test_blank_and_reasoning_only_stops_have_positive_terminal_evidence(run, delta):
    result = run([
        'data: ' + json.dumps({'choices': [{'delta': delta}]}),
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ])
    assert result.state is ProviderStreamState.EMPTY_RESPONSE
    evidence = result.usage['_stream_terminal']
    assert evidence['finish_seen'] is True
    assert evidence['done_seen'] is True
    assert evidence['malformed_frames'] == 0
    assert evidence['sse_events'] == 3
    if 'reasoning' in delta:
        assert result.message['reasoning_content'] == 'thinking only'
        assert evidence['reasoning_chars'] == len('thinking only')


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_snapshot_in_sse_cannot_be_silently_lost_as_empty_stop(run):
    result = run([
        'data: {"choices":[{"message":{"content":"not a delta"},"finish_reason":"stop"}]}',
        'data: [DONE]',
    ])
    assert result.state is ProviderStreamState.MALFORMED_STREAM
    assert result.usage['_stream_terminal']['malformed_frames'] == 1


@pytest.mark.parametrize('run', [_run_sync, _run_async])
def test_done_without_provider_finish_is_not_clean_empty_response(run):
    result = run(['data: [DONE]'])
    assert result.state is ProviderStreamState.PREMATURE_CLOSE
    assert result.usage['_stream_terminal']['finish_seen'] is False
    assert result.usage['_stream_terminal']['done_seen'] is True


def test_opt_in_raw_capture_retains_original_payload_not_just_length(tmp_path, monkeypatch):
    from lib.llm import diagnostics
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '1')
    dumper = RawSSEDumper('model', 'trace', {})
    dumper.start()
    acc = SSEAccumulator({}, 'trace', dumper, None, time.monotonic())
    acc.feed_event(SSEEvent(data=json.dumps({
        'choices': [{'delta': {'content': 'visible text'}, 'finish_reason': 'stop'}],
        'api_key': 'sk-' + 'secret' * 10,
    })))
    acc.feed_event(SSEEvent(data='[DONE]'))
    acc.finalize()
    text = (tmp_path / 'raw_sse.log').read_text()
    assert 'visible text' in text
    assert 'finish_reason' in text
    assert '[DONE]' in text
    assert 'secret' * 10 in text


def test_default_diagnostics_do_not_capture_provider_body(tmp_path, monkeypatch):
    from lib.llm import diagnostics
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '')
    dumper = RawSSEDumper('model', 'trace', {})
    acc = SSEAccumulator({}, 'trace', dumper, None, time.monotonic())
    acc.feed_event(SSEEvent(data=json.dumps({
        'choices': [{'delta': {'reasoning_content': 'private reasoning'}, 'finish_reason': 'stop'}],
    })))
    acc.feed_event(SSEEvent(data='[DONE]'))
    acc.finalize()
    assert not (tmp_path / 'raw_sse.log').exists()
    assert 'private reasoning' not in (tmp_path / 'raw_sse_anomaly.log').read_text()


def test_ring_accounting_tracks_retained_lines_after_maxlen_eviction():
    dumper = RawSSEDumper('model', 'trace', {})
    for index in range(2000):
        dumper.line(str(index))
    assert len(dumper._ring) == 400
    assert dumper._ring_bytes == sum(len(line.encode()) for line in dumper._ring)


@pytest.mark.parametrize('payloads', [
    [{'choices': [{'delta': {'tool_calls': [{'function': {
        'arguments': '{"password":"CANARY-CREDENTIAL"}',
    }}]}}]}],
    [{'choices': [{'delta': {'tool_calls': [{'function': {
        'arguments': fragment,
    }}]}}]} for fragment in ('{"pass', 'word":"', 'CANARY-', 'CREDENTIAL"}')],
    [{'type': 'response.function_call_arguments.delta', 'delta': 'CANARY-CREDENTIAL'}],
    [{'type': 'content_block_delta', 'delta': {
        'type': 'input_json_delta', 'partial_json': 'CANARY-CREDENTIAL'}}],
    [{'type': 'content_block_start', 'content_block': {
        'type': 'tool_use', 'input': {'password': 'CANARY-CREDENTIAL'}}}],
    [{'metadata': json.dumps({'nested': json.dumps({'password': 'CANARY-CREDENTIAL'})})}],
])
def test_raw_capture_preserves_tool_values_and_encoded_objects(tmp_path, monkeypatch, payloads):
    from lib.llm import diagnostics
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '1')
    dumper = RawSSEDumper('model', 'trace', {})
    dumper.start()
    for payload in payloads:
        dumper.line('data: ' + json.dumps(payload))
    dumper.dump_anomaly('test')
    dumper.finish()
    for path in tmp_path.glob('raw_sse*.log'):
        text = path.read_text()
        assert 'CANARY-' in text
        assert 'CREDENTIAL' in text


@pytest.mark.parametrize('enabled', ['', '1'])
def test_malformed_json_payload_is_metadata_only_even_in_debug(tmp_path, monkeypatch, enabled):
    from lib.llm import diagnostics
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', enabled)
    dumper = RawSSEDumper('model', 'trace', {})
    dumper.start()
    dumper.line('data: {"CANARY-UNCLASSIFIED')
    dumper.dump_anomaly('invalid_json')
    dumper.finish()
    for path in tmp_path.glob('raw_sse*.log'):
        assert 'CANARY-UNCLASSIFIED' not in path.read_text()


def test_structured_raw_capture_has_depth_node_and_payload_limits():
    from lib.llm.diagnostics import _safe_sse_line
    payload = {'leaf': 'CANARY-DEEP'}
    for _ in range(15):
        payload = {'nested': payload}
    deep = _safe_sse_line('data: ' + json.dumps(payload), capture_payload=True)
    assert 'CANARY-DEEP' not in deep and 'diagnostic limit' in deep
    many = _safe_sse_line('data: ' + json.dumps([{'n': n} for n in range(2000)]),
                         capture_payload=True)
    assert len(many) < 64 * 1024 and 'diagnostic limit' in many
    huge = _safe_sse_line('data: ' + json.dumps({'text': 'CANARY-HUGE' * 10_000}),
                         capture_payload=True)
    assert 'CANARY-HUGE' not in huge and 'omitted payload chars=' in huge


def test_single_long_debug_request_rotates_without_reopening(tmp_path, monkeypatch):
    from lib.llm import diagnostics
    monkeypatch.setattr(diagnostics, 'LOG_DIR', str(tmp_path))
    monkeypatch.setattr(diagnostics, '_RAW_SSE_FILTER', '1')
    monkeypatch.setattr(diagnostics, 'stream_max_bytes', lambda _name: 8192)
    dumper = RawSSEDumper('model', 'trace', {})
    dumper.start()
    try:
        for _ in range(100):
            dumper.line('x' * 2048)
        assert (tmp_path / 'raw_sse.log').stat().st_size <= 8192
        assert (tmp_path / 'raw_sse.log.1').stat().st_size <= 8192
        assert len(list(tmp_path.glob('raw_sse.log*'))) == 2
    finally:
        dumper.finish()
