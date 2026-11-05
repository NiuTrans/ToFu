"""A per-attempt transport timeout must rotate the pair, not surface.

Before this spec the streaming dispatch surfaced ``RequestTransportError``
untouched, leaving slot health unmarked for a TIMEOUT. The picker then re-chose
the same dead pair, so a caller-funded retry bought nothing: this is the second
half of the "one silent provider ate the whole translation budget" failure
(conv mvhiznrwv669ue, 2026-10-21). A timeout is pair evidence — the pair
accepted the request and stopped producing bytes — while a genuinely ambiguous
delivery failure must still surface so a caller cannot double-charge.
"""

from __future__ import annotations

import os
import sys
import asyncio
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


def _make_slot(model='grok-4.6', key='k0'):
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
        slot.record_request()
        return slot

    def has_capable_slots(self, *a, **kw):
        return bool(self._slots)

    def summarize_slots(self, *a, **kw):
        return 'fake-slots'


_OK = (
    {'role': 'assistant', 'content': '译文'}, 'stop',
    {'prompt_tokens': 10, 'completion_tokens': 4},
)


@pytest.fixture(params=['sync', 'async'])
def stream_dispatch(request, monkeypatch):
    from lib.llm_dispatch import api

    def install(callback):
        if request.param == 'sync':
            monkeypatch.setattr('lib.llm.stream_chat', callback)
        else:
            async def transport(*args, **kwargs):
                return callback(*args, **kwargs)
            monkeypatch.setattr('lib.llm.astream.async_stream_chat', transport)

    def invoke(*args, **kwargs):
        if request.param == 'sync':
            return api.dispatch_stream(*args, **kwargs)
        return asyncio.run(api.async_dispatch_stream(*args, **kwargs))

    return SimpleNamespace(install=install, invoke=invoke, kind=request.param)


def _timeout_error(cause='attempt_deadline'):
    from lib.llm.llm_errors import ResponseReadTimeoutError
    return ResponseReadTimeoutError(
        base_url='http://fake.local/v1',
        timeout_seconds=20.0,
        cause_type=cause)


def test_interactive_dispatch_keeps_the_historical_stream_call_shape(
        monkeypatch, stream_dispatch):
    """A transport double that mirrors the old signature must still work."""
    from lib.llm_dispatch import api

    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _FakeDispatcher([_make_slot()]))

    def _strict_stream(body, **kwargs):
        unexpected = set(kwargs) & {'attempt_timeout', 'first_byte_timeout'}
        assert not unexpected, f'unexpected transport kwargs: {unexpected}'
        return _OK

    stream_dispatch.install(_strict_stream)

    message, _finish, _usage = stream_dispatch.invoke(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
    assert message['content'] == '译文'


def test_attempt_timeout_rotates_to_the_next_pair(monkeypatch, stream_dispatch):
    from lib.llm_dispatch import api

    silent = _make_slot(key='k1')
    healthy = _make_slot(key='k2', model='deepseek-v4.1-flash')
    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _FakeDispatcher([silent, healthy]))
    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                        lambda *_a, **_k: None)

    calls = []

    def _fake_stream(body, **kwargs):
        calls.append(body)
        if len(calls) == 1:
            raise _timeout_error()
        return _OK

    stream_dispatch.install(_fake_stream)

    timeouts = {'attempt_timeout': 20.0, 'first_byte_timeout': 20.0} if stream_dispatch.kind == 'sync' else {}
    retries, restarts = [], []
    message, finish, usage = stream_dispatch.invoke(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        on_retry=lambda **value: retries.append(value),
        on_attempt_restart=lambda **value: restarts.append(value), **timeouts)

    assert message['content'] == '译文'
    assert finish == 'stop'
    assert len(calls) == 2, 'the silent pair must be rotated, not re-picked'
    # The timed-out pair carries the failure, and the retry landed elsewhere.
    assert silent.total_errors >= 1
    assert silent.last_success_time == 0
    assert healthy.last_success_time > 0
    assert silent.inflight == healthy.inflight == 0
    assert retries[0]['reason'] == 'Request timed out'
    assert retries[0]['attempt_model'] == silent.model
    assert restarts == [{'reason': 'transport timeout — rotating slot'}]


