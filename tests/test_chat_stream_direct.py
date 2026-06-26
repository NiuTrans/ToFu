#!/usr/bin/env python3
"""Tests for the native-async on-loop streaming core (routes/api_v1/chat_direct.py).

``run_direct_stream`` drives ``async_dispatch_stream`` directly on the event
loop and bridges its on-loop ``on_content``/``on_thinking`` callbacks into an
asyncio.Queue that an async generator drains into OpenAI ``chat.completion.chunk``
SSE frames. These tests inject a stub ``dispatch_fn`` (no LLM/network) that
fires the callbacks then returns ``(msg, finish_reason, usage)`` — exactly the
``async_dispatch_stream`` contract — and assert the emitted SSE frame sequence.

Per the async-test convention: drain the async generator with
``[f async for f in gen]`` inside ``run_until_complete``.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


def _run(coro):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _drain(gen):
    return [f async for f in gen]


def _frames_to_objs(frames):
    """Parse the data: JSON frames (skip the [DONE] sentinel + heartbeats)."""
    objs = []
    for f in frames:
        if not f.startswith('data: '):
            continue
        payload = f[len('data: '):].strip()
        if payload == '[DONE]':
            objs.append('[DONE]')
            continue
        objs.append(json.loads(payload))
    return objs


def _make_core(dispatch_fn):
    from routes.api_v1.chat_direct import run_direct_stream
    return run_direct_stream(
        [{'role': 'user', 'content': 'hi'}],
        model='test-model', cfg={'maxTokens': 100, 'temperature': 0},
        completion_id='chatcmpl-test', dispatch_fn=dispatch_fn)


def test_content_deltas_stream_in_order_then_done():
    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        on_content('Hello ')
        on_content('world')
        return ({'content': 'Hello world', 'tool_calls': []}, 'stop',
                {'completion_tokens': 2, 'prompt_tokens': 5})

    frames = _run(_drain(_make_core(_dispatch)))
    objs = _frames_to_objs(frames)

    # First non-[DONE] frame carries the assistant role.
    assert objs[0]['choices'][0]['delta'].get('role') == 'assistant'
    # Content deltas in order.
    contents = [o['choices'][0]['delta'].get('content')
                for o in objs if isinstance(o, dict)
                and o['choices'][0]['delta'].get('content')]
    assert contents == ['Hello ', 'world']
    # Terminal frame: finish_reason + usage; then [DONE].
    assert objs[-1] == '[DONE]'
    final = objs[-2]
    assert final['choices'][0]['finish_reason'] == 'stop'
    assert final['usage']['completion_tokens'] == 2


def test_thinking_deltas_surface_as_reasoning_content():
    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        on_thinking('let me think')
        on_content('answer')
        return ({'content': 'answer'}, 'stop', {})

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    thinks = [o['choices'][0]['delta'].get('reasoning_content')
              for o in objs if isinstance(o, dict)
              and o['choices'][0]['delta'].get('reasoning_content')]
    assert thinks == ['let me think']


def test_role_emitted_exactly_once():
    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        on_content('a')
        on_content('b')
        on_content('c')
        return ({'content': 'abc'}, 'stop', {})

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    roles = [o for o in objs if isinstance(o, dict)
             and o['choices'][0]['delta'].get('role') == 'assistant']
    assert len(roles) == 1


def test_dispatch_error_emits_envelope_and_done():
    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        raise RuntimeError('all slots exhausted')

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    # Even on immediate error: a role frame, then a terminal frame with the
    # tofu_error envelope, then [DONE]. No crash.
    assert objs[-1] == '[DONE]'
    final = objs[-2]
    assert 'tofu_error' in final
    assert final['choices'][0]['finish_reason'] == 'stop'


def test_zero_delta_still_well_formed():
    """A dispatch that yields no deltas (e.g. empty completion) still emits a
    well-formed role + terminal + [DONE] so generic clients don't hang."""
    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        return ({'content': ''}, 'stop', {'completion_tokens': 0})

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    assert objs[0]['choices'][0]['delta'].get('role') == 'assistant'
    assert objs[-1] == '[DONE]'
    assert objs[-2]['choices'][0]['finish_reason'] == 'stop'


def test_finish_reason_passthrough_length():
    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        on_content('x')
        return ({'content': 'x'}, 'length', {})

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    assert objs[-2]['choices'][0]['finish_reason'] == 'length'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
