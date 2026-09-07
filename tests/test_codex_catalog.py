"""Authenticated Codex `/model` catalogue refresh and fallback tests."""

from __future__ import annotations

import json
import os
import stat
from unittest import mock

import pytest

import lib.oauth.codex_catalog as codex_catalog

pytestmark = pytest.mark.unit


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
    assert url.endswith('/models?client_version=' +
                        codex_catalog.CODEX_CLIENT_VERSION)
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


def test_fetch_revalidates_with_etag_and_accepts_304():
    rows = codex_catalog._normalise_models(_payload())
    cache = {'models': rows, 'etag': 'W/"old"'}
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
        (720, {'ok': False, 'catalog_changed': False}, False, 1440),
        (180, {'ok': False, 'skipped': 'not_authenticated'}, False, 3600),
    ],
)
def test_refresh_interval_adapts_without_exceeding_one_hour(
        current, result, explicit_wake, expected):
    assert codex_catalog._next_refresh_interval(
        current, result, explicit_wake=explicit_wake) == expected


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
