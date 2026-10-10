"""Authenticated Codex `/model` catalogue refresh and fallback tests."""

from __future__ import annotations

import json
import os
import stat
from unittest import mock

import pytest

import lib.oauth.codex_catalog as codex_catalog

import lib.oauth.codex as codex  # Import before patching token_store.load_token.
from lib.oauth.outbound import CODEX_CLIENT_VERSION

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _floor_client_version():
    """Keep version resolution deterministic: no real cache file, no network."""
    with mock.patch.object(codex_catalog, 'codex_client_version',
                           return_value=CODEX_CLIENT_VERSION):
        yield


def _payload():
    return {
        'models': [
            {
                'slug': 'gpt-visible',
                'display_name': 'GPT Visible',
                'description': 'visible row',
                'visibility': 'list',
                'priority': 2,
                'supported_in_api': True,
                'default_reasoning_level': 'medium',
                'supported_reasoning_levels': [
                    {'effort': 'low', 'description': 'fast'},
                    {'effort': 'high', 'description': 'deep'},
                ],
                'input_modalities': ['text', 'image'],
            },
            {
                'slug': 'gpt-hidden',
                'visibility': 'hide',
                'priority': 1,
                'supported_in_api': False,
                'supported_reasoning_levels': [],
                'input_modalities': ['text'],
            },
            # Duplicate slugs and malformed rows must not poison the cache.
            {'slug': 'gpt-visible', 'priority': 0},
            {'display_name': 'missing slug'},
        ],
    }


class _Response:
    status_code = 200
    headers = {'ETag': 'W/"catalog-v1"'}

    def __init__(self, payload):
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


class _NotModifiedResponse:
    status_code = 304
    headers = {}
    content = b''

    def json(self):
        raise AssertionError('304 response body must not be decoded')


def _token():
    return {'access_token': 'stored-token', 'account_id': 'account-1'}


def test_normalises_order_visibility_modalities_and_reasoning():
    rows = codex_catalog._normalise_models(_payload())
    assert [row['slug'] for row in rows] == ['gpt-hidden', 'gpt-visible']
    models = codex_catalog._provider_models(rows)
    hidden, visible = models
    assert hidden['catalog_visibility'] == 'hide'
    assert hidden['capabilities'] == ['text']
    assert visible['catalog_visibility'] == 'list'
    assert visible['capabilities'] == ['text', 'vision', 'thinking']
    assert visible['supported_reasoning_levels'] == ['low', 'high']
    assert visible['thinking_default'] is True


def test_refresh_uses_authenticated_codex_endpoint_and_writes_private_cache(tmp_path):
    cache_path = tmp_path / 'oauth' / 'codex_models_cache.json'
    response = _Response(_payload())
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(cache_path)), \
         mock.patch('lib.oauth.token_store.load_token', return_value=_token()), \
         mock.patch('lib.oauth.outbound.resolve_oauth_request',
                    return_value=('live-token', {
                        'originator': 'codex-tui',
                        'chatgpt-account-id': 'account-1',
                    }, {})), \
         mock.patch('lib.desktop.egress.route_request', return_value='direct'), \
         mock.patch('lib.http_client.http_get', return_value=response) as get, \
         mock.patch.object(codex_catalog, '_provision_from_best_available',
                           return_value=True):
        result = codex_catalog.refresh_codex_model_catalog(force=True)
        projected = codex_catalog.cached_codex_provider_models()

    assert result['ok'] is True
    assert result['changed'] is True
    assert result['catalog_changed'] is True
    assert result['catalog_source'] == 'remote_cache'
    assert [m['model_id'] for m in projected] == ['gpt-hidden', 'gpt-visible']
    url = get.call_args.args[0]
    headers = get.call_args.kwargs['headers']
    assert url.endswith('/models?client_version=' + CODEX_CLIENT_VERSION)
    assert headers['Authorization'] == 'Bearer live-token'
    assert headers['originator'] == 'codex-tui'
    assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600
    cached = json.loads(cache_path.read_text())
    assert cached['etag'] == 'W/"catalog-v1"'
    assert cached['account_fingerprint']
    assert 'access_token' not in cached


