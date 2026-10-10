"""Offline specifications for neutral, exactly-once local dispatch settlement.

Real reasoning/conversion validators fail behind request-owned body/transport
seams; no dispatcher singleton, provider, credential store or network is used.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.llm.conversion import ProtocolConversionError, object_arguments
from lib.llm.reasoning import ReasoningContractError, capture_intent
from lib.llm_errors import LocalRequestPreparationError

pytestmark = pytest.mark.unit


class _ReservedSlot(SimpleNamespace):
    def __init__(self, name):
        super().__init__(
            key_name=name, model='offline-local-dispatch', api_key='fake-key',
            provider_id='offline-provider', routing_provider_id='',
            base_url='https://provider.invalid', extra_headers={}, oauth='',
            adapter={}, protocol='openai', responses_profile='compatible',
            thinking_format='none', inference_profile={}, stream_only=False,
            inflight=0, release_calls=0,
            total_errors=7, consecutive_errors=2, cooldown_until=123.0,
            cooldown_reason='existing', last_error_time=42.0,
            last_error_msg='existing failure', last_success_time=24.0,
            record_error=Mock(), record_success=Mock(),
            record_truncation=Mock(),
        )

    def release(self):
        self.release_calls += 1
        self.inflight -= 1  # Deliberately not clamped: double release is visible.

    def health(self):
        return (self.total_errors, self.consecutive_errors, self.cooldown_until,
                self.cooldown_reason, self.last_error_time, self.last_error_msg,
                self.last_success_time)


class _RequestDispatcher:
    def __init__(self):
        self.slots = [_ReservedSlot('primary'), _ReservedSlot('fallback')]
        self.picks = []
        self.note_shared_contention = Mock()
        self.note_shared_contention_recovered = Mock()

    def summarize_slots(self, *args, **kwargs):
        return 'offline primary and fallback'

    def pick_and_reserve(self, **kwargs):
        self.picks.append(kwargs)
        # Fail fast rather than hanging if a regression starts retrying.
        assert len(self.picks) == 1, 'local failure must not retry or fall back'
        self.slots[0].inflight += 1
        return self.slots[0]


@pytest.fixture
def dispatch_fixture(monkeypatch):
    from lib.llm_dispatch import api

    dispatcher = _RequestDispatcher()
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(api, '_audit_severity_downgrade', lambda: None)
    monkeypatch.setattr('lib.llm_dispatch._api_stream._check_request_headroom',
                        lambda *a, **kw: None)
    monkeypatch.setattr('lib.llm_dispatch.conv_affinity.get_conv_affinity',
                        lambda: '')
    sleep = Mock(side_effect=AssertionError('local failure must not back off'))
    monkeypatch.setattr('lib.llm._transport.abortable_sleep', sleep)
    monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', sleep)
    return api, dispatcher, sleep


def _fail_validation(kind):
    if kind == 'reasoning':
        capture_intent(thinking_enabled='not-a-boolean')
    else:
        object_arguments('[]')
    pytest.fail('invalid input must be rejected before provider I/O')


@pytest.mark.parametrize('kind,error_type', [
    ('reasoning', ReasoningContractError),
    ('conversion', ProtocolConversionError),
])
@pytest.mark.parametrize('entry,stage', [
    ('sync_stream', 'body_adaptation'),
    ('async_stream', 'body_adaptation'),
    ('sync_stream', 'transport_preparation'),
    ('async_stream', 'transport_preparation'),
    ('chat', 'transport_preparation'),
])
def test_local_failure_releases_once_without_health_retry_or_fallback(
        monkeypatch, dispatch_fixture, entry, stage, kind, error_type):
    import lib.llm as llm
    from lib.llm import astream

    api, dispatcher, sleep = dispatch_fixture
    snapshots = [slot.health() for slot in dispatcher.slots]
    failures = []
    provider_send = Mock(side_effect=AssertionError('provider I/O is forbidden'))
    preparations = Mock()
    restarts = Mock()
    content = Mock()

    def prepare(*args, **kwargs):
        preparations()
        assert dispatcher.slots[0].inflight == 1
        try:
            _fail_validation(kind)
        except error_type as exc:
            failures.append(exc)
            raise
        provider_send()  # A local validator must never reach this boundary.

    async def async_prepare(*args, **kwargs):
        return prepare(*args, **kwargs)

    if stage == 'body_adaptation':
        # Run the real shared slot adapter up to its body-builder boundary.
        monkeypatch.setattr(llm, 'build_body', prepare)
        monkeypatch.setattr(llm, 'stream_chat', provider_send)
        monkeypatch.setattr(astream, 'async_stream_chat', provider_send)
    else:
        # Request-owned body avoids model catalog/storage lookups; the actual
        # public dispatcher still owns admission, exception handling and cleanup.
        monkeypatch.setattr(api, '_adapt_stream_body_for_slot',
                            lambda *a, **kw: {'messages': []})
        monkeypatch.setattr(llm, 'stream_chat', prepare)
        monkeypatch.setattr(astream, 'async_stream_chat', async_prepare)
        monkeypatch.setattr(llm, 'chat', prepare)

    messages = [{'role': 'user', 'content': 'offline specification'}]
    kwargs = {'max_retries': 3, 'owner_user_id': 987654}
    with pytest.raises(LocalRequestPreparationError) as raised:
        if entry == 'chat':
            api.dispatch_chat(messages, **kwargs)
        else:
            kwargs.update(on_attempt_restart=restarts, on_content=content)
            if entry == 'async_stream':
                asyncio.run(api.async_dispatch_stream(messages, **kwargs))
            else:
                api.dispatch_stream(messages, **kwargs)

    assert len(failures) == 1
    assert isinstance(failures[0], error_type)
    if stage == 'body_adaptation':
        assert raised.value.__cause__ is failures[0]
        assert raised.value.stage == 'slot_body_adaptation'
    else:
        assert raised.value is failures[0]
    assert len(dispatcher.picks) == 1
    assert [slot.release_calls for slot in dispatcher.slots] == [1, 0]
    assert [slot.inflight for slot in dispatcher.slots] == [0, 0]
    assert [slot.health() for slot in dispatcher.slots] == snapshots
    for slot in dispatcher.slots:
        slot.record_error.assert_not_called()
        slot.record_success.assert_not_called()
        slot.record_truncation.assert_not_called()
    dispatcher.note_shared_contention.assert_not_called()
    dispatcher.note_shared_contention_recovered.assert_not_called()
    preparations.assert_called_once_with()
    provider_send.assert_not_called()
    restarts.assert_not_called()
    content.assert_not_called()
    sleep.assert_not_called()
