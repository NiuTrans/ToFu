"""Effective Codex CLI client version resolution.

The Codex backend gates its ``/models`` catalogue on ``client_version``: a
stale pinned version hides newly shipped models (0.146.0 hid ``gpt-6-astra``
for an account whose plan included it; 0.153.0 exposes it). The pinned
``CODEX_CLIENT_VERSION`` in ``lib.oauth.outbound`` is therefore only the
floor — the effective version tracks the latest ``@openai/codex`` npm
release, cached on disk with a TTL so request paths never block on the
network. Resolution never returns a version below the pinned floor.
"""

from __future__ import annotations

import re
import threading
import time

from lib.config_dir import config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_NPM_LATEST_URL = 'https://registry.npmjs.org/@openai%2Fcodex/latest'
_FETCH_TIMEOUT_S = 2.5
_CACHE_TTL_S = 24 * 3600
_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')

_lock = threading.Lock()
_memo: dict = {}


def _floor() -> str:
    from lib.oauth.outbound import CODEX_CLIENT_VERSION
    return CODEX_CLIENT_VERSION


def _cache_path() -> str:
    return config_path('oauth', 'codex_client_version.json')


def _parse(version: str) -> tuple:
    return tuple(int(part) for part in str(version).split('.'))


def _fetch_latest_version(*, user_id: str = '') -> str:
    from lib.desktop import egress as _eg

    route = _eg.route_request(_NPM_LATEST_URL, user_id=user_id)
    if route == 'direct':
        from lib.http_client import http_get
        response = http_get(_NPM_LATEST_URL, timeout=_FETCH_TIMEOUT_S)
    else:
        response = _eg.egress_http(
            _NPM_LATEST_URL, method='GET', timeout=_FETCH_TIMEOUT_S,
            user_id=user_id, agent_id=route)
    if response.status_code != 200:
        raise RuntimeError(
            'npm latest-version request failed with HTTP '
            f'{response.status_code}')
    version = str((response.json() or {}).get('version') or '').strip()
    if not _VERSION_RE.match(version):
        raise RuntimeError(f'npm latest-version payload invalid: {version!r}')
    return version


def codex_client_version() -> str:
    """Return the effective client version; never below the pinned floor."""
    floor = _floor()
    now = time.time()
    with _lock:
        memo = dict(_memo)
    if memo and now - memo['at'] < _CACHE_TTL_S:
        return memo['version']
    cached = read_json(_cache_path(), default={}) or {}
    cached_version = str(cached.get('version') or '')
    try:
        fresh = (now - float(cached.get('fetched_at_unix'))) < _CACHE_TTL_S
    except (TypeError, ValueError):
        fresh = False
    if fresh and _VERSION_RE.match(cached_version):
        resolved = max(floor, cached_version, key=_parse)
    else:
        try:
            fetched = _fetch_latest_version()
            write_json_atomic(_cache_path(), {
                'version': fetched,
                'fetched_at_unix': now,
            }, mode=0o600)
            resolved = max(floor, fetched, key=_parse)
        except Exception as exc:
            logger.debug('[CodexVersion] latest-version refresh failed: %s',
                         exc)
            resolved = (max(floor, cached_version, key=_parse)
                        if _VERSION_RE.match(cached_version) else floor)
    with _lock:
        _memo.clear()
        _memo.update({'version': resolved, 'at': now})
    return resolved


def codex_user_agent() -> str:
    """The cloaked User-Agent carrying the resolved (not pinned) version."""
    from lib.oauth.outbound import _CODEX_USER_AGENT, CODEX_CLIENT_VERSION
    resolved = codex_client_version()
    if resolved == CODEX_CLIENT_VERSION:
        return _CODEX_USER_AGENT
    return _CODEX_USER_AGENT.replace(CODEX_CLIENT_VERSION, resolved)


__all__ = ['codex_client_version', 'codex_user_agent']
