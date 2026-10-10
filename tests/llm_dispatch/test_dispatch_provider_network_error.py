"""Provider-reported ``network_error`` finish is retried, never accepted empty.

tb21 v11 evidence: zai terminated well-formed SSE streams with an explicit
``finish_reason='network_error'`` (230 bytes, 2 frames, finish+DONE seen) and
the dispatch layer recorded it as a successful turn with zero actionable
content, silently ending the agent. The dispatch loops (sync + async) must
treat that shape as one bounded non-429 failure and rotate the slot instead.
Turns that already carry content or tool calls keep the historical handling.

Run: pytest tests/llm_dispatch/test_dispatch_provider_network_error.py -m unit
"""
from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _make_slot(model='glm-5.3-m17', key='k0'):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'})


class _FakeDispatcher:
    def __init__(self, slots):
        self._slots = list(slots)

    def pick_and_reserve(self, **kwargs):
        if not self._slots:
            return None
        slot = self._slots.pop(0)
        if slot is not None:
            slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


_EMPTY_NETWORK_ERROR = (
    {'role': 'assistant', 'content': ''}, 'network_error',
    {'prompt_tokens': 10, 'completion_tokens': 0},
)
_OK = (
    {'role': 'assistant', 'content': 'recovered'}, 'stop',
    {'prompt_tokens': 11, 'completion_tokens': 5},
)


@pytest.mark.unit
class TestSyncProviderNetworkErrorRetry:
    def test_empty_network_error_is_retried_then_succeeds(self, monkeypatch):
        from lib.llm_dispatch import api

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        monkeypatch.setattr(api, 'get_dispatcher',
                            lambda: _FakeDispatcher([slot1, slot2]))
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                            lambda *_a, **_k: None)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            return _EMPTY_NETWORK_ERROR if calls['n'] == 1 else _OK

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        result = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        msg, finish, usage = result

        assert msg['content'] == 'recovered'
        assert finish == 'stop'
        assert calls['n'] == 2
        assert slot1.total_errors >= 1
        assert slot1.last_success_time == 0
        assert slot2.last_success_time > 0
        assert usage['_dispatch']['attempt'] == 2

    def test_content_carrying_network_error_is_not_retried(self, monkeypatch):
        from lib.llm_dispatch import api

        slot = _make_slot()
        monkeypatch.setattr(api, 'get_dispatcher',
                            lambda: _FakeDispatcher([slot]))
        payload = (
            {'role': 'assistant', 'content': 'partial answer'},
            'network_error', {'prompt_tokens': 10, 'completion_tokens': 4},
        )
        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            return payload

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, _usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg['content'] == 'partial answer'
        assert finish == 'network_error'
        assert calls['n'] == 1

    def test_tool_call_network_error_is_not_retried(self, monkeypatch):
        from lib.llm_dispatch import api

        slot = _make_slot()
        monkeypatch.setattr(api, 'get_dispatcher',
                            lambda: _FakeDispatcher([slot]))
        payload = (
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 'c1', 'type': 'function',
                             'function': {'name': 'run', 'arguments': '{}'}}]},
            'network_error', {'prompt_tokens': 10, 'completion_tokens': 4},
        )
        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            return payload

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        _msg, finish, _usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
        assert finish == 'network_error'
        assert calls['n'] == 1

    def test_retry_budget_exhaustion_raises_typed_error(self, monkeypatch):
        from lib.llm_dispatch import api

        slots = [_make_slot(key=f'k{i}') for i in range(4)]
        monkeypatch.setattr(api, 'get_dispatcher',
                            lambda: _FakeDispatcher(slots))
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                            lambda *_a, **_k: None)

        def _always_network_error(body, **kwargs):
            return _EMPTY_NETWORK_ERROR

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _always_network_error)

        with pytest.raises(Exception) as excinfo:
            api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
                max_retries=2)
        assert 'network_error' in str(excinfo.value) or \
            'exhausted' in str(excinfo.value).lower()


@pytest.mark.unit
class TestAsyncProviderNetworkErrorRetry:
    def test_async_empty_network_error_is_retried(self, monkeypatch):
        from lib.llm_dispatch import api

        slot1 = _make_slot(key='k1')
        slot2 = _make_slot(key='k2')
        monkeypatch.setattr(api, 'get_dispatcher',
                            lambda: _FakeDispatcher([slot1, slot2]))

        async def _no_sleep(secs, abort_check=None):
            return None
        monkeypatch.setattr(
            'lib.llm._transport.async_abortable_sleep', _no_sleep)

        calls = {'n': 0}

        async def _fake_astream(body, **kwargs):
            calls['n'] += 1
            return _EMPTY_NETWORK_ERROR if calls['n'] == 1 else _OK

        import lib.llm.astream as astream_mod
        monkeypatch.setattr(astream_mod, 'async_stream_chat', _fake_astream)

        msg, finish, usage = asyncio.new_event_loop().run_until_complete(
            api.async_dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], log_prefix='[t]'))

        assert msg['content'] == 'recovered'
        assert finish == 'stop'
        assert calls['n'] == 2
        assert slot1.total_errors >= 1
        assert slot2.last_success_time > 0
        assert usage['_dispatch']['attempt'] == 2
