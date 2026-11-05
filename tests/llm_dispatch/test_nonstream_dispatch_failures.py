"""Non-stream failures release reservations and stop impossible retry waits.

Public dispatch runs against real slots and controlled provider/selection ports;
the selection double applies the exclusions supplied by the dispatcher.
"""
import asyncio

import pytest

from lib.llm.llm_errors import AbortedError, RateLimitError, RequestTransportError, StreamOnlyError
from tests.llm_dispatch.test_async_dispatch_stream import _FakeDispatcher, _make_slot

pytestmark = pytest.mark.unit
MESSAGES = [{'role': 'user', 'content': 'hello'}]


def _queued(monkeypatch, slots):
    from lib.llm_dispatch import api
    dispatcher = _FakeDispatcher(slots)
    dispatcher.slots = [slot for slot in slots if slot is not None]
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    return dispatcher


@pytest.mark.parametrize('phase', ['admission', 'transport'])
@pytest.mark.parametrize('exception_type', [asyncio.CancelledError, KeyboardInterrupt, AbortedError])
def test_control_signals_release_exactly_once(monkeypatch, phase, exception_type):
    from lib.llm_dispatch import api
    slot, unused = _make_slot(), _make_slot(key='unused')
    dispatcher = _queued(monkeypatch, [slot, unused])
    error = exception_type('controlled stop')
    releases, release = [], slot.release

    def tracked_release():
        releases.append(True)
        release()

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(slot, 'release', tracked_release)
    if phase == 'admission':
        monkeypatch.setattr('lib.llm_dispatch._api_chat._admit_or_defer_shared_contention', fail)
        monkeypatch.setattr('lib.llm.chat', lambda **kwargs: pytest.fail('no provider request'))
    else:
        monkeypatch.setattr('lib.llm.chat', fail)
    with pytest.raises(exception_type) as caught:
        api.dispatch_chat(MESSAGES)
    assert caught.value is error
    assert releases == [True]
    assert slot.inflight == slot.total_errors == 0
    assert dispatcher.picks == 1 and unused.total_requests == 0


def test_admission_failure_releases_without_provider_health_damage(monkeypatch):
    from lib.llm_dispatch import api
    slot = _make_slot()
    _queued(monkeypatch, [slot])
    error = RuntimeError('local admission failure')

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr('lib.llm_dispatch._api_chat._admit_or_defer_shared_contention', fail)
    monkeypatch.setattr('lib.llm.chat', lambda **kwargs: pytest.fail('no provider request'))
    with pytest.raises(RuntimeError) as caught:
        api.dispatch_chat(MESSAGES)
    assert caught.value is error
    assert slot.inflight == slot.total_errors == 0


@pytest.mark.parametrize('exception_type', [RuntimeError, asyncio.CancelledError, KeyboardInterrupt, AbortedError])
def test_post_settlement_failure_preserves_other_reservations(monkeypatch, exception_type):
    from lib.llm_dispatch import api
    slot, unused = _make_slot(), _make_slot(key='unused')
    dispatcher = _queued(monkeypatch, [slot, unused])
    slot.record_request()  # An independently owned request already uses this slot.
    error = exception_type('controlled post-settlement failure')

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr('lib.llm.chat', lambda **kwargs: ('done', {}))
    monkeypatch.setattr('lib.llm_dispatch._api_chat._note_shared_contention_recovered', fail)
    try:
        with pytest.raises(exception_type) as caught:
            api.dispatch_chat(MESSAGES, max_retries=1)
        assert caught.value is error
        assert slot.inflight == 1, 'the other reservation remains owned by its request'
        assert slot.total_errors == 0 and slot.last_success_time > 0
        assert dispatcher.picks == 1 and unused.total_requests == 0
    finally:
        slot.release()


class _ExclusionAwareDispatcher:
    def __init__(self, slots):
        self.slots = slots

    def _eligible(self, exclude_models=None, exclude_keys=None, exclude_pairs=None, **kwargs):
        return [slot for slot in self.slots
                if slot.model not in (exclude_models or ())
                and slot.key_name not in (exclude_keys or ())
                and (slot.key_name, slot.model) not in (exclude_pairs or ())]

    def pick_and_reserve(self, **kwargs):
        for slot in self._eligible(**kwargs):
            if slot.is_available:
                slot.record_request()
                return slot
        return None

    def has_capable_slots(self, capability, **kwargs):
        return bool(self._eligible(**kwargs))

    def summarize_slots(self, *args, **kwargs):
        return 'exclusion-aware slots'

    def cooling_cause_summary(self, *args, **kwargs):
        return set()