def test_successful_200_with_same_normalised_rows_is_not_a_catalog_change(
        tmp_path):
    cache_path = tmp_path / 'codex_models_cache.json'
    rows = codex_catalog._normalise_models(_payload())
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(cache_path)), \
         mock.patch('lib.oauth.token_store.load_token', return_value=_token()):
        codex_catalog._write_cache(
            rows, 'old-etag', codex_catalog._account_fingerprint(_token()))
        with mock.patch.object(
                codex_catalog, '_fetch_catalog',
                return_value=(rows, 'new-etag', False)), \
             mock.patch.object(codex_catalog,
                               '_provision_from_best_available',
                               return_value=False):
            result = codex_catalog.refresh_codex_model_catalog(force=True)

    assert result['ok'] is True
    assert result['not_modified'] is False
    assert result['catalog_changed'] is False


def test_refresh_failure_keeps_last_good_cache(tmp_path):
    cache_path = tmp_path / 'codex_models_cache.json'
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(cache_path)), \
         mock.patch('lib.oauth.token_store.load_token', return_value=_token()):
        rows = codex_catalog._normalise_models(_payload())
        codex_catalog._write_cache(
            rows, 'old-etag', codex_catalog._account_fingerprint(_token()))
        before = cache_path.read_bytes()
        with mock.patch.object(codex_catalog, '_fetch_catalog',
                               side_effect=RuntimeError('upstream down')), \
             mock.patch.object(codex_catalog, '_provision_from_best_available',
                               return_value=False):
            result = codex_catalog.refresh_codex_model_catalog(force=True)
        projected = codex_catalog.cached_codex_provider_models()

    assert result['ok'] is False
    assert result['catalog_changed'] is False
    assert result['catalog_source'] == 'remote_cache'
    assert result['catalog_stale'] is False
    assert 'upstream down' in result['catalog_error']
    assert cache_path.read_bytes() == before
    assert [m['model_id'] for m in projected] == ['gpt-hidden', 'gpt-visible']


def test_catalog_admission_failure_preserves_last_good_and_retries(
        tmp_path, monkeypatch):
    path = tmp_path / 'codex_models_cache.json'
    old = codex_catalog._normalise_models(_payload())
    changed = codex_catalog._normalise_models({
        'models': [{'slug': 'gpt-new', 'input_modalities': ['text'],
                    'supported_reasoning_levels': []}]})
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(path))
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: _token())
    codex_catalog._write_cache(old, 'etag-old',
                               codex_catalog._account_fingerprint(_token()))
    before = path.read_bytes()
    attempts = []

    def provision():
        attempts.append([row['model_id'] for row in
                         codex_catalog.cached_codex_provider_models()])
        if len(attempts) == 1:
            raise RuntimeError('registration admission rejected model')
        return True

    monkeypatch.setattr(codex_catalog, '_provision_from_best_available', provision)
    monkeypatch.setattr(codex_catalog, '_fetch_catalog',
                        lambda *_a, **_kw: (changed, 'etag-new', False))
    result = codex_catalog.refresh_codex_model_catalog(force=True)
    assert result['ok'] is False
    assert path.read_bytes() == before
    assert [m['model_id'] for m in codex_catalog.cached_codex_provider_models()] == [
        'gpt-hidden', 'gpt-visible']
    assert attempts == [['gpt-new']]

    # The fetched rows are retried on the next poll, even if a fresh cache
    # would normally avoid HTTP. They only become visible after admission.
    monkeypatch.setattr(codex_catalog, '_fetch_catalog',
                        lambda *_a, **_kw: pytest.fail('pending catalog must avoid refetch'))
    result = codex_catalog.refresh_codex_model_catalog(force=False)
    assert result['ok'] is True
    assert attempts == [['gpt-new'], ['gpt-new']]
    assert [m['model_id'] for m in codex_catalog.cached_codex_provider_models()] == [
        'gpt-new']


