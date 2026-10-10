"""Live transport traffic cannot hide waits or erase failed-attempt evidence."""
import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
import requests

from lib.llm._transport import StreamIdleWatchdog

pytestmark = pytest.mark.unit


def test_metadata_keeps_transport_alive_and_still_publishes_wait_status():
    published = threading.Event()
    stopped = threading.Event()
    statuses = []

    def observe(status):
        statuses.append(status)
        published.set()

    watcher = StreamIdleWatchdog(
        heartbeat_interval=.03, idle_timeout=.15, on_progress=observe)
    watcher.notify_response_headers()

    def traffic():
        while not stopped.wait(.005):
            watcher.notify_transport_bytes(100)

    producer = threading.Thread(target=traffic)
    producer.start()
    watcher.start()
    try:
        assert published.wait(1), 'active metadata must not suppress status updates'
        assert not watcher.idle_timed_out
        assert statuses[0].kind == 'waiting_event'
        assert statuses[0].transport_byte_count > 0
        assert statuses[0].semantic_idle_s >= .03
        assert statuses[0].transport_idle_s < .03
    finally:
        stopped.set()
        watcher.cancel()
        producer.join(1)
        watcher._thread.join(1)
    assert not producer.is_alive()
    assert not watcher._thread.is_alive()


def test_fresh_semantic_output_suppresses_wait_status():
    statuses = []
    watcher = StreamIdleWatchdog(
        heartbeat_interval=.03, idle_timeout=.15,
        on_progress=statuses.append)
    watcher.start()
    try:
        for _ in range(20):
            watcher.notify_transport_bytes(100)
            watcher.progress.mark_content('working')
            time.sleep(.005)
        assert not statuses
        assert not watcher.idle_timed_out
    finally:
        watcher.cancel()
        watcher._thread.join(1)


def test_unfinished_tool_arguments_publish_status_while_arriving():
    published = threading.Event()
    stopped = threading.Event()
    statuses = []

    def observe(status):
        statuses.append(status)
        published.set()

    watcher = StreamIdleWatchdog(
        heartbeat_interval=.03, idle_timeout=.15, on_progress=observe)
    watcher.notify_response_headers()

    def traffic():
        while not stopped.wait(.005):
            watcher.notify_transport_bytes(100)
            watcher.progress.mark_tool_delta(argument_delta='x')

    producer = threading.Thread(target=traffic)
    producer.start()
    watcher.start()
    try:
        assert published.wait(1)
        assert statuses[0].kind == 'preparing_tool_call'
        assert statuses[0].semantic_idle_s < .03
        assert not watcher.idle_timed_out
    finally:
        stopped.set()
        watcher.cancel()
        producer.join(1)
        watcher._thread.join(1)


def _frame(delta, finish=None):
    return b'data: ' + json.dumps({'choices': [{
        'delta': delta, 'finish_reason': finish,
    }]}).encode() + b'\n\n'


def test_task_localizes_unfinished_tool_generation_without_claiming_execution(
        monkeypatch):
    import lib.tasks_pkg.manager._stream as manager
    from tests.support.chat_tasks import chat_task_fixture_guard, chat_task_registry

    task = {'id': 'partial-tool-generation', 'convId': 'partial-tool-conv',
            '_userId': 1, 'status': 'running', 'content': '', 'thinking': '',
            'config': {'userId': 1}, 'events': [], 'toolRounds': [],
            'content_lock': threading.Lock(), 'events_lock': threading.Lock()}

    def dispatch(body, **kwargs):
        kwargs['on_waiting'](
            status=SimpleNamespace(kind='preparing_tool_call',
                                   request_elapsed_s=45, semantic_idle_s=0),
            slot=SimpleNamespace(model='fixture'))
        return ({'role': 'assistant', 'content': 'ok',
                 'reasoning_content': ''}, 'stop', {})

    monkeypatch.setattr(manager, 'dispatch_stream', dispatch)
    with chat_task_fixture_guard:
        chat_task_registry[task['id']] = task
    try:
        manager.stream_llm_response(
            task, {'model': 'fixture',
                   'messages': [{'role': 'user', 'content': 'go'}]}, tag='R1')
    finally:
        with chat_task_fixture_guard:
            chat_task_registry.pop(task['id'], None)
    events = [event for event in task['events']
              if event.get('type') == 'phase'
              and 'preparing a tool call' in event.get('detail', '')]
    assert len(events) == 1
    assert events[0]['phase'] == 'llm_thinking'
    assert events[0]['detailKey'] == 'stream.phase.generatingResponse'


def test_failed_partial_arguments_keep_evidence_after_successful_retry(
        monkeypatch, caplog):
    import lib.llm.stream as stream
    import lib.desktop.egress as egress
    import lib.proxy as proxy

    private_argument = '{"path":"private-argument-marker'
    failed_frame = _frame({'tool_calls': [{
        'index': 0, 'id': 'call_probe', 'type': 'function',
        'function': {'name': 'read_files', 'arguments': private_argument},
    }]})

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, failing):
            self.failing = failing
            self.closed = False

        def iter_content(self, chunk_size):
            if self.failing:
                yield failed_frame
                raise requests.exceptions.ChunkedEncodingError('synthetic break')
            yield _frame({'content': 'ok'}, 'stop')
            yield b'data: [DONE]\n\n'

        def close(self):
            self.closed = True

    responses = [Response(True), Response(False)]
    calls = []

    class Session:
        def post(self, *args, **kwargs):
            calls.append(kwargs)
            return responses[len(calls)-1]

    monkeypatch.setattr(stream, 'get_sync_session', lambda: Session())
    monkeypatch.setattr(stream, 'abortable_sleep', lambda *args: None)
    monkeypatch.setattr(egress, 'route_request', lambda *args, **kwargs: 'direct')
    monkeypatch.setattr(proxy, 'subscription_route_candidates', lambda *args: [])
    monkeypatch.setattr(stream, '_proxy_report_outcome', lambda *args, **kwargs: None)

    with caplog.at_level(logging.WARNING, logger='lib.llm.stream'):
        result = stream.stream_chat(
            {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]},
            api_key='synthetic-not-a-credential', base_url='http://fake.local/v1')

    assert result.is_verified_complete
    assert result.message['content'] == 'ok'
    assert len(calls) == 2
    assert all(response.closed for response in responses)
    failures = [record for record in caplog.records
                if getattr(record, 'tofu_event_name', '') == 'llm.stream_interrupted']
    assert len(failures) == 1
    fields = failures[0].tofu_event_fields
    assert fields['error_kind'] == 'ChunkedEncodingError'
    assert fields['trace_id']
    evidence = fields['stream_evidence']
    assert evidence['response_headers_seen']
    assert evidence['transport_byte_count'] == len(failed_frame)
    assert evidence['sse_event_count'] == 1
    assert evidence['tool_argument_chars'] == len(private_argument)
    assert evidence['tool_argument_chunks'] == 1
    assert not evidence['provider_finish_seen']
    assert not evidence['done_seen']
    assert private_argument not in caplog.text
    assert 'synthetic-not-a-credential' not in caplog.text
