"""lib/self_update/_version.py — version parsing and release discovery.

``_parse_semver`` / ``current_version`` and the GitHub tags API lookups
``_fetch_latest_release_detailed`` / ``fetch_latest_release``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Optional

import requests

from lib.http_client import http_get
from lib.self_update._config import UPDATE_REPO, _TAGS_URL

from lib.log import get_logger

logger = get_logger(__name__)

#: Minimum interval (seconds) between GitHub tags API attempts once a check
#: has failed. Repeated UI polls / sync threads within this window return the
#: cached failure instead of re-hammering a proxy that 403s api.github.com.
#: Override with ``TOFU_UPDATE_CHECK_MIN_INTERVAL_S`` (0 disables backoff).
_UPDATE_CHECK_MIN_INTERVAL_S = 60.0

_failure_lock = threading.Lock()
_failure_state = {'ts': 0.0, 'error': None}


def _update_backoff_seconds() -> float:
    try:
        value = float(os.environ.get('TOFU_UPDATE_CHECK_MIN_INTERVAL_S', '60'))
    except (TypeError, ValueError) as e:
        logger.debug('[Update] invalid TOFU_UPDATE_CHECK_MIN_INTERVAL_S: %s', e)
        value = 60.0
    return max(0.0, value)


def _cached_failure(now: float):
    """Return ``(cached_error, age)`` when a recent failure is still warm."""
    with _failure_lock:
        error = _failure_state['error']
        if error is None:
            return None, 0.0
        age = now - _failure_state['ts']
        if age < _update_backoff_seconds():
            return dict(error), age
    return None, 0.0


def _record_failure(error: dict) -> None:
    with _failure_lock:
        _failure_state['ts'] = time.monotonic()
        _failure_state['error'] = dict(error)


def _clear_failure() -> None:
    with _failure_lock:
        _failure_state['ts'] = 0.0
        _failure_state['error'] = None


def _is_proxy_tunnel_refusal(exc: BaseException) -> bool:
    """True when the configured proxy refused to CONNECT to api.github.com."""
    if isinstance(exc, requests.exceptions.ProxyError):
        return True
    text = str(exc).lower()
    return 'tunnel connection failed' in text and '403' in text


def _parse_semver(tag: str) -> Optional[tuple]:
    """Parse a 'vX.Y.Z' / 'X.Y.Z' tag into a comparable tuple, or None."""
    m = re.match(r'^v?(\d+)\.(\d+)\.(\d+)', tag.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def current_version() -> str:
    """Current installed version (from the VERSION file via lib.version)."""
    try:
        from lib.version import __version__
        return __version__ or '0.0.0'
    except Exception as e:
        logger.warning('[Update] Could not read current version: %s', e)
        return '0.0.0'


def _fetch_latest_release_detailed() -> tuple:
    """Fetch the newest semver tag, returning ``(payload, error)``.

    On success ``payload`` is ``{'tag': 'v0.9.3', 'version': '0.9.3'}`` for
    the highest semver tag and ``error`` is ``None``. On failure ``payload``
    is ``None`` and ``error`` is a dict ``{'kind', 'detail', 'status'?}``
    that names the CONCRETE cause so the UI can tell the user exactly why
    the check failed instead of a vague "try again later". ``kind`` is one
    of ``network`` (couldn't reach GitHub at all), ``rate_limited``
    (HTTP 403/429), ``http`` (other non-200), ``parse`` (unreadable JSON),
    or ``no_tags`` (repo has no semver tags). Never raises.

    Failure backoff: after a failure the concrete error is cached for
    ``TOFU_UPDATE_CHECK_MIN_INTERVAL_S`` (default 60s) and repeated checks
    within that window return it without touching the network. A proxy that
    refuses the CONNECT to ``api.github.com`` (ProxyError / tunnel 403) is
    retried DIRECTLY once before being treated as a network failure.
    """
    now = time.monotonic()
    cached, age = _cached_failure(now)
    if cached is not None:
        logger.debug('[Update] GitHub tags API check throttled after previous '
                     'failure (%.0fs ago): %s', age, cached.get('detail', ''))
        return None, cached

    try:
        resp = http_get(_TAGS_URL, timeout=15,
                        headers={'Accept': 'application/vnd.github+json'})
    except Exception as e:
        if _is_proxy_tunnel_refusal(e):
            logger.info('[Update] proxy refused GitHub tags API (%s); '
                        'retrying without proxy', str(e)[:200])
            try:
                resp = http_get(_TAGS_URL, timeout=15,
                                headers={'Accept': 'application/vnd.github+json'},
                                use_proxy=False)
            except Exception as e2:
                err = {'kind': 'network', 'detail': str(e2)[:300]}
                _record_failure(err)
                logger.warning('[Update] Failed to reach GitHub tags API: %s', e2)
                return None, err
        else:
            err = {'kind': 'network', 'detail': str(e)[:300]}
            _record_failure(err)
            logger.warning('[Update] Failed to reach GitHub tags API: %s', e)
            return None, err

    if resp.status_code != 200:
        logger.warning('[Update] GitHub tags API returned %s for %s',
                       resp.status_code, UPDATE_REPO)
        kind = 'rate_limited' if resp.status_code in (403, 429) else 'http'
        err = {'kind': kind, 'status': resp.status_code,
               'detail': f'HTTP {resp.status_code} from {_TAGS_URL}'}
        _record_failure(err)
        return None, err

    try:
        tags = resp.json()
    except Exception as e:
        logger.warning('[Update] Could not parse GitHub tags JSON: %s', e)
        err = {'kind': 'parse', 'detail': str(e)[:300]}
        _record_failure(err)
        return None, err

    best_tag = None
    best_ver = None
    for entry in tags or []:
        name = (entry or {}).get('name') or ''
        parsed = _parse_semver(name)
        if parsed is None:
            continue
        if best_ver is None or parsed > best_ver:
            best_ver = parsed
            best_tag = name
    if best_tag is None:
        logger.warning('[Update] No semver tags found for %s', UPDATE_REPO)
        err = {'kind': 'no_tags', 'detail': UPDATE_REPO}
        _record_failure(err)
        return None, err

    _clear_failure()
    return ({'tag': best_tag, 'version': '.'.join(str(p) for p in best_ver)},
            None)


def fetch_latest_release() -> Optional[dict]:
    """Fetch the newest semver tag from the official GitHub repo.

    Returns ``{'tag': 'v0.9.3', 'version': '0.9.3'}`` for the highest
    semver tag, or None on any failure (network, parse, empty list).
    Failures are logged, not raised — the caller degrades gracefully.
    Thin wrapper over :func:`_fetch_latest_release_detailed` that drops the
    error detail (callers that need the reason use the detailed variant).
    """
    payload, _err = _fetch_latest_release_detailed()
    return payload