def test_logout_during_catalog_fetch_cannot_publish_or_reprovision(
        tmp_path, monkeypatch):
    path = tmp_path / 'codex_models_cache.json'
    token = _token()
    rows = codex_catalog._normalise_models(_payload())
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(path))
    state = {'token': token}
    monkeypatch.setattr('lib.oauth.token_store.load_token',
                        lambda _name: state['token'])
    codex_catalog._write_cache(rows, 'etag',
                               codex_catalog._account_fingerprint(token))
    before = path.read_bytes()
    provision = mock.Mock(side_effect=AssertionError('must not reprovision'))
    monkeypatch.setattr(codex_catalog, '_provision_from_best_available', provision)

    def logout_during_fetch(*_args, **_kwargs):
        state['token'] = {}
        return rows, 'new-etag', False

    monkeypatch.setattr(codex_catalog, '_fetch_catalog', logout_during_fetch)
    result = codex_catalog.refresh_codex_model_catalog(force=True)
    assert result['skipped'] == 'not_authenticated'
    assert path.read_bytes() == before
    provision.assert_not_called()


def test_fetch_revalidates_with_etag_and_accepts_304():
    rows = codex_catalog._normalise_models(_payload())
    cache = {'models': rows, 'etag': 'W/"old"',
             'client_version': CODEX_CLIENT_VERSION}
    with mock.patch('lib.oauth.outbound.resolve_oauth_request',
                    return_value=('live-token', {}, {})), \
         mock.patch('lib.desktop.egress.route_request', return_value='direct'), \
         mock.patch('lib.http_client.http_get',
                    return_value=_NotModifiedResponse()) as get:
        got, etag, not_modified = codex_catalog._fetch_catalog(cache)

    assert got == rows
    assert etag == 'W/"old"'
    assert not_modified is True
    assert get.call_args.kwargs['headers']['If-None-Match'] == 'W/"old"'


def test_account_change_rejects_previous_accounts_cache(tmp_path):
    cache_path = tmp_path / 'codex_models_cache.json'
    old = {'access_token': 'old', 'account_id': 'account-old'}
    new = {'access_token': 'new', 'account_id': 'account-new'}
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(cache_path)):
        codex_catalog._write_cache(
            codex_catalog._normalise_models(_payload()), 'etag',
            codex_catalog._account_fingerprint(old))
        with mock.patch('lib.oauth.token_store.load_token', return_value=new):
            assert codex_catalog.cached_codex_provider_models() == []
            assert codex_catalog.codex_catalog_status()[
                'catalog_source'] == 'static_fallback'


def test_auth_loss_degrades_to_stale_cache_not_empty(tmp_path):
    """Expired/cleared token (empty current fingerprint) is an outage, not an
    account switch: the last-good catalogue must stay visible as degraded."""
    cache_path = tmp_path / 'codex_models_cache.json'
    good = {'access_token': 'live', 'account_id': 'account-1'}
    dead = {'access_token': '', 'refresh_token': '', 'account_id': 'account-1'}
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(cache_path)):
        codex_catalog._write_cache(
            codex_catalog._normalise_models(_payload()), 'etag',
            codex_catalog._account_fingerprint(good))
        # Token file still names the account but the credential is burned:
        # account_id is present, access_token is gone (expired/cleared).
        with mock.patch('lib.oauth.token_store.load_token', return_value=dead):
            dead_no_id = dict(dead)
            dead_no_id.pop('account_id')
            with mock.patch('lib.oauth.token_store.load_token',
                            return_value=dead_no_id):
                projected = codex_catalog.cached_codex_provider_models()
                status = codex_catalog.codex_catalog_status()
    assert [m['model_id'] for m in projected] == ['gpt-hidden', 'gpt-visible']
    assert status['catalog_source'] == 'remote_cache'


def test_empty_upstream_catalogue_is_rejected():
    with pytest.raises(ValueError, match='empty'):
        codex_catalog._normalise_models({'models': []})


