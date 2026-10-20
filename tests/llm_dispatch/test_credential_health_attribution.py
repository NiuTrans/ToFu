"""Credential disabling requires positive evidence across all dispatch lanes.

Uses isolated durable stats and real Slots; transports fail without network.
Legacy telemetry and model/request failures must never poison sibling models.
"""
import asyncio
import importlib
import json
import threading

import pytest

from lib.llm.llm_errors import (
    LocalRequestPreparationError, PermissionError_, RateLimitError, RetryableAPIError,
)

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]
PROVIDER = 'credential-health-test'
KEY = 'key-a'
PAIR = f'{PROVIDER}::{KEY}'


@pytest.fixture
def stats(monkeypatch, tmp_path):
    import lib.key_stats as ks
    from lib.key_stats import _state

    snapshot = dict(ks._cache)
    pending = list(_state._pending_mutations)
    path = tmp_path / 'key_stats.json'
    monkeypatch.setattr(ks, '_STATS_PATH', str(path))
    # A healthy sibling prevents last-resort promotion hiding a false disable.
    monkeypatch.setattr(ks, '_list_siblings',
                        lambda _: [PAIR, f'{PROVIDER}::healthy-sibling'])
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    _state._pending_mutations.clear()
    yield ks, path
    ks._cache.clear()
    ks._cache.update(snapshot)
    _state._pending_mutations[:] = pending


def make_slot(key=KEY):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='synthetic-key', model='test-model',
                provider_id=PROVIDER, capabilities={'text'})


@pytest.mark.parametrize('failure', [
    TypeError('harness adapter got an unexpected keyword argument'),
    RuntimeError('invalid_api_key HTTP 401 in local harness diagnostics'),
    ConnectionError('endpoint DNS unavailable'),
    TimeoutError('stream read timeout'),
    RetryableAPIError('HTTP 500 internal server error'),
    LocalRequestPreparationError('tool schema could not be prepared'),
    PermissionError_('model access denied', status_code=403),
    PermissionError_('region is not supported', status_code=403),
    PermissionError_('missing api key', status_code=401),
    PermissionError_('invalid_api_key'),
    'HTTP 401 invalid api key',
])
def test_unattributed_errors_never_disable_credentials(stats, failure):
    ks, _ = stats
    slot = make_slot()
    for _ in range(ks.MIN_ATTEMPTS + 3):
        slot.record_request()
        slot.record_error(error=failure)
    row = ks.get_today_stats(PROVIDER, KEY)
    assert row['failure'] == ks.MIN_ATTEMPTS + 3
    assert row['credential_failures'] == 0
    assert row['auto_disabled'] is False
    assert row['enabled'] is True
    assert row['last_resort'] is False
    assert ks.is_key_enabled(PROVIDER, KEY, model='another-model') is True
    assert slot.inflight == 0
    assert slot.cooldown_until > 0  # Request-local recovery still exists.


@pytest.mark.parametrize('failure', [
    PermissionError_('credential rejected', status_code=401),
    PermissionError_('invalid_api_key', status_code=403),
    PermissionError_('API key has been revoked', status_code=403),
    PermissionError_('API key has expired', status_code=403),
])
def test_proven_credential_failure_disables_and_retains_reason(stats, failure):
    ks, path = stats
    slot = make_slot()
    for _ in range(ks.MIN_ATTEMPTS):
        slot.record_request()
        slot.record_error(error=failure)
    # An unrelated later error must not become the displayed disable reason.
    slot.record_request()
    slot.record_error(error=TypeError('harness failed later'))
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    row = ks.get_today_stats(PROVIDER, KEY)
    assert row['credential_failures'] == ks.MIN_ATTEMPTS
    assert row['auto_disabled'] is True
    assert row['enabled'] is False
    assert row['last_error'] == str(failure)
    assert ks.get_all_stats()['keys'][PAIR]['last_error'] == str(failure)
    assert ks.is_key_enabled(PROVIDER, KEY, model='another-model') is False
    assert json.loads(path.read_text())['stats'][PAIR]['credential_failures'] == ks.MIN_ATTEMPTS


