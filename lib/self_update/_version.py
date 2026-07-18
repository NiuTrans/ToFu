"""lib/self_update/_version.py — version parsing and release discovery.

``_parse_semver`` / ``current_version`` and the GitHub tags API lookups
``_fetch_latest_release_detailed`` / ``fetch_latest_release``.
"""

from __future__ import annotations

import re
from typing import Optional

from lib.http_client import http_get
from lib.self_update._config import UPDATE_REPO, _TAGS_URL

from lib.log import get_logger

logger = get_logger(__name__)

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
    """
    try:
        resp = http_get(_TAGS_URL, timeout=15,
                        headers={'Accept': 'application/vnd.github+json'})
    except Exception as e:
        logger.warning('[Update] Failed to reach GitHub tags API: %s', e)
        return None, {'kind': 'network', 'detail': str(e)[:300]}

    if resp.status_code != 200:
        logger.warning('[Update] GitHub tags API returned %s for %s',
                       resp.status_code, UPDATE_REPO)
        kind = 'rate_limited' if resp.status_code in (403, 429) else 'http'
        return None, {'kind': kind, 'status': resp.status_code,
                      'detail': f'HTTP {resp.status_code} from {_TAGS_URL}'}

    try:
        tags = resp.json()
    except Exception as e:
        logger.warning('[Update] Could not parse GitHub tags JSON: %s', e)
        return None, {'kind': 'parse', 'detail': str(e)[:300]}

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
        return None, {'kind': 'no_tags', 'detail': UPDATE_REPO}

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