@pytest.mark.parametrize(
    ('current', 'result', 'explicit_wake', 'expected'),
    [
        (180, {'ok': True, 'catalog_changed': False}, False, 360),
        (360, {'ok': True, 'catalog_changed': False}, False, 720),
        (720, {'ok': True, 'catalog_changed': False}, False, 1440),
        (1440, {'ok': True, 'catalog_changed': False}, False, 2880),
        (2880, {'ok': True, 'catalog_changed': False}, False, 3600),
        (3600, {'ok': True, 'catalog_changed': False}, False, 3600),
        (3600, {'ok': True, 'catalog_changed': True}, False, 180),
        (3600, {'ok': True, 'catalog_changed': False}, True, 180),
        (720, {'ok': False, 'catalog_changed': False}, False, 900),
        (3600, {'ok': False, 'catalog_changed': False}, False, 900),
        (180, {'ok': False, 'catalog_changed': False}, False, 360),
        (180, {'ok': False, 'skipped': 'not_authenticated'}, False, 3600),
    ],
)
def test_refresh_interval_adapts_without_exceeding_one_hour(
        current, result, explicit_wake, expected):
    assert codex_catalog._next_refresh_interval(
        current, result, explicit_wake=explicit_wake) == expected


def test_cached_catalog_poll_proactively_refreshes_codex_token(
        tmp_path, monkeypatch):
    cache_path = tmp_path / 'codex_models_cache.json'
    token = {'access_token': 'old', 'refresh_token': 'rt',
             'expire': 1_000_200, 'account_id': 'account-1'}
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(cache_path))
    monkeypatch.setattr(codex_catalog.time, 'time', lambda: 1_000_000)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: token)
    codex_catalog._write_cache(codex_catalog._normalise_models(_payload()),
                               'etag', codex_catalog._account_fingerprint(token))
    refreshed = []
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token',
                        lambda **kw: refreshed.append(kw) or 'new')
    monkeypatch.setattr(codex_catalog, '_provision_from_best_available',
                        lambda: False)
    monkeypatch.setattr(codex_catalog, '_fetch_catalog',
                        lambda *_a, **_kw: pytest.fail('fresh cache must skip HTTP'))

    result = codex_catalog.refresh_codex_model_catalog()
    assert result['ok'] is True
    assert refreshed == [{'user_id': ''}]


def test_idle_catalog_poll_never_retries_terminal_refresh(
        tmp_path, monkeypatch):
    cache_path = tmp_path / 'codex_models_cache.json'
    token = {'access_token': 'old', 'refresh_token': '',
             'refresh_invalidated_at': 999_999, 'expire': 1_000_200}
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(cache_path))
    monkeypatch.setattr(codex_catalog.time, 'time', lambda: 1_000_000)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: token)
    codex_catalog._write_cache(codex_catalog._normalise_models(_payload()),
                               'etag', codex_catalog._account_fingerprint(token))
    monkeypatch.setattr('lib.oauth.codex.codex_get_valid_token',
                        lambda **kw: pytest.fail('terminal refresh replayed'))
    monkeypatch.setattr(codex_catalog, '_provision_from_best_available',
                        lambda: False)
    assert codex_catalog.refresh_codex_model_catalog()['ok'] is True


def test_catalog_poll_wakes_before_codex_access_token_expires(monkeypatch):
    # Stable model-catalog responses can back off beyond the token lifetime.
    now = 1_000_000.0
    monkeypatch.setattr(codex_catalog.time, 'time', lambda: now)
    monkeypatch.setattr('lib.oauth.token_store.load_token',
                        lambda _name: {'access_token': 'tok',
                                       'refresh_token': 'refresh',
                                       'expire': now + 1200})
    assert codex_catalog._token_refresh_poll_interval(3600) == 900
    assert codex_catalog._token_refresh_poll_interval(360) == 360

    monkeypatch.setattr('lib.oauth.token_store.load_token',
                        lambda _name: {'access_token': 'tok',
                                       'refresh_token': 'refresh',
                                       'expire': now + 100})
    assert codex_catalog._token_refresh_poll_interval(3600) == 180


@pytest.mark.parametrize('token', [
    {},
    {'access_token': 'tok', 'expire': 1_001_200},
    {'access_token': 'tok', 'refresh_token': 'refresh',
     'refresh_invalidated_at': 999_999, 'expire': 1_001_200},
])
def test_catalog_poll_does_not_spin_without_usable_refresh(monkeypatch, token):
    monkeypatch.setattr(codex_catalog.time, 'time', lambda: 1_000_000)
    monkeypatch.setattr('lib.oauth.token_store.load_token',
                        lambda _name: token)
    assert codex_catalog._token_refresh_poll_interval(3600) == 3600