def test_legacy_failures_never_become_credential_evidence(stats):
    ks, path = stats
    path.write_text(json.dumps({
        'day': ks._today(), 'overrides': {},
        'stats': {PAIR: {'success': 1, 'failure': 999,
                         'last_error': 'harness failed'}},
    }))
    row = ks.get_today_stats(PROVIDER, KEY)
    assert row['failure'] == 999
    assert row['credential_failures'] == 0
    assert row['enabled'] is True
    assert row['auto_disabled'] is False
    assert ks.get_all_stats()['keys'][PAIR]['auto_disabled'] is False
    assert ks.is_key_enabled(PROVIDER, KEY) is True


def test_only_credential_outcomes_reach_the_minimum_sample(stats):
    ks, _ = stats
    for _ in range(20):
        ks.record_outcome(PROVIDER, KEY, False, 'harness error')
    slot = make_slot()
    slot.record_request()
    slot.record_error(error=PermissionError_('revoked', status_code=401))
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    assert ks.get_today_stats(PROVIDER, KEY)['auto_disabled'] is False


def test_successful_requests_still_offset_credential_failures(stats):
    ks, _ = stats
    slot = make_slot()
    for _ in range(5):
        slot.record_request()
        slot.record_success(10)
        slot.record_request()
        slot.record_error(error=PermissionError_('rejected', status_code=401))
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    slot.record_request()
    slot.record_error(error=PermissionError_('rejected', status_code=401))
    assert ks.is_key_enabled(PROVIDER, KEY) is False


def test_overrides_billing_and_rollover_keep_their_authority(stats, monkeypatch):
    ks, _ = stats
    slot = make_slot()
    for _ in range(ks.MIN_ATTEMPTS):
        slot.record_request()
        slot.record_error(error=PermissionError_('revoked', status_code=401))
    assert ks.set_key_override(PROVIDER, KEY, True)['enabled'] is True
    ks.clear_key_override(PROVIDER, KEY)
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    monkeypatch.setattr(ks, '_today', lambda: '2099-01-02')
    assert ks.get_today_stats(PROVIDER, KEY)['credential_failures'] == 0
    assert ks.is_key_enabled(PROVIDER, KEY) is True
    ks.mark_key_exhausted(PROVIDER, KEY, reason='HTTP 402')
    assert ks.is_key_enabled(PROVIDER, KEY) is False
    assert ks.set_key_override(PROVIDER, KEY, True)['enabled'] is True
    ks.set_key_override(PROVIDER, KEY, False)
    monkeypatch.setattr(ks, '_today', lambda: '2099-01-03')
    assert ks.is_key_enabled(PROVIDER, KEY) is False


class Dispatcher:
    """Reservation seam shared by ordinary, streaming, and racing dispatch."""
    def __init__(self, slots):
        self.slots = slots
        self.available = list(slots)

    def pick_and_reserve(self, **kwargs):
        if not self.available:
            return None
        slot = self.available.pop(0)
        slot.record_request()
        return slot

    def pick_top_n(self, **kwargs):
        slots, self.available = self.available, []
        for slot in slots:
            slot.record_request()
        return slots

    def has_capable_slots(self, *args, **kwargs):
        return bool(self.available)

    def summarize_slots(self, *args, **kwargs):
        return 'synthetic candidates'


