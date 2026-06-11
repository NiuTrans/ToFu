"""Tests for the native-async ``async_dispatch_stream`` (lib/llm_dispatch/api.py).

Stage-1 of the native-async migration replaced the old
``await asyncio.to_thread(dispatch_stream, ...)`` stopgap with a genuine async
loop that drives the ``async_stream_chat`` (httpx) transport on the event loop.
These tests pin that behaviour without any network:

  - it is a coroutine function (runs on the loop, not the thread pool);
  - success returns (msg, finish, usage), records slot success, injects
    ``usage['_dispatch']`` metadata;
  - a 429 (RateLimitError) is retried (free) then succeeds on the next slot;
  - AbortedError propagates immediately (no retry) and releases the slot.

Run:  pytest tests/test_async_dispatch_stream.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_slot(model='qwen-plus', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


class _FakeDispatcher:
    """Hands out a queued list of slots; None entries simulate 'no slot'."""

    def __init__(self, slots):
        self._slots = list(slots)
        self.picks = 0

    def pick_and_reserve(self, **kwargs):
        self.picks += 1
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()  # mirror real pick_and_reserve(reserve=True)
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.unit
class TestAsyncDispatchStreamIsNative:
    def test_is_coroutine_function(self):
        from lib.llm_dispatch.api import async_dispatch_stream
        assert asyncio.iscoroutinefunction(async_dispatch_stream)

    def test_does_not_use_to_thread_stopgap(self):
        # Regression guard: the old impl body was literally
        # `await asyncio.to_thread(dispatch_stream, ...)`. Ensure we no longer
        # delegate the whole thing to a thread (which would re-block via
        # the sync `requests` transport).
        import inspect

        from lib.llm_dispatch import api
        src = inspect.getsource(api.async_dispatch_stream)
        # Match an actual delegating CALL, not the docstring's prose mention
        # of the old stopgap. The call form was `asyncio.to_thread(...)`.
        assert 'asyncio.to_thread(dispatch_stream' not in src
        assert 'await async_stream_chat(' in src


@pytest.mark.unit
class TestAsyncDispatchStreamSuccess:
    def test_success_returns_tuple_and_records_slot(self, monkeypatch):
        from lib.llm_dispatch import api

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        async def _fake_astream(body, **kwargs):
            # The async transport is awaited on the loop.
            kwargs['on_content']('hello ')
            kwargs['on_content']('world')
            return 'hello world', 'stop', {'completion_tokens': 7}

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        chunks = []
        msg, finish, usage = _run(api.async_dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            on_content=chunks.append, log_prefix='[t]'))

        assert msg == 'hello world'
        assert finish == 'stop'
        assert chunks == ['hello ', 'world']
        # Slot success recorded → inflight released, success timestamp set,
        # error streak cleared (Slot has no total_successes field).
        assert slot.inflight == 0
        assert slot.last_success_time > 0
        assert slot.consecutive_errors == 0
        # Dispatch metadata injected.
        assert usage['_dispatch']['model'] == 'qwen-plus'
        assert usage['_dispatch']['key'] == 'k0'
        assert usage['_dispatch']['429_retries'] == 0


@pytest.mark.unit
class TestAsyncDispatchStreamRetry:
    def test_429_then_success(self, monkeypatch):
        from lib.llm_dispatch import api

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        # Avoid real sleeping between 429 retries.
        async def _no_sleep(secs, abort_check=None):
            return None
        monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', _no_sleep)

        from lib.llm_errors import RateLimitError
        calls = {'n': 0}

        async def _fake_astream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('429 too many requests')
            return 'ok', 'stop', {}

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        msg, finish, usage = _run(api.async_dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]'))

        assert msg == 'ok'
        assert calls['n'] == 2
        assert usage['_dispatch']['429_retries'] >= 1
        # First slot saw a rate-limit error recorded; second slot succeeded.
        assert slot1.total_errors >= 1
        assert slot2.last_success_time > 0


@pytest.mark.unit
class TestAsyncDispatchStreamUnreachableFailover:
    def test_unreachable_cools_slot_and_fails_over(self, monkeypatch):
        from lib.llm_dispatch import api

        dead = _make_slot(key='dead')
        live = _make_slot(key='live')
        disp = _FakeDispatcher([dead, live])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        from lib.llm_errors import EndpointUnreachableError
        calls = {'n': 0}

        async def _fake_astream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise EndpointUnreachableError(
                    'endpoint unreachable: connect timeout',
                    base_url='http://33.236.243.109:8080/v1')
            return 'ok', 'stop', {}

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        msg, finish, usage = _run(api.async_dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]'))

        # Failed over to the live slot — did NOT retry the dead one.
        assert msg == 'ok'
        assert calls['n'] == 2
        # The dead slot was cooled down (routed around), not just error-bumped.
        assert dead.cooldown_until > time.time()
        assert dead.total_errors >= 1
        assert live.last_success_time > 0


@pytest.mark.unit
class TestAsyncDispatchStreamAllUnreachable:
    def test_all_unreachable_raises_friendly_error(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import EndpointUnreachableError

        # Single dead slot; after it's excluded the dispatcher returns None.
        dead = _make_slot(model='glm5.1-FP8', key='dead')
        disp = _FakeDispatcher([dead])
        # has_capable_slots False once the pair is excluded → loop ends.
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        async def _fake_astream(body, **kwargs):
            raise EndpointUnreachableError(
                'endpoint unreachable: connect timeout',
                base_url='http://33.236.243.109:8080/v1')

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        with pytest.raises(EndpointUnreachableError) as ei:
            _run(api.async_dispatch_stream(
                [{'role': 'user', 'content': 'hi'}],
                prefer_model='glm5.1-FP8', strict_model=True,
                max_retries=3, log_prefix='[t]'))
        # The terminal error must be the AGGREGATED friendly message
        # naming the model — not the raw urllib3/httpx connect error.
        msg = str(ei.value)
        assert 'unreachable' in msg.lower()
        assert 'glm5.1-FP8' in msg


@pytest.mark.unit
class TestAsyncDispatchStreamAbort:
    def test_abort_propagates_and_releases_slot(self, monkeypatch):
        from lib.llm_dispatch import api
        from lib.llm_errors import AbortedError

        slot = _make_slot()
        disp = _FakeDispatcher([slot])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)

        async def _fake_astream(body, **kwargs):
            raise AbortedError('user aborted')

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        with pytest.raises(AbortedError):
            _run(api.async_dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], log_prefix='[t]'))
        # Abort must release the inflight reservation, not leak it.
        assert slot.inflight == 0