def test_worker_applies_refresh_deadline_to_first_poll(monkeypatch):
    monkeypatch.setattr(codex_catalog, 'refresh_codex_model_catalog',
                        lambda **_kwargs: {'ok': True, 'catalog_changed': False})
    monkeypatch.setattr(codex_catalog, '_token_refresh_poll_interval',
                        lambda interval: min(interval, 17))
    waits = []
    monkeypatch.setattr(codex_catalog._refresh_wake, 'wait',
                        lambda seconds: waits.append(seconds) or False)
    monkeypatch.setattr(codex_catalog._refresh_wake, 'clear', lambda: None)
    monkeypatch.setattr(codex_catalog._worker_stop, 'is_set',
                        lambda: bool(waits))
    codex_catalog._worker_loop()
    assert waits == [17]


def test_unauthenticated_start_keeps_catalog_worker_absent(monkeypatch):
    codex_catalog.stop_codex_catalog_refresher(timeout=0)
    monkeypatch.setattr(codex_catalog, '_worker_thread', None)
    monkeypatch.setattr(codex_catalog, '_worker_started', False)
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: {})
    thread = mock.Mock(side_effect=AssertionError('must remain thread-free'))
    monkeypatch.setattr(codex_catalog.threading, 'Thread', thread)

    assert codex_catalog.start_codex_catalog_refresher() is False
    assert codex_catalog._worker_thread is None
    assert codex_catalog._worker_started is False
    thread.assert_not_called()


