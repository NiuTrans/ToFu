"""tests/test_mcp_prewarm_resilience.py — pre-warm proxy env + negative cache.

Pins the two production fixes for the boot pre-warm storm:

  1. ``_propagate_proxy_env`` carries HTTP(S)_PROXY / ALL_PROXY (and the
     ``TOFU_*`` aliases) into the ``uv`` launcher subprocess env, so the cold
     dependency resolve can reach the package index through the deployment's
     outbound proxy.
  2. The disk-backed negative cache makes a boot that already proved pypi.org
     unreachable fail fast instead of re-running a doomed resolve for every
     vendored server on every boot cycle.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.mcp.client import _vendor as vendor  # noqa: E402

pytestmark = pytest.mark.unit


# ── proxy env propagation ────────────────────────────────────────────

def test_propagate_proxy_env_copies_standard_vars(monkeypatch):
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy:8080')
    monkeypatch.setenv('http_proxy', 'http://proxy-lower:8080')
    monkeypatch.setenv('ALL_PROXY', 'socks5://proxy:1080')

    env = {}
    vendor._propagate_proxy_env(env)

    assert env['HTTPS_PROXY'] == 'http://proxy:8080'
    assert env['http_proxy'] == 'http://proxy-lower:8080'
    assert env['ALL_PROXY'] == 'socks5://proxy:1080'


def test_propagate_proxy_env_tofu_alias(monkeypatch):
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                'ALL_PROXY', 'all_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('TOFU_HTTPS_PROXY', 'http://alias:9090')

    env = {}
    vendor._propagate_proxy_env(env)

    assert env['HTTPS_PROXY'] == 'http://alias:9090'


def test_propagate_proxy_env_does_not_override_existing(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://from-env:8080')

    env = {'HTTPS_PROXY': 'http://from-caller:8080'}
    vendor._propagate_proxy_env(env)

    assert env['HTTPS_PROXY'] == 'http://from-caller:8080'


# ── app proxy-pool fallback ──────────────────────────────────────────

def _clear_proxy_env(monkeypatch):
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy',
                'TOFU_HTTPS_PROXY', 'TOFU_HTTP_PROXY', 'TOFU_ALL_PROXY',
                'TOFU_NO_PROXY'):
        monkeypatch.delenv(key, raising=False)


def test_propagate_proxy_env_pool_fallback(monkeypatch):
    """No env/alias proxy → the app's reachable global pool entry is injected."""
    _clear_proxy_env(monkeypatch)
    import lib.proxy as lib_proxy
    monkeypatch.setattr(lib_proxy, 'first_reachable_global_proxy_url',
                        lambda: 'http://user:pass@pool-proxy:8080')

    env = {}
    vendor._propagate_proxy_env(env)

    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        assert env[key] == 'http://user:pass@pool-proxy:8080'


def test_propagate_proxy_env_pool_never_overrides_env(monkeypatch):
    """An explicit env/alias value always beats the pool fallback."""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv('HTTPS_PROXY', 'http://explicit:8080')
    import lib.proxy as lib_proxy
    monkeypatch.setattr(lib_proxy, 'first_reachable_global_proxy_url',
                        lambda: 'http://pool-proxy:8080')

    env = {}
    vendor._propagate_proxy_env(env)

    assert env['HTTPS_PROXY'] == 'http://explicit:8080'
    assert 'HTTP_PROXY' not in env


def test_propagate_proxy_env_pool_empty(monkeypatch):
    """No reachable pool entry leaves env untouched."""
    _clear_proxy_env(monkeypatch)
    import lib.proxy as lib_proxy
    monkeypatch.setattr(lib_proxy, 'first_reachable_global_proxy_url', lambda: '')

    env = {}
    vendor._propagate_proxy_env(env)

    assert env == {}


# ── negative cache ───────────────────────────────────────────────────

def _reset_cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(tmp_path))


def test_negative_cache_roundtrip(tmp_path, monkeypatch):
    _reset_cache_root(tmp_path, monkeypatch)
    monkeypatch.setenv('TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS', '6')

    assert vendor._prewarm_unreachable_reason() == ''
    vendor._prewarm_note_unreachable('package index unreachable: dns error')
    assert vendor._prewarm_unreachable_reason().startswith('package index unreachable')


def test_negative_cache_expires(tmp_path, monkeypatch):
    _reset_cache_root(tmp_path, monkeypatch)
    monkeypatch.setenv('TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS', '6')

    vendor._prewarm_note_unreachable('stale')
    path = vendor._prewarm_unreachable_marker_path()
    with open(path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    data['ts'] = time.time() - 7 * 3600
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)

    assert vendor._prewarm_unreachable_reason() == ''


def test_negative_cache_disabled_when_zero(tmp_path, monkeypatch):
    _reset_cache_root(tmp_path, monkeypatch)
    monkeypatch.setenv('TOFU_MCP_PREWARM_NEGATIVE_CACHE_HOURS', '0')

    vendor._prewarm_note_unreachable('nope')
    assert vendor._prewarm_unreachable_reason() == ''


def test_is_index_unreachable_matches_dns_only():
    assert vendor._is_index_unreachable(
        'Failed to fetch https://pypi.org/simple/mcp/ dns error: '
        'Name or service not known') is True
    assert vendor._is_index_unreachable(
        'error: distribution mcp not found (some build conflict)') is False
    assert vendor._is_index_unreachable('') is False