@pytest.mark.parametrize('exception_type', [RuntimeError, StreamOnlyError])
def test_permanently_excluded_models_do_not_enter_capacity_wait(monkeypatch, exception_type):
    from lib.llm_dispatch import api
    slot, unused = _make_slot(), _make_slot(key='unused')
    dispatcher = _ExclusionAwareDispatcher([slot, unused])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: pytest.fail(
        'capacity cannot recover a model that this request permanently excludes'))
    error = (exception_type('controlled model failure', slot.model)
             if exception_type is StreamOnlyError else exception_type('controlled model failure'))

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr('lib.llm.chat', fail)
    with pytest.raises(exception_type) as caught:
        api.dispatch_chat(MESSAGES, max_retries=3)
    assert caught.value is error
    assert slot.inflight == 0 and slot.total_errors == 1
    assert unused.total_requests == 0


def test_sensitive_input_rejection_does_not_rotate_same_model_slots(monkeypatch):
    from lib.llm_dispatch import api
    from lib.llm.llm_errors import ModelRefusalError, _classify_http_error

    slot, unused = _make_slot(), _make_slot(key='unused')
    dispatcher = _queued(monkeypatch, [slot, unused])

    def reject(**kwargs):
        _classify_http_error(
            500,
            'API HTTP 500: user sensitive words detected: example_flag (request id: test)',
            slot.model,
            '[test]',
        )

    monkeypatch.setattr('lib.llm.chat', reject)
    with pytest.raises(ModelRefusalError):
        api.dispatch_chat(MESSAGES, max_retries=3)

    assert dispatcher.picks == 1
    assert unused.total_requests == 0
    assert slot.inflight == slot.total_errors == 0


@pytest.mark.parametrize('stage', ['read', 'upload', 'connection'])
def test_nonstream_transport_failures_keep_request_scope(monkeypatch, stage):
    from lib.llm_dispatch import api
    slot, unused = _make_slot(), _make_slot(key='unused')
    dispatcher = _queued(monkeypatch, [slot, unused])
    error = RequestTransportError(failure_stage=stage, reason='controlled_failure')

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr('lib.llm.chat', fail)
    with pytest.raises(RequestTransportError) as caught:
        api.dispatch_chat(MESSAGES)
    assert caught.value is error
    assert slot.inflight == slot.total_errors == 0
    assert dispatcher.picks == 1 and unused.total_requests == 0


def test_local_waits_do_not_spend_upstream_retry_budget(monkeypatch):
    from lib.llm_dispatch import api
    first, second = _make_slot(), _make_slot(key='second')
    _queued(monkeypatch, [None, first, second])
    monkeypatch.setattr(api.time, 'sleep', lambda seconds: None)
    calls = []

    def chat(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RateLimitError('controlled upstream throttle')
        return 'done', {'prompt_tokens': 2, 'completion_tokens': 1}

    monkeypatch.setattr('lib.llm.chat', chat)
    content, usage = api.dispatch_chat(MESSAGES, max_429_attempts=2)
    assert content == 'done' and len(calls) == 2
    assert first.inflight == second.inflight == 0
    counters = usage['_dispatch']
    assert counters['429_retries'] == 2
    assert counters['slot_wait_cycles'] == counters['upstream_429_retries'] == 1
    assert counters['attempt'] == 1


def test_deterministic_bad_request_does_not_rotate_credentials(monkeypatch):
    from lib.llm import BadRequestError
    from lib.llm_dispatch import api
    first, second = _make_slot(), _make_slot(key='second')
    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _ExclusionAwareDispatcher([first, second]))
    calls = []

    def reject(**kwargs):
        calls.append(kwargs['api_key'])
        raise BadRequestError('unchanged request shape rejected')

    monkeypatch.setattr('lib.llm.chat', reject)
    with pytest.raises(BadRequestError, match='unchanged request shape rejected'):
        api.dispatch_chat(MESSAGES, max_retries=3)
    assert calls == [first.api_key]
    assert second.total_requests == 0
    assert first.inflight == first.total_errors == 0


def test_deterministic_bad_request_uses_another_model_once(monkeypatch):
    from lib.llm import BadRequestError
    from lib.llm_dispatch import api
    rejected = _make_slot(key='rejected')
    healthy = _make_slot(key='healthy', model='other-model')
    _queued(monkeypatch, [rejected, healthy])
    calls = []

    def chat(**kwargs):
        calls.append(kwargs['model'])
        if kwargs['model'] == rejected.model:
            raise BadRequestError('route-specific request shape rejected')
        return 'done', {}

    monkeypatch.setattr('lib.llm.chat', chat)
    content, _usage = api.dispatch_chat(MESSAGES, max_retries=3)
    assert content == 'done'
    assert calls == [rejected.model, healthy.model]
    assert rejected.inflight == rejected.total_errors == 0