def test_worker_researches_discovery_then_throttles_unchanged_catalog(monkeypatch):
    from lib.model_routing import OwnerBoundary
    boundary = OwnerBoundary.create(521)
    refreshes, waits, researches = [], [], []
    results = [{'ok': True, 'catalog_changed': False},
               {'ok': True, 'catalog_changed': False},
               {'ok': True, 'catalog_changed': True},
               {'ok': False, 'catalog_changed': True}]
    def refresh(**kwargs):
        refreshes.append(kwargs)
        return results[len(refreshes)-1]
    monkeypatch.setattr(codex_catalog, 'refresh_codex_model_catalog', refresh)
    monkeypatch.setattr(codex_catalog, '_research_registrations',
                        lambda owner: researches.append(owner) or {'status': 'ok'})
    monkeypatch.setattr(codex_catalog.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(codex_catalog, '_token_refresh_poll_interval', lambda interval: interval)
    monkeypatch.setattr(codex_catalog._refresh_wake, 'wait', lambda seconds: waits.append(seconds) or False)
    monkeypatch.setattr(codex_catalog._refresh_wake, 'clear', lambda: None)
    monkeypatch.setattr(codex_catalog._worker_stop, 'is_set', lambda: len(refreshes) == 4)
    codex_catalog._worker_loop(boundary=boundary)
    assert researches == [boundary, boundary]
    assert len(waits) == 3


def test_background_research_carries_owner_and_cancels_on_account_change(monkeypatch):
    from lib.model_catalog import registration_research
    from lib.model_routing import OwnerBoundary
    boundary = OwnerBoundary.create(522, tenant_id='research-test')
    fingerprint = ['account-one']
    monkeypatch.setattr(codex_catalog, '_account_fingerprint', lambda: fingerprint[0])
    monkeypatch.setattr(codex_catalog._worker_stop, 'is_set', lambda: False)
    monkeypatch.setattr(codex_catalog.time, 'monotonic', lambda: 10.0)
    def research(repository, owner, **kwargs):
        assert owner == boundary and kwargs['provider_id'] == 'oauth_codex'
        assert kwargs['deadline'] == 10.0 + codex_catalog.CODEX_REGISTRATION_MAX_RUNTIME_S
        assert not codex_catalog._refresh_lock.locked()
        assert kwargs['cancelled']() is False
        fingerprint[0] = 'account-two'
        assert kwargs['cancelled']() is True
        return {'status': 'ok', 'ready': 0}
    monkeypatch.setattr(registration_research, 'run_registration_research', research)
    assert codex_catalog._research_registrations(boundary)['status'] == 'ok'


def test_research_failure_does_not_terminate_catalog_polling(monkeypatch):
    from lib.model_catalog import registration_research
    from lib.model_routing import OwnerBoundary
    monkeypatch.setattr(codex_catalog, '_account_fingerprint', lambda: 'account-one')
    monkeypatch.setattr(codex_catalog._worker_stop, 'is_set', lambda: False)
    def unavailable(*args, **kwargs):
        raise RuntimeError('research service unavailable')
    monkeypatch.setattr(registration_research, 'run_registration_research', unavailable)
    assert codex_catalog._research_registrations(OwnerBoundary.create(523)) == {'status': 'unavailable'}


@pytest.mark.parametrize('context', [None, 16384])
def test_subscription_context_keeps_evidence_and_absence(context):
    payload = _payload()
    if context is not None:
        payload['models'][0]['context_window'] = context
    rows = codex_catalog._normalise_models(payload)
    models = codex_catalog._provider_models(rows)
    model = next(m for m in models if m['model_id'] == 'gpt-visible')
    if context is None:
        assert 'context_window' not in model
        assert 'context_window' in model['inherited_model_fields']
    else:
        assert model['context_window'] == context
        assert 'context_window' not in model['inherited_model_fields']


@pytest.mark.parametrize('refresh_succeeds', [False, True])
def test_client_upgrade_retains_capabilities_and_forces_revalidation(
        tmp_path, monkeypatch, refresh_succeeds):
    path = tmp_path / 'catalog.json'
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(path))
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda _name: _token())
    rows = codex_catalog._normalise_models(_payload())
    monkeypatch.setattr(codex_catalog, 'codex_client_version', lambda: '1.0.0')
    codex_catalog._write_cache(rows, 'old-version-etag',
                               codex_catalog._account_fingerprint(_token()))
    before = path.read_bytes()
    monkeypatch.setattr(codex_catalog, 'codex_client_version', lambda: '1.1.0')
    assert codex_catalog.codex_catalog_status()['catalog_stale'] is True
    observed = []

    def provision():
        observed.extend(codex_catalog.cached_codex_provider_models())
        return False

    monkeypatch.setattr(codex_catalog, '_provision_from_best_available', provision)
    with mock.patch.object(codex_catalog, '_fetch_catalog',
                           return_value=(rows, 'new-version-etag', False),
                           side_effect=None if refresh_succeeds else RuntimeError('offline')) as fetch:
        result = codex_catalog.refresh_codex_model_catalog()
    fetch.assert_called_once()  # Version change bypasses a still-fresh TTL.
    assert result['ok'] is refresh_succeeds
    assert result['catalog_source'] == 'remote_cache'
    assert result['catalog_stale'] is not refresh_succeeds
    assert next(m for m in observed if m['model_id'] == 'gpt-visible')[
        'capabilities'] == ['text', 'vision', 'thinking']
    if refresh_succeeds:
        assert json.loads(path.read_text())['client_version'] == '1.1.0'
    else:
        assert path.read_bytes() == before


@pytest.mark.parametrize('status', [200, 304])
def test_client_upgrade_does_not_reuse_old_version_etag(status):
    rows = codex_catalog._normalise_models(_payload())
    cache = {'models': rows, 'etag': 'old-version-etag', 'client_version': '0.0.1'}
    response = _Response(_payload()) if status == 200 else _NotModifiedResponse()
    with mock.patch('lib.oauth.outbound.resolve_oauth_request',
                    return_value=('live-token', {}, {})), \
         mock.patch('lib.desktop.egress.route_request', return_value='direct'), \
         mock.patch('lib.http_client.http_get', return_value=response) as get:
        if status == 304:
            with pytest.raises(RuntimeError, match='matching client-version cache'):
                codex_catalog._fetch_catalog(cache)
        else:
            assert codex_catalog._fetch_catalog(cache)[2] is False
    assert 'If-None-Match' not in get.call_args.kwargs['headers']
