"""External completion markers preserve verified truncation and stream evidence."""
import asyncio
import json
import threading

import pytest

from lib.compat.anthropic import build_anthropic_response, stream_anthropic_chunks
from lib.compat.openai import build_openai_response, stream_openai_chunks

pytestmark = pytest.mark.unit


def terminal_task(reason, *, event_reason=True):
    event = {'type': 'done', 'seq': 0}
    if event_reason:
        event['finishReason'] = reason
    return {'id': 'projection-fixture', 'status': 'done', 'content': 'Verified prefix',
            'finishReason': reason, 'events': [event], 'events_lock': threading.Lock()}


def streamed_terminal(task):
    async def drain():
        return [frame async for frame in stream_anthropic_chunks(task, model='fixture')]

    frames = asyncio.run(drain())
    payloads = [json.loads(line[6:]) for frame in frames for line in frame.splitlines()
                if line.startswith('data: ')]
    return next(value['delta']['stop_reason'] for value in payloads
                if value['type'] == 'message_delta')


@pytest.mark.parametrize(('reason', 'expected'), [
    ('length', 'max_tokens'), ('max_tokens', 'max_tokens'),
    ('context_length', 'model_context_window_exceeded'),
    ('budget_exceeded', 'max_tokens'), ('incomplete', 'max_tokens'),
    ('max_turns', 'pause_turn'), ('content_filter', 'refusal'),
])
def test_anthropic_truncation_is_not_projected_as_natural_completion(reason, expected):
    task = terminal_task(reason)
    assert build_anthropic_response(task, model='fixture')['stop_reason'] == expected
    assert streamed_terminal(task) == expected


@pytest.mark.parametrize('reason', ['max_tokens', 'tool_use', 'content_filter'])
def test_anthropic_done_event_without_reason_uses_verified_task_evidence(reason):
    task = terminal_task(reason, event_reason=False)
    assert streamed_terminal(task) == build_anthropic_response(task, model='fixture')['stop_reason']


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('reason', ['stop', 'max_tokens', 'tool_use'])
def test_late_terminal_connection_preserves_finish_usage_and_tool_handoff(protocol, reason):
    task = terminal_task(reason)
    task['events'] = []
    task['usage'] = {'input_tokens': 5, 'output_tokens': 8, 'total_tokens': 13}
    call = {'id': 'call-fixture', 'type': 'function',
            'function': {'name': 'fixture_tool', 'arguments': '{"number":7}'}}
    if reason == 'tool_use':
        task['toolRounds'] = [{'tool_calls': [call]}]
    build, stream = ((build_openai_response, stream_openai_chunks) if protocol == 'openai'
                     else (build_anthropic_response, stream_anthropic_chunks))
    expected = build(task, model='fixture')

    async def drain():
        return [frame async for frame in stream(task, model='fixture')]

    frames = asyncio.run(drain())
    payloads = [json.loads(line[6:]) for frame in frames for line in frame.splitlines()
                if line.startswith('data: ') and line != 'data: [DONE]']
    if protocol == 'openai':
        terminal = [p for p in payloads if p['choices'][0]['finish_reason'] is not None]
        assert len(terminal) == 1
        assert terminal[0]['choices'][0]['finish_reason'] == expected['choices'][0]['finish_reason']
        assert terminal[0]['usage'] == task['usage']
        assert sum(frame == 'data: [DONE]\n\n' for frame in frames) == 1
        if reason == 'tool_use':
            calls = [c for p in payloads for c in p['choices'][0]['delta'].get('tool_calls', [])]
            assert calls == [dict(call, index=0)]
    else:
        terminal = [p for p in payloads if p['type'] == 'message_delta']
        assert len(terminal) == 1
        assert terminal[0]['delta']['stop_reason'] == expected['stop_reason']
        assert terminal[0]['usage'] == {'input_tokens': 5, 'output_tokens': 8}
        assert sum(p['type'] == 'message_stop' for p in payloads) == 1
        if reason == 'tool_use':
            tools = [p['content_block'] for p in payloads if p['type'] == 'content_block_start'
                     and p['content_block']['type'] == 'tool_use']
            assert [(item['id'], item['name']) for item in tools] == [('call-fixture', 'fixture_tool')]
            assert any(p.get('delta', {}).get('partial_json') == '{"number": 7}' for p in payloads)


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_late_failure_keeps_error_channel_and_releases_waiter(protocol, monkeypatch):
    from lib.agent_core import admission

    released = []
    monkeypatch.setattr(admission, 'unregister_waiter', released.append)
    task = terminal_task('premature_close')
    task.update(events=[], streamState='premature_close')
    stream = stream_openai_chunks if protocol == 'openai' else stream_anthropic_chunks

    async def drain():
        return ''.join([frame async for frame in stream(task, model='fixture')])

    wire = asyncio.run(drain())
    assert '"error"' in wire
    assert '"finish_reason"' not in wire
    assert 'event: message_stop' not in wire
    assert released == [task['id']]


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
def test_disconnect_during_terminal_projection_releases_waiter(protocol, monkeypatch):
    from lib.agent_core import admission

    released = []
    monkeypatch.setattr(admission, 'unregister_waiter', released.append)
    task = terminal_task('stop')
    task['events'] = []
    stream = stream_openai_chunks if protocol == 'openai' else stream_anthropic_chunks

    async def disconnect():
        response = stream(task, model='fixture')
        assert await anext(response)
        await response.aclose()

    asyncio.run(disconnect())
    assert released == [task['id']]
