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


def _timeout_error(cause='attempt_deadline'):
    from lib.llm.llm_errors import ResponseReadTimeoutError
    return ResponseReadTimeoutError(
        base_url='http://fake.local/v1',
        timeout_seconds=20.0,
        cause_type=cause)


def test_interactive_dispatch_keeps_the_historical_stream_call_shape(
        monkeypatch):
    """A transport double that mirrors the old signature must still work."""
    from lib.llm_dispatch import api

    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _FakeDispatcher([_make_slot()]))

    def _strict_stream(body, **kwargs):
        unexpected = set(kwargs) & {'attempt_timeout', 'first_byte_timeout'}
        assert not unexpected, f'unexpected transport kwargs: {unexpected}'
        return _OK

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _strict_stream)

    message, _finish, _usage = api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
    assert message['content'] == '译文'


def test_attempt_timeout_rotates_to_the_next_pair(monkeypatch):
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

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

    message, finish, usage = api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], log_prefix='[t]',
        attempt_timeout=20.0, first_byte_timeout=20.0)

    assert message['content'] == '译文'
    assert finish == 'stop'
    assert len(calls) == 2, 'the silent pair must be rotated, not re-picked'
    # The timed-out pair carries the failure, and the retry landed elsewhere.
    assert silent.total_errors >= 1
    assert silent.last_success_time == 0
    assert healthy.last_success_time > 0


def test_ambiguous_transport_failure_still_surfaces(monkeypatch):
    """A non-timeout transport failure may already have been billed."""
    from lib.llm_dispatch import api
    from lib.llm.llm_errors import RequestTransportError

    slot = _make_slot()
    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _FakeDispatcher([slot, _make_slot(key='k9')]))

    def _fake_stream(body, **kwargs):
        raise RequestTransportError(
            failure_stage='connection', reason='delivery_ambiguous',
            base_url='http://fake.local/v1')

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

    with pytest.raises(RequestTransportError):
        api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')
    assert slot.total_errors == 0, (
        'an ambiguous delivery is not slot-health evidence')