@pytest.mark.parametrize('stage,reason', [
    ('connection', 'delivery_ambiguous'), ('upload', 'timeout'), ('tls', 'handshake_failed'),
])
def test_ambiguous_transport_failure_still_surfaces(monkeypatch, stream_dispatch, stage, reason):
    """A non-timeout transport failure may already have been billed."""
    from lib.llm_dispatch import api
    from lib.llm.llm_errors import RequestTransportError

    slot = _make_slot()
    unused = _make_slot(key='k9')
    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _FakeDispatcher([slot, unused]))
    error = RequestTransportError(failure_stage=stage, reason=reason,
                                  base_url='http://fake.local/v1')

    def _fake_stream(body, **kwargs):
        raise error

    stream_dispatch.install(_fake_stream)

    with pytest.raises(RequestTransportError) as failure:
        stream_dispatch.invoke(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
    assert failure.value is error
    assert slot.total_errors == 0, (
        'an ambiguous delivery is not slot-health evidence')
    assert slot.inflight == 0
    assert unused.total_requests == 0


def test_read_timeout_preserves_the_callers_attempt_budget(monkeypatch, stream_dispatch):
    from lib.llm_dispatch import api

    silent, unused = _make_slot(), _make_slot(key='unused')
    monkeypatch.setattr(api, 'get_dispatcher', lambda: _FakeDispatcher([silent, unused]))
    error = _timeout_error()

    def transport(*args, **kwargs):
        raise error

    stream_dispatch.install(transport)
    with pytest.raises(type(error)) as failure:
        stream_dispatch.invoke([{'role': 'user', 'content': 'hi'}], max_retries=1)
    assert failure.value is error
    assert silent.inflight == 0 and silent.total_errors == 1
    assert unused.total_requests == 0


@pytest.mark.parametrize('exception_type', [asyncio.CancelledError, KeyboardInterrupt])
def test_control_signals_release_without_health_damage(monkeypatch, stream_dispatch, exception_type):
    from lib.llm_dispatch import api

    slot, unused = _make_slot(), _make_slot(key='unused')
    monkeypatch.setattr(api, 'get_dispatcher', lambda: _FakeDispatcher([slot, unused]))
    error = exception_type()

    def transport(*args, **kwargs):
        raise error

    stream_dispatch.install(transport)
    with pytest.raises(exception_type) as failure:
        stream_dispatch.invoke([{'role': 'user', 'content': 'hi'}])
    assert failure.value is error
    assert slot.inflight == slot.total_errors == 0
    assert unused.total_requests == 0


def test_oauth_refresh_is_owned_once_by_each_request(monkeypatch, stream_dispatch):
    from lib.llm_dispatch import api
    from lib.llm.llm_errors import PermissionError_

    refreshes = []
    monkeypatch.setattr(api, '_force_oauth_token_refresh',
        lambda provider, prefix, owner: refreshes.append((provider, owner)) or True)
    for owner in (41, 42):
        slot, healthy = _make_slot(), _make_slot(key='healthy')
        slot.oauth = 'codex'
        monkeypatch.setattr(api, 'get_dispatcher', lambda: _FakeDispatcher([slot, slot, healthy]))
        calls = []

        def transport(*args, **kwargs):
            calls.append(None)
            if len(calls) <= 2:
                raise PermissionError_('credential rejected', status_code=401)
            return _OK

        stream_dispatch.install(transport)
        assert stream_dispatch.invoke([{'role': 'user', 'content': 'hi'}],
            owner_user_id=owner)[0]['content'] == '译文'
        assert len(calls) == 3 and slot.total_errors == 1
        assert slot.inflight == healthy.inflight == 0
    assert refreshes == [('codex', 41), ('codex', 42)]
