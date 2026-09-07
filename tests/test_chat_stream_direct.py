#!/usr/bin/env python3
"""Tests for the native-async on-loop streaming core (routes/api_v1/chat_direct.py).

``run_direct_stream`` drives ``async_dispatch_stream`` directly on the event
loop and bridges its on-loop ``on_content``/``on_thinking`` callbacks into an
asyncio.Queue that an async generator drains into OpenAI ``chat.completion.chunk``
SSE frames. These tests inject a stub ``dispatch_fn`` (no LLM/network) that
fires the callbacks then returns the typed provider result (or an explicit
legacy tuple at the compatibility seam) and asserts the emitted sequence.

Per the async-test convention: drain the async generator with
``[f async for f in gen]`` inside ``run_until_complete``.
"""
import asyncio
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
    # An immediate error uses the error channel, never an empty assistant turn
    # with a fabricated finish_reason=stop.
    assert objs[-1] == '[DONE]'
    final = objs[-2]
    assert 'tofu_error' in final
    assert final['error']['code'] == 'generation_error'
    assert 'choices' not in final


def test_malformed_typed_result_emits_error_not_fake_stop():
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState

    async def _dispatch(messages, *, on_content=None, on_thinking=None, **kw):
        on_content('safe prefix')
        return ProviderStreamResult(
            message={'role': 'assistant', 'content': 'safe prefix'},
            compatibility_finish_reason='stop',
            usage={'completion_tokens': 2},
            state=ProviderStreamState.MALFORMED_STREAM,
            malformed_frame_count=1,
        )

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    error = objs[-2]
    assert error['error']['code'] == 'provider_stream_error'
    assert 'choices' not in error
    assert objs[-1] == '[DONE]'


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


def test_owner_is_forwarded_to_async_dispatch():
    captured = {}

    async def _dispatch(messages, *, on_content=None, **kwargs):
        captured.update(kwargs)
        on_content('ok')
        return ({'content': 'ok'}, 'stop', {})

    from routes.api_v1.chat_direct import run_direct_stream
    stream = run_direct_stream(
        [{'role': 'user', 'content': 'hi'}],
        model='test-model', cfg={'maxTokens': 100, 'temperature': 0},
        completion_id='chatcmpl-owner', dispatch_fn=_dispatch,
        owner_user_id=41,
    )
    _run(_drain(stream))
    assert captured['owner_user_id'] == 41
    assert callable(captured['abort_check'])
    assert captured['max_429_attempts'] > 0


def test_attempt_restart_after_visible_delta_is_typed_failure_not_success():
    async def _dispatch(
            messages, *, on_content=None, on_attempt_restart=None, **kwargs):
        on_content('discarded-prefix')
        await asyncio.sleep(0)
        on_attempt_restart(reason='429 rotation')
        await asyncio.sleep(0)
        on_content('authoritative')
        return ({'content': 'authoritative'}, 'stop', {})

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    contents = [
        obj['choices'][0]['delta'].get('content')
        for obj in objs
        if isinstance(obj, dict) and 'choices' in obj
        and obj['choices'][0]['delta'].get('content')
    ]
    assert contents == ['discarded-prefix']
    assert objs[-2]['error']['code'] == \
        'provider_attempt_restarted_after_output'
    assert not any(
        isinstance(obj, dict) and obj.get('choices')
        and obj['choices'][0].get('finish_reason') == 'stop'
        for obj in objs
    )


def test_bounded_relay_overflow_fails_instead_of_dropping_and_stopping():
    async def _dispatch(messages, *, on_content=None, **kwargs):
        on_content('A')
        on_content('B')
        on_content('C')
        return ({'content': 'ABC'}, 'stop', {})

    from routes.api_v1.chat_direct import run_direct_stream
    stream = run_direct_stream(
        [{'role': 'user', 'content': 'hi'}],
        model='test-model', cfg={'maxTokens': 100, 'temperature': 0},
        completion_id='chatcmpl-overflow', dispatch_fn=_dispatch,
        queue_maxsize=1,
    )
    objs = _frames_to_objs(_run(_drain(stream)))
    assert objs[-2]['error']['code'] == 'relay_queue_overflow'
    assert objs[-1] == '[DONE]'


def test_dispatch_exception_is_redacted_from_public_sse():
    secret = 'Bearer sk-sensitive-value'

    async def _dispatch(messages, **kwargs):
        raise RuntimeError(secret)

    frames = _run(_drain(_make_core(_dispatch)))
    assert secret not in ''.join(frames)
    objs = _frames_to_objs(frames)
    assert objs[-2]['error']['message'] == objs[-2]['tofu_error']['message']
    assert not objs[-2]['tofu_error'].get('raw')
    assert not objs[-2]['tofu_error'].get('detail')