def test_deterministic_bad_request_keeps_same_wire_on_another_endpoint(monkeypatch):
    """Two gateways serve one wire ID; only the rejecting route is excluded."""
    from lib.llm import BadRequestError
    from lib.llm_dispatch import api
    rejected = _make_slot(key='k0')
    rejected.base_url = 'https://gateway-a.example/v1'
    sibling = _make_slot(key='k1')
    sibling.base_url = rejected.base_url
    healthy = _make_slot(key='k2')
    healthy.base_url = 'https://gateway-b.example/v1'
    dispatcher = _ExclusionAwareDispatcher([rejected, sibling, healthy])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    calls = []

    def chat(**kwargs):
        calls.append(kwargs['api_key'])
        if len(calls) == 1:
            raise BadRequestError('gateway A rejects this request shape')
        return 'done', {}

    monkeypatch.setattr('lib.llm.chat', chat)
    content, _usage = api.dispatch_chat(MESSAGES, max_retries=3)
    assert content == 'done'
    assert calls == [rejected.api_key, healthy.api_key]
    assert sibling.total_requests == 0
    assert healthy.total_requests == 1


@pytest.mark.parametrize('exception_type,strict_model', [(TimeoutError, False), (RuntimeError, True)])
def test_retryable_pair_failure_can_use_another_key(monkeypatch, exception_type, strict_model):
    from lib.llm_dispatch import api
    first, second = _make_slot(), _make_slot(key='second')
    monkeypatch.setattr(api, 'get_dispatcher', lambda: _ExclusionAwareDispatcher([first, second]))
    calls = []

    def chat(**kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise exception_type('controlled pair failure')
        return 'done', {}

    monkeypatch.setattr('lib.llm.chat', chat)
    assert api.dispatch_chat(MESSAGES, strict_model=strict_model)[0] == 'done'
    assert len(calls) == 2
    assert first.total_errors == 1 and second.last_success_time > 0
    assert first.inflight == second.inflight == 0


def test_model_fallback_skips_the_failed_models_other_keys(monkeypatch):
    from lib.llm_dispatch import api
    first, unused = _make_slot(), _make_slot(key='unused')
    healthy = _make_slot(key='healthy', model='deepseek-v4.1-flash')
    monkeypatch.setattr(api, 'get_dispatcher',
                        lambda: _ExclusionAwareDispatcher([first, unused, healthy]))

    def chat(**kwargs):
        if kwargs['model'] == first.model:
            raise RuntimeError('controlled model failure')
        return 'done', {}

    monkeypatch.setattr('lib.llm.chat', chat)
    assert api.dispatch_chat(MESSAGES)[0] == 'done'
    assert first.total_requests == healthy.total_requests == 1
    assert unused.total_requests == 0
    assert first.inflight == healthy.inflight == 0


def test_oauth_refresh_is_once_per_request_and_keeps_the_owner(monkeypatch):
    from lib.llm_dispatch import api
    from lib.llm.llm_errors import PermissionError_
    refreshes = []
    monkeypatch.setattr(api, '_force_oauth_token_refresh',
        lambda provider, prefix, owner: refreshes.append((provider, owner)) or True)
    for owner in (41, 42):
        slot, healthy = _make_slot(), _make_slot(key='healthy')
        slot.oauth = 'codex'
        _queued(monkeypatch, [slot, slot, healthy])
        calls = []

        def chat(**kwargs):
            calls.append(None)
            if len(calls) <= 2:
                raise PermissionError_('controlled credential rejection', status_code=401)
            return 'done', {}

        monkeypatch.setattr('lib.llm.chat', chat)
        assert api.dispatch_chat(MESSAGES, owner_user_id=owner, max_retries=2)[0] == 'done'
        assert len(calls) == 3 and slot.total_errors == 1
        assert slot.inflight == healthy.inflight == 0
    assert refreshes == [('codex', 41), ('codex', 42)]


def test_legacy_quota_wrapper_does_not_introduce_a_new_starvation_deadline(monkeypatch):
    from lib.llm_dispatch import api
    first, second = _make_slot(), _make_slot(key='second')
    _queued(monkeypatch, [first, second])
    now, calls = [0.0], []
    monkeypatch.setattr(api.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(api, '_saturation_budget_secs', lambda: 1)

    def sleep(seconds):
        now[0] += 2

    def chat(**kwargs):
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError('usage_limit_reached')
        return 'done', {}

    monkeypatch.setattr(api.time, 'sleep', sleep)
    monkeypatch.setattr('lib.llm.chat', chat)
    content, usage = api.dispatch_chat(MESSAGES, max_429_attempts=2)
    assert content == 'done' and len(calls) == 2
    assert usage['_dispatch']['upstream_429_retries'] == 1
    assert usage['_dispatch']['slot_wait_cycles'] == 0
    assert usage['_dispatch']['queue_wait_ms'] == 2000
