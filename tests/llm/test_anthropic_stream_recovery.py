"""Native SSE byte evidence must drive paced, bounded recovery on every round."""

import json
import threading
import time

import pytest

from lib.llm._sse_core import SSEAccumulator
from lib.llm._sse_framer import SSEFramer
from lib.llm.anthropic_outbound._sse import AnthropicSSETranslator
from lib.llm.stream_result import ProviderStreamState
from lib.llm_errors import RateLimitError, RetryableAPIError
from lib.tasks_pkg.stream_handler import _budget
from lib.tasks_pkg.stream_handler.api import analyse_stream_result
from tests._registered_chat_task import registered_chat_task

pytestmark = pytest.mark.unit


class _Dumper:
    enabled = False

    def line(self, *_args, **_kwargs):
        pass

    def payload(self, *_args, **_kwargs):
        pass

    def dump_anomaly(self, *_args, **_kwargs):
        pass

    def finish(self, *_args, **_kwargs):
        pass


def _parse(events):
    accumulator = SSEAccumulator(
        {'model': 'claude-opus-5'}, 'synthetic-trace', _Dumper(),
        AnthropicSSETranslator(), time.monotonic() - 35)
    wire = ''.join(
        f'event: {event["type"]}\r\ndata: {json.dumps(event)}\r\n\r\n'
        for event in events
    ).encode('utf-8')
    framer = SSEFramer()
    # Exercise splits inside fields, JSON and CRLF through the actual parser.
    for offset in range(0, len(wire), 7):
        for event in framer.feed(wire[offset:offset + 7]):
            accumulator.feed_event(event)
    for event in framer.finalize():
        accumulator.feed_event(event)
    issues = framer.drain_issues()
    accumulator.record_malformed_frames(issues.count, issues.diagnostics)
    result = accumulator.finalize()
    return result.with_usage({**result.usage,
        '_dispatch': {'key': 'failed-credential', 'model': 'claude-opus-5'},
    })


def _recover(stream, monkeypatch, *, round_num=0, attempts=0, aborted=False):
    sleeps = []
    monkeypatch.setattr(_budget, '_interruptible_sleep', lambda delay, _task: sleeps.append(delay))
    task = {
        'id': 'native-stream-recovery', 'status': 'running', 'aborted': aborted,
        'content': '', 'thinking': '', 'error': None, 'events': [],
        'events_lock': threading.Lock(),
    }
    messages = [{'role': 'user', 'content': 'Continue the authorized work.'}]
    with registered_chat_task(task):
        decision = analyse_stream_result(
            stream.message, stream.finish_reason, task, tid=task['id'],
            model='claude-opus-5', round_num=round_num,
            _premature_retry_count=attempts, messages=messages,
            stream_result=stream,
        )
    return decision, task, sleeps, messages


@pytest.mark.parametrize('round_num', [0, 7])
@pytest.mark.parametrize('heartbeat_count', [2, 3, 12])
def test_heartbeat_only_eof_retries_with_backoff_on_every_round(monkeypatch, round_num, heartbeat_count):
    stream = _parse([{'type': 'ping'}] * heartbeat_count)
    assert stream.state is ProviderStreamState.PREMATURE_CLOSE
    assert stream.evidence.sse_event_count == heartbeat_count
    assert stream.evidence.malformed_frame_count == 0
    decision, task, sleeps, messages = _recover(stream, monkeypatch, round_num=round_num)
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 1
    assert len(sleeps) == 1 and 0.5 <= sleeps[0] <= 1.0
    assert task['_force_rotate_pair'] == ('failed-credential', 'claude-opus-5')
    assert task['error'] is None
    assert len(messages) == 1  # Retry the preserved context; do not invent a user turn.


@pytest.mark.parametrize('round_num', [0, 7])
def test_small_reasoning_prefix_eof_gets_paced_recovery(monkeypatch, round_num):
    stream = _parse([
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'thinking', 'thinking': 'Let me check.'}},
    ])
    decision, _task, sleeps, _messages = _recover(stream, monkeypatch, round_num=round_num)
    assert stream.state is ProviderStreamState.PREMATURE_CLOSE
    assert decision['action'] == 'continue'
    assert len(sleeps) == 1 and sleeps[0] > 0


def test_heartbeat_retry_cap_still_fails_honestly(monkeypatch):
    stream = _parse([{'type': 'ping'}] * 3)
    decision, task, sleeps, _messages = _recover(
        stream, monkeypatch, attempts=_budget._PREMATURE_RETRY_MAX_CLASSIC)
    assert decision['action'] == 'break'
    assert decision['last_finish_reason'] == 'abnormal_stop'
    assert task['error']
    assert sleeps == []


def test_later_heartbeat_retry_backoff_remains_bounded(monkeypatch):
    decision, _task, sleeps, _messages = _recover(
        _parse([{'type': 'ping'}] * 3), monkeypatch, attempts=10)
    assert decision['action'] == 'continue'
    assert decision['premature_retry_count'] == 11
    assert len(sleeps) == 1 and 8 <= sleeps[0] <= 8.5


def test_user_abort_does_not_retry_a_heartbeat_disconnect(monkeypatch):
    decision, _task, sleeps, _messages = _recover(
        _parse([{'type': 'ping'}] * 3), monkeypatch, aborted=True)
    assert decision['action'] == 'break'
    assert sleeps == []


def test_complete_reply_after_heartbeats_needs_no_recovery(monkeypatch):
    stream = _parse([
        {'type': 'ping'},
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'text', 'text': 'The requested audit is complete.'}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}},
        {'type': 'message_stop'},
    ])
    assert stream.state is ProviderStreamState.PROVIDER_FINISHED
    decision, task, sleeps, _messages = _recover(stream, monkeypatch)
    assert decision['last_finish_reason'] == 'stop'
    assert task['error'] is None
    assert sleeps == []


@pytest.mark.parametrize(('error_type', 'message', 'expected_exception', 'status'), [
    ('overloaded_error', 'Overloaded', RetryableAPIError, 529),
    ('api_error', 'Internal server error', RetryableAPIError, 500),
    ('rate_limit_error', 'Rate limit reached', RateLimitError, 429),
])
def test_native_error_type_drives_recovery_without_http_status(error_type, message, expected_exception, status):
    with pytest.raises(expected_exception) as caught:
        _parse([{'type': 'error', 'error': {'type': error_type, 'message': message}}])
    if isinstance(caught.value, RetryableAPIError):
        assert caught.value.status_code == status
    else:
        assert not caught.value.is_gateway


def test_explicit_gateway_error_status_is_preserved():
    with pytest.raises(RateLimitError) as caught:
        _parse([{'type': 'error', 'error': {
            'type': 'api_error', 'message': 'Gateway unavailable', 'http_code': '503',
        }}])
    assert caught.value.is_gateway


def test_non_retryable_native_error_stays_terminal():
    with pytest.raises(Exception) as caught:
        _parse([{'type': 'error', 'error': {
            'type': 'invalid_request_error', 'message': 'Unsupported parameter',
        }}])
    assert not isinstance(caught.value, (RateLimitError, RetryableAPIError))