def test_terminal_resource_failure_refuses_verified_provider_success():
    from lib.agent_core.execution_session import ExecutionPhase, ExecutionSession
    from routes.api_v1.chat_direct import run_direct_stream

    async def _dispatch(messages, *, on_content=None, **kwargs):
        on_content('provider-success')
        return ({'content': 'provider-success'}, 'stop', {})

    session = ExecutionSession(
        execution_id='direct-resource-failure',
        kind='chat_direct',
        owner_user_id=1,
        deadline_seconds=60,
    )
    session.hold_resource(
        'model_route',
        lambda _context: (_ for _ in ()).throw(RuntimeError('dispose failed')),
    )
    terminal = {}

    def _settle():
        terminal['execution_receipt'] = session.settle(
            ExecutionPhase.COMPLETED)

    stream = run_direct_stream(
        [{'role': 'user', 'content': 'hi'}],
        model='test-model', cfg={'maxTokens': 100, 'temperature': 0},
        completion_id='chatcmpl-resource-failure', dispatch_fn=_dispatch,
        execution_session=session,
        dispatch_terminal=terminal,
        on_dispatch_settled=_settle,
    )
    objs = _frames_to_objs(_run(_drain(stream)))
    assert objs[-2]['error']['code'] == 'terminal_resource_invariant_failed'
    assert not any(
        isinstance(obj, dict) and obj.get('choices')
        and obj['choices'][0].get('finish_reason') == 'stop'
        for obj in objs
    )


@pytest.mark.parametrize(
    ('settler', 'cause'),
    [
        (lambda: None, 'dispatch_settlement_missing'),
        (
            lambda: (_ for _ in ()).throw(RuntimeError('settler failed')),
            'dispatch_settlement_failed',
        ),
    ],
)
def test_missing_or_failed_terminal_settlement_refuses_provider_success(
        settler, cause):
    from lib.agent_core.execution_session import ExecutionSession
    from routes.api_v1.chat_direct import run_direct_stream

    async def _dispatch(messages, **kwargs):
        return ({'content': ''}, 'stop', {})

    session = ExecutionSession(
        execution_id=f'direct-{cause}',
        kind='chat_direct',
        owner_user_id=1,
        deadline_seconds=60,
    )
    stream = run_direct_stream(
        [{'role': 'user', 'content': 'hi'}],
        model='test-model', cfg={'maxTokens': 100, 'temperature': 0},
        completion_id=f'chatcmpl-{cause}', dispatch_fn=_dispatch,
        execution_session=session,
        dispatch_terminal={},
        on_dispatch_settled=settler,
    )

    objs = _frames_to_objs(_run(_drain(stream)))
    assert objs[-2]['error']['code'] == cause
    assert session.phase.value == 'failed'


def test_worker_thread_callbacks_cross_the_loop_before_terminal_frame():
    async def _dispatch(messages, *, on_content=None, **kwargs):
        await asyncio.to_thread(on_content, 'thread-result')
        return ({'content': 'thread-result'}, 'stop', {})

    objs = _frames_to_objs(_run(_drain(_make_core(_dispatch))))
    contents = [
        obj['choices'][0]['delta'].get('content')
        for obj in objs
        if isinstance(obj, dict) and obj.get('choices')
    ]
    assert 'thread-result' in contents
    assert objs[-2]['choices'][0]['finish_reason'] == 'stop'


def test_client_disconnect_does_not_cancel_started_provider_dispatch():
    async def _scenario():
        import asyncio
        import routes.api_v1.chat_direct as direct

        provider_started = asyncio.Event()
        release_provider = asyncio.Event()
        provider_completed = asyncio.Event()
        admission_settled = asyncio.Event()
        cancellation_seen = {'value': False}

        async def _dispatch(
                messages, *, on_content=None, on_thinking=None, **kw):
            provider_started.set()
            on_content('prefix')
            try:
                await release_provider.wait()
            except asyncio.CancelledError:
                cancellation_seen['value'] = True
                raise
            # This callback is intentionally after the HTTP consumer is gone.
            # The parser still completes; the relay chunk is simply discarded.
            on_content('tail')
            provider_completed.set()
            return ({'content': 'prefixtail'}, 'stop', {
                'completion_tokens': 2,
            })

        stream = direct.run_direct_stream(
            [{'role': 'user', 'content': 'hi'}],
            model='test-model',
            cfg={'maxTokens': 100, 'temperature': 0},
            completion_id='chatcmpl-detached',
            dispatch_fn=_dispatch,
            on_dispatch_settled=admission_settled.set,
        )

        first = await anext(stream)
        assert first.startswith('data: ')
        assert provider_started.is_set()
        await stream.aclose()
        assert not provider_completed.is_set()
        assert not admission_settled.is_set()

        release_provider.set()
        await asyncio.wait_for(provider_completed.wait(), timeout=1.0)
        await asyncio.wait_for(admission_settled.wait(), timeout=1.0)
        await asyncio.sleep(0)

        assert cancellation_seen['value'] is False
        assert not direct._DETACHED_DIRECT_DISPATCHES

    _run(_scenario())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