def test_race_after_child_import_sends_and_settles_each_slot(stats, monkeypatch):
    importlib.import_module('lib.llm.chat')
    from lib.llm import chat
    from lib.llm_dispatch import api

    assert callable(chat)
    ks, _ = stats
    slots = [make_slot(), make_slot('key-b')]
    for slot in slots:
        slot.base_url = 'https://example.invalid/v1'
    monkeypatch.setattr(api, 'get_dispatcher', lambda: Dispatcher(slots))
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **k: 'direct')
    both_sending = threading.Barrier(2, timeout=15)
    requests = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {'choices': [{'message': {'content': 'race succeeded'},
                                 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 3}}

    def post(*args, **kwargs):
        requests.append(json.loads(kwargs['data']))
        both_sending.wait()
        return Response()

    monkeypatch.setattr('lib.http_transport.client.http_post', post)
    messages = [{'role': 'user', 'content': 'test'}]
    text, usage = api.dispatch_fastest(messages, owner_user_id=17)
    assert text == 'race succeeded'
    assert usage['_dispatch']['mode'] == 'race'
    assert len(requests) == 2
    assert all(request['model'] == 'test-model' for request in requests)
    for slot in slots:
        assert slot.inflight == 0
        assert slot.total_errors == 0
        assert ks.get_today_stats(PROVIDER, slot.key_name)['success'] == 1


@pytest.mark.parametrize('lane', ['chat', 'stream', 'async_stream', 'fastest'])
@pytest.mark.parametrize('failure,credential_count', [
    (PermissionError_('revoked credential', status_code=401), 1),
    (PermissionError_('model not allowed', status_code=403), 0),
    (LocalRequestPreparationError('harness adaptation failed'), 0),
    (TypeError('harness unexpected argument'), 0),
])
def test_dispatch_retains_typed_credential_evidence(
        stats, monkeypatch, lane, failure, credential_count):
    from lib.llm_dispatch import api

    ks, _ = stats
    slot = make_slot()
    slots = [slot, make_slot('key-b')] if lane == 'fastest' else [slot]
    dispatcher = Dispatcher(slots)
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)

    def fail(*args, **kwargs):
        raise failure

    async def async_fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr('lib.llm.chat', fail)
    monkeypatch.setattr('lib.llm.stream_chat', fail)
    monkeypatch.setattr('lib.llm.astream.async_stream_chat', async_fail)
    messages = [{'role': 'user', 'content': 'test'}]
    with pytest.raises(type(failure)):
        if lane == 'chat':
            api.dispatch_chat(messages, max_retries=1)
        elif lane == 'stream':
            api.dispatch_stream(messages, max_retries=1)
        elif lane == 'async_stream':
            asyncio.run(api.async_dispatch_stream(messages, max_retries=1))
        else:
            api.dispatch_fastest(messages)
    assert ks.get_today_stats(PROVIDER, KEY)['credential_failures'] == credential_count
    assert all(s.inflight == 0 for s in slots)
    if isinstance(failure, LocalRequestPreparationError):
        assert all(s.total_errors == 0 for s in slots)


@pytest.mark.parametrize('failure,field', [
    (RateLimitError('slow down', status_code=429), 'rate_limited'),
    (RateLimitError('gateway down', is_gateway=True, status_code=503), 'gateway_errors'),
    (RateLimitError('balance exhausted', is_quota=True, status_code=402), 'exhausted'),
    (RateLimitError('model quota', is_quota=True, status_code=429), 'exhausted_models'),
])
def test_race_preserves_rate_gateway_and_billing_scopes(stats, monkeypatch, failure, field):
    from lib.llm_dispatch import api

    ks, _ = stats
    slots = [make_slot(), make_slot('key-b')]
    monkeypatch.setattr(api, 'get_dispatcher', lambda: Dispatcher(slots))

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr('lib.llm.chat', fail)
    with pytest.raises(RateLimitError):
        api.dispatch_fastest([{'role': 'user', 'content': 'test'}])
    for slot in slots:
        row = ks.get_today_stats(PROVIDER, slot.key_name)
        assert row[field]
        assert row['credential_failures'] == 0
        assert row['auto_disabled'] is False
        assert row['exhausted'] is (field == 'exhausted')
        assert slot.inflight == 0
