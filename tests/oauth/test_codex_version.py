"""tests/oauth/test_codex_version.py — auto-tracking Codex CLI client version.

Pins the 2026-09-06 fix: the backend gates ``/models`` on client_version and
the pinned 0.146.0 hid ``gpt-6-astra`` from an account whose plan served it
(live-verified: same token, 0.146.0 → 8 models, 0.153.0 → +gpt-6-astra). The
effective version now tracks the latest @openai/codex npm release with a
disk cache + TTL, never dropping below the pinned floor.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import lib.oauth.codex_catalog as codex_catalog
import lib.oauth.codex_version as codex_version
from lib.oauth.outbound import _CODEX_USER_AGENT, CODEX_CLIENT_VERSION

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def cache_path(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_version, '_memo', {})
    cache = tmp_path / 'codex_client_version.json'
    monkeypatch.setattr(codex_version, '_cache_path', lambda: str(cache))
    return cache


def _write(path, version, *, age_s=0):
    path.write_text(json.dumps({
        'version': version,
        'fetched_at_unix': time.time() - age_s,
    }))


def test_fetches_latest_version_and_caches_it(cache_path):
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           return_value='0.153.4') as fetch:
        assert codex_version.codex_client_version() == '0.153.4'
        # Second call is served from the in-process memo — no refetch.
        assert codex_version.codex_client_version() == '0.153.4'
    assert fetch.call_count == 1
    cached = json.loads(cache_path.read_text())
    assert cached['version'] == '0.153.4'
    assert stat.S_IMODE(os.stat(cache_path).st_mode) == 0o600


def test_fetch_failure_falls_back_to_pinned_floor(cache_path):
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           side_effect=RuntimeError('offline')):
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION
    assert not cache_path.exists()


def test_fresh_cache_wins_without_network(cache_path):
    _write(cache_path, '0.153.4')
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           side_effect=AssertionError('must not fetch')):
        assert codex_version.codex_client_version() == '0.153.4'


def test_never_returns_below_the_pinned_floor(cache_path):
    _write(cache_path, '0.100.0')
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           side_effect=AssertionError('must not fetch')):
        assert codex_version.codex_client_version() == CODEX_CLIENT_VERSION


def test_stale_cache_survives_refresh_failure(cache_path):
    _write(cache_path, '0.153.4', age_s=codex_version._CACHE_TTL_S + 60)
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           side_effect=RuntimeError('offline')):
        assert codex_version.codex_client_version() == '0.153.4'


def test_stale_cache_is_refreshed(cache_path):
    _write(cache_path, '0.153.0', age_s=codex_version._CACHE_TTL_S + 60)
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           return_value='0.153.4'):
        assert codex_version.codex_client_version() == '0.153.4'
    assert json.loads(cache_path.read_text())['version'] == '0.153.4'


def test_user_agent_carries_the_resolved_version(cache_path):
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           return_value='0.153.4'):
        ua = codex_version.codex_user_agent()
    assert ua == _CODEX_USER_AGENT.replace(CODEX_CLIENT_VERSION, '0.153.4')
    assert CODEX_CLIENT_VERSION not in ua


def test_user_agent_at_floor_matches_cloaking_constant(cache_path):
    with mock.patch.object(codex_version, '_fetch_latest_version',
                           side_effect=RuntimeError('offline')):
        assert codex_version.codex_user_agent() == _CODEX_USER_AGENT


def test_catalog_cache_becomes_stale_across_version_change(tmp_path, cache_path):
    models_cache = tmp_path / 'codex_models_cache.json'
    models_cache.write_text(json.dumps({
        'schema_version': codex_catalog._CACHE_SCHEMA_VERSION,
        'client_version': '0.153.0',
        'fetched_at_unix': time.time(),
        'etag': '',
        'account_fingerprint': '',
        'models': [{'slug': 'gpt-x'}],
    }))
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(models_cache)), \
         mock.patch.object(codex_catalog, 'codex_client_version',
                           return_value='0.153.0'):
        assert codex_catalog._read_cache(current_account=False)['models']
    with mock.patch.object(codex_catalog, '_cache_path',
                           return_value=str(models_cache)), \
         mock.patch.object(codex_catalog, 'codex_client_version',
                           return_value='0.153.4'):
        cached = codex_catalog._read_cache(current_account=False)
        assert cached['models'][0]['slug'] == 'gpt-x'
        assert cached['client_version_stale'] is True
        assert cached['client_version'] == '0.153.0'
