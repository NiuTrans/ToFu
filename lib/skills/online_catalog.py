"""Bounded, on-demand discovery and verified installation from ClawHub.

Nothing in this module participates in the resident task prompt. A short
capability query reaches the public registry only after an explicit search;
results are compact routing metadata and are cached in bounded, reclaimable
memory. Installation re-verifies an exact version and its complete file
manifest before the existing atomic package installer is allowed to activate
any bytes.

Public entry points:
  * ``search_online_skills`` — live search for UI/model discovery.
  * ``install_clawhub_skill`` — exact-version, clean/pass install transaction.
  * ``parse_clawhub_catalog_id`` — validate the stable registry identity.
"""

from __future__ import annotations

import atexit
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import io
import json
import os
from pathlib import PurePosixPath
import re
import threading
from typing import Any, Callable
import unicodedata
from urllib.parse import urlsplit

import requests

from lib.http_client import http_get
from lib.identity import require_user_id
from lib.log import get_logger
from lib.skills.installer import InstallerError, install_skill_package
from lib.ttl_cache import TTLCache

logger = get_logger(__name__)

_PROVIDER = 'clawhub'
_BASE_URL = 'https://clawhub.ai'
_SEARCH_URL = f'{_BASE_URL}/api/v1/search'
_VERIFY_URL_TEMPLATE = f'{_BASE_URL}/api/v1/skills/{{slug}}/verify'
_DOWNLOAD_URL = f'{_BASE_URL}/api/v1/download'
_ALLOWED_REGISTRY_HOSTS = frozenset({'clawhub.ai'})
_ALLOWED_ARCHIVE_HOSTS = frozenset({'codeload.github.com'})

_QUERY_MAX_CHARS = 160
_RESULT_LIMIT_DEFAULT = 5
_RESULT_LIMIT_MAX = 8
_RAW_SEARCH_LIMIT = 12
_SEARCH_JSON_MAX_BYTES = 512 * 1024
_VERIFY_JSON_MAX_BYTES = 2 * 1024 * 1024
_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
_SEARCH_TIMEOUT_SECONDS = 8
_VERIFY_TIMEOUT_SECONDS = 6
_DOWNLOAD_TIMEOUT_SECONDS = 60
_MAX_MANIFEST_FILES = 2_000
_MAX_PACKAGE_BYTES = 25 * 1024 * 1024

_OWNER_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,95}$')
_VERSION_RE = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$')
_REPO_RE = re.compile(
    r'^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$')
_HEX_40_RE = re.compile(r'^[0-9a-f]{40}$')
_HEX_64_RE = re.compile(r'^[0-9a-f]{64}$')

_SEARCH_CACHE = TTLCache(
    300, max_size=64, name='skills_clawhub_search')
_VERIFY_CACHE = TTLCache(
    300, max_size=256, name='skills_clawhub_verify')
_FAILURE_CACHE = TTLCache(
    30, max_size=32, name='skills_clawhub_failures')

_VERIFY_WORKERS = 4
_executor_lock = threading.Lock()
_verify_executor: ThreadPoolExecutor | None = None
_verify_batches = 0


class OnlineCatalogError(InstallerError):
    """Typed public-registry failure with a safe user-facing code."""

    def __init__(self, message: str, *, code: str,
                 http_status: int = 502, retry_after: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after


@contextmanager
def _executor_batch():
    """Lease one shared executor generation for a complete search batch."""
    global _verify_batches, _verify_executor
    with _executor_lock:
        if _verify_executor is None:
            _verify_executor = ThreadPoolExecutor(
                max_workers=_VERIFY_WORKERS,
                thread_name_prefix='skill-online')
        executor = _verify_executor
        _verify_batches += 1
    try:
        yield executor
    finally:
        with _executor_lock:
            _verify_batches = max(0, _verify_batches - 1)
            if _verify_batches == 0 and _verify_executor is executor:
                # Do not publish a replacement while exceptional leftovers
                # from this bounded generation are still draining.
                executor.shutdown(wait=True, cancel_futures=True)
                _verify_executor = None


def online_catalog_executor_snapshot() -> dict[str, bool | int]:
    """Return verification concurrency and resident-thread diagnostics."""
    with _executor_lock:
        executor = _verify_executor
        threads = tuple(getattr(executor, '_threads', ()))
        return {
            'maxWorkers': _VERIFY_WORKERS,
            'activeBatches': _verify_batches,
            'executorActive': executor is not None,
            'residentThreads': sum(thread.is_alive() for thread in threads),
        }


def close_online_catalog() -> None:
    """Release the lazily created verification pool (shutdown/test seam)."""
    global _verify_batches, _verify_executor
    with _executor_lock:
        executor, _verify_executor = _verify_executor, None
        _verify_batches = 0
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)


atexit.register(close_online_catalog)


def _online_enabled() -> bool:
    value = os.environ.get('TOFU_SKILLS_ONLINE_DISCOVERY', '1')
    return str(value).strip().lower() not in {'0', 'false', 'no', 'off'}


def _text(value: object, *, max_chars: int) -> str:
    raw = unicodedata.normalize('NFKC', str(value or ''))
    cleaned = ''.join(
        char if char in '\n\t' or ord(char) >= 32 else ' '
        for char in raw)
    return ' '.join(cleaned.split())[:max_chars]


def _query(value: object) -> str:
    return _text(value, max_chars=_QUERY_MAX_CHARS).lower()


def _limit(value: object) -> int:
    try:
        parsed = int(str(value)[:16])
    except (TypeError, ValueError):
        parsed = _RESULT_LIMIT_DEFAULT
    return max(1, min(parsed, _RESULT_LIMIT_MAX))


def _approved_url(url: str, hosts: frozenset[str]) -> bool:
    try:
        parsed = urlsplit(str(url or ''))
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == 'https'
        and parsed.hostname in hosts
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def _retry_after(response: Any) -> int | None:
    headers = getattr(response, 'headers', None) or {}
    try:
        value = int(str(headers.get('Retry-After') or '')[:16])
    except (TypeError, ValueError):
        return None
    return max(1, min(value, 3_600))


def _response_bytes(response: Any, *, max_bytes: int) -> bytes:
    headers = getattr(response, 'headers', None) or {}
    declared = headers.get('Content-Length')
    if declared:
        try:
            if int(declared) > max_bytes:
                raise OnlineCatalogError(
                    'Online skill response exceeded the product byte budget.',
                    code='online_response_too_large', http_status=413)
        except ValueError:
            logger.debug('[SkillsOnline] invalid Content-Length: %r', declared)
    buffer = io.BytesIO()
    total = 0
    iterator = getattr(response, 'iter_content', None)
    if callable(iterator):
        chunks = iterator(chunk_size=64 * 1024)
    else:
        chunks = (bytes(getattr(response, 'content', b'') or b''),)
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            raise OnlineCatalogError(
                'Online skill response exceeded the product byte budget.',
                code='online_response_too_large', http_status=413)
        buffer.write(chunk)
    if total == 0:
        raise OnlineCatalogError(
            'The online skill registry returned an empty response.',
            code='online_empty_response', http_status=502)
    return buffer.getvalue()


def _fetch_bytes(
    url: str,
    *,
    params: dict[str, object] | None,
    getter: Callable[..., Any],
    allowed_hosts: frozenset[str],
    timeout: int,
    max_bytes: int,
    accept: str,
) -> tuple[bytes, str]:
    if not _approved_url(url, allowed_hosts):
        raise OnlineCatalogError(
            'Online skill source URL was rejected.',
            code='online_source_rejected', http_status=409)
    response = None
    try:
        response = getter(
            url, params=params, timeout=timeout, stream=True,
            headers={'Accept': accept})
        status = int(getattr(response, 'status_code', 200) or 200)
        if status == 429:
            raise OnlineCatalogError(
                'The online skill registry is rate-limited; try again later.',
                code='online_rate_limited', http_status=503,
                retry_after=_retry_after(response))
        if status in (401, 403):
            raise OnlineCatalogError(
                'The selected online skill is not publicly installable.',
                code='online_access_blocked', http_status=409)
        if status in (404, 410):
            raise OnlineCatalogError(
                'The selected online skill release is no longer available.',
                code='online_release_not_found', http_status=404)
        response.raise_for_status()
        final_url = str(getattr(response, 'url', '') or url)
        if not _approved_url(final_url, allowed_hosts):
            raise OnlineCatalogError(
                'Online skill download redirected outside its trusted host.',
                code='online_redirect_rejected', http_status=502)
        data = _response_bytes(response, max_bytes=max_bytes)
        content_type = str(
            (getattr(response, 'headers', None) or {}).get(
                'Content-Type') or '').split(';', 1)[0].strip().lower()
        return data, content_type
    except OnlineCatalogError:
        raise
    except requests.RequestException as exc:
        raise OnlineCatalogError(
            'The online skill registry is temporarily unavailable.',
            code='online_unavailable', http_status=502) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise OnlineCatalogError(
            'The online skill registry returned an invalid response.',
            code='online_invalid_response', http_status=502) from exc
    finally:
        close = getattr(response, 'close', None)
        if callable(close):
            close()


def _fetch_json(
    url: str,
    *,
    params: dict[str, object] | None,
    getter: Callable[..., Any],
    max_bytes: int,
    timeout: int,
) -> dict[str, Any]:
    data, _content_type = _fetch_bytes(
        url, params=params, getter=getter,
        allowed_hosts=_ALLOWED_REGISTRY_HOSTS, timeout=timeout,
        max_bytes=max_bytes, accept='application/json')
    try:
        parsed = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise OnlineCatalogError(
            'The online skill registry returned invalid JSON.',
            code='online_invalid_json', http_status=502) from exc
    if not isinstance(parsed, dict):
        raise OnlineCatalogError(
            'The online skill registry returned an invalid object.',
            code='online_invalid_json', http_status=502)
    return parsed


def build_clawhub_catalog_id(owner: str, slug: str) -> str:
    normalized_owner = str(owner or '').strip().lower()
    normalized_slug = str(slug or '').strip().lower()
    if (not _OWNER_RE.fullmatch(normalized_owner)
            or not _SLUG_RE.fullmatch(normalized_slug)):
        raise OnlineCatalogError(
            'ClawHub returned an invalid publisher or skill identifier.',
            code='online_invalid_identity', http_status=502)
    value = f'clawhub.{normalized_owner}.{normalized_slug}'
    if len(value) > 128:
        raise OnlineCatalogError(
            'ClawHub skill identifier exceeds the product limit.',
            code='online_invalid_identity', http_status=502)
    return value


def parse_clawhub_catalog_id(catalog_id: str) -> tuple[str, str] | None:
    value = str(catalog_id or '').strip().lower()
    if not value.startswith('clawhub.'):
        return None
    parts = value.split('.')
    if len(parts) != 3:
        raise OnlineCatalogError(
            'Invalid ClawHub catalog id.',
            code='online_invalid_identity', http_status=400)
    _prefix, owner, slug = parts
    build_clawhub_catalog_id(owner, slug)
    return owner, slug


def _canonical_page_url(value: object) -> str:
    raw = _text(value, max_chars=2_048)
    if raw.startswith('/'):
        raw = f'{_BASE_URL}{raw}'
    return raw if _approved_url(raw, _ALLOWED_REGISTRY_HOSTS) else ''


def _normalize_search_row(row: object) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    owner_obj = row.get('owner') if isinstance(row.get('owner'), dict) else {}
    publisher = (
        row.get('publisher') if isinstance(row.get('publisher'), dict) else {})
    native = row.get('native') if isinstance(row.get('native'), dict) else {}
    native_skill = (
        native.get('skill') if isinstance(native.get('skill'), dict) else {})
    stats = row.get('stats') if isinstance(row.get('stats'), dict) else {}
    if not stats and isinstance(native_skill.get('stats'), dict):
        stats = native_skill['stats']
    owner = _text(
        row.get('ownerHandle') or owner_obj.get('handle')
        or publisher.get('handle') or native.get('ownerHandle'),
        max_chars=64).lower()
    slug = _text(row.get('slug'), max_chars=96).lower()
    try:
        catalog_id = build_clawhub_catalog_id(owner, slug)
    except OnlineCatalogError:
        return None
    topics = row.get('topics')
    if not isinstance(topics, list):
        topics = native_skill.get('topics')
    tags = [
        _text(value, max_chars=64) for value in (topics or ())[:12]
        if _text(value, max_chars=64)
    ]
    try:
        downloads = max(0, min(
            int(row.get('downloads') or stats.get('downloads') or 0),
            2_147_483_647))
    except (TypeError, ValueError):
        downloads = 0
    version = _text(row.get('version'), max_chars=64)
    if version and not _VERSION_RE.fullmatch(version):
        version = ''
    author = _text(
        publisher.get('displayName') or owner_obj.get('displayName') or owner,
        max_chars=120)
    official = bool(row.get('official') or publisher.get('official'))
    return {
        'id': catalog_id,
        'catalog_id': catalog_id,
        'name': _text(
            row.get('displayName') or native_skill.get('displayName') or slug,
            max_chars=160),
        'description': _text(
            row.get('summary') or row.get('description')
            or native_skill.get('summary'), max_chars=400),
        'tags': tags,
        'author': author,
        'publisher': owner,
        'category': 'ClawHub',
        'homepage': _canonical_page_url(
            row.get('canonicalUrl')
            or (row.get('links') or {}).get('canonical')
            if isinstance(row.get('links'), dict) else row.get('canonicalUrl')),
        'featured': bool(row.get('featured') or official),
        'official': official,
        'downloads': downloads,
        'source': _PROVIDER,
        'source_revision': version,
        'verified': False,
        'installable': False,
        'unavailable_reason': 'Awaiting exact ClawHub verification.',
    }


def _fetch_search_candidates(
    query: str,
    *,
    getter: Callable[..., Any],
) -> list[dict[str, Any]]:
    payload = _fetch_json(
        _SEARCH_URL,
        params={
            'q': query,
            'limit': _RAW_SEARCH_LIMIT,
            'nonSuspiciousOnly': 'true',
        },
        getter=getter, max_bytes=_SEARCH_JSON_MAX_BYTES,
        timeout=_SEARCH_TIMEOUT_SECONDS)
    rows = payload.get('results')
    if not isinstance(rows, list):
        raise OnlineCatalogError(
            'ClawHub search returned an invalid result list.',
            code='online_invalid_response', http_status=502)
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows[:_RAW_SEARCH_LIMIT]:
        candidate = _normalize_search_row(row)
        if not candidate or candidate['catalog_id'] in seen:
            continue
        seen.add(candidate['catalog_id'])
        normalized.append(candidate)
    return normalized


def _verification_envelope(
    owner: str,
    slug: str,
    *,
    version: str | None,
    getter: Callable[..., Any],
) -> dict[str, Any]:
    params: dict[str, object] = {'ownerHandle': owner}
    if version:
        if not _VERSION_RE.fullmatch(version):
            raise OnlineCatalogError(
                'Invalid online skill version.',
                code='online_invalid_revision', http_status=400)
        params['version'] = version
    else:
        params['tag'] = 'latest'
    payload = _fetch_json(
        _VERIFY_URL_TEMPLATE.format(slug=slug),
        params=params, getter=getter, max_bytes=_VERIFY_JSON_MAX_BYTES,
        timeout=_VERIFY_TIMEOUT_SECONDS)
    returned_owner = _text(payload.get('publisherHandle'), max_chars=64).lower()
    returned_slug = _text(payload.get('slug'), max_chars=96).lower()
    returned_version = _text(payload.get('version'), max_chars=64)
    if (returned_owner != owner or returned_slug != slug
            or not _VERSION_RE.fullmatch(returned_version)
            or (version is not None and returned_version != version)):
        raise OnlineCatalogError(
            'ClawHub verification identity did not match the request.',
            code='online_verification_mismatch', http_status=409)
    security = payload.get('security')
    if not isinstance(security, dict):
        security = {}
    if not (
        payload.get('ok') is True
        and payload.get('decision') == 'pass'
        and security.get('status') == 'clean'
        and security.get('passed') is True
    ):
        raise OnlineCatalogError(
            'ClawHub has not produced a clean, installable verification for '
            'this exact release.',
            code='online_verification_not_clean', http_status=409)
    return payload


def _verified_candidate(
    candidate: dict[str, Any],
    *,
    getter: Callable[..., Any],
    use_cache: bool,
) -> dict[str, Any]:
    owner = str(candidate['publisher'])
    slug = str(candidate['catalog_id']).rsplit('.', 1)[-1]
    requested_version = str(candidate.get('source_revision') or '') or None
    cache_key = (owner, slug, requested_version or 'latest')
    sentinel = object()
    if use_cache:
        cached = _VERIFY_CACHE.get(cache_key, sentinel)
        if cached is not sentinel:
            return deepcopy(cached)
    result = dict(candidate)
    try:
        envelope = _verification_envelope(
            owner, slug, version=requested_version, getter=getter)
        version = str(envelope['version'])
        result.update({
            'catalog_id': build_clawhub_catalog_id(owner, slug),
            'id': build_clawhub_catalog_id(owner, slug),
            'source_revision': version,
            'verified': True,
            'installable': True,
            'unavailable_reason': '',
            'homepage': _canonical_page_url(envelope.get('pageUrl'))
            or result.get('homepage', ''),
            'author': _text(
                envelope.get('publisherDisplayName') or result.get('author'),
                max_chars=120),
            'signed': (
                isinstance(envelope.get('signature'), dict)
                and envelope['signature'].get('status') not in (None, 'unsigned')),
        })
    except OnlineCatalogError as exc:
        result.update({
            'source_revision': '',
            'verified': False,
            'installable': False,
            'unavailable_reason': str(exc),
        })
    if use_cache:
        _VERIFY_CACHE.set(cache_key, deepcopy(result))
    return result


def search_online_skills(
    query: str,
    *,
    limit: int = _RESULT_LIMIT_DEFAULT,
    include_unverified: bool = True,
    http_get_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Search ClawHub only when called; return compact normalized metadata."""
    normalized_query = _query(query)
    result_limit = _limit(limit)
    if not normalized_query:
        return {
            'catalog': [],
            'online': {
                'provider': _PROVIDER, 'attempted': False, 'ok': True,
                'cached': False, 'query': '', 'verified_count': 0,
            },
        }
    if not _online_enabled():
        return {
            'catalog': [],
            'online': {
                'provider': _PROVIDER, 'attempted': False, 'ok': False,
                'cached': False, 'query': normalized_query,
                'error': 'online_discovery_disabled', 'verified_count': 0,
            },
        }

    getter = http_get_fn or http_get
    use_cache = http_get_fn is None
    cache_hit = False
    failure_key = normalized_query
    if use_cache:
        recent_failure = _FAILURE_CACHE.get(failure_key)
        if recent_failure:
            return {
                'catalog': [],
                'online': dict(recent_failure, cached=True),
            }
    try:
        sentinel = object()
        candidates = (
            _SEARCH_CACHE.get(normalized_query, sentinel)
            if use_cache else sentinel)
        if candidates is sentinel:
            candidates = _fetch_search_candidates(
                normalized_query, getter=getter)
            if use_cache:
                _SEARCH_CACHE.set(normalized_query, deepcopy(candidates))
        else:
            cache_hit = True
            candidates = deepcopy(candidates)

        verify_rows = candidates[:min(
            len(candidates), max(result_limit * 2, result_limit))]
        if use_cache and len(verify_rows) > 1:
            with _executor_batch() as executor:
                verified_rows = list(executor.map(
                    lambda row: _verified_candidate(
                        row, getter=getter, use_cache=True),
                    verify_rows))
        else:
            verified_rows = [
                _verified_candidate(
                    row, getter=getter, use_cache=use_cache)
                for row in verify_rows
            ]
        if not include_unverified:
            verified_rows = [
                row for row in verified_rows if row.get('installable')]
        rows = verified_rows[:result_limit]
        return {
            'catalog': rows,
            'online': {
                'provider': _PROVIDER,
                'attempted': True,
                'ok': True,
                'cached': cache_hit,
                'query': normalized_query,
                'result_count': len(rows),
                'verified_count': sum(
                    1 for row in rows if row.get('verified')),
            },
        }
    except OnlineCatalogError as exc:
        logger.info('[SkillsOnline] search unavailable (%s): %s',
                    exc.code, exc)
        status = {
            'provider': _PROVIDER,
            'attempted': True,
            'ok': False,
            'cached': False,
            'query': normalized_query,
            'error': exc.code,
            'retry_after': exc.retry_after,
            'verified_count': 0,
        }
        if use_cache:
            _FAILURE_CACHE.set(failure_key, dict(status))
        return {'catalog': [], 'online': status}


def _file_manifest(envelope: dict[str, Any]) -> dict[str, dict[str, Any]]:
    artifact = envelope.get('artifact')
    rows = artifact.get('files') if isinstance(artifact, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > _MAX_MANIFEST_FILES:
        raise OnlineCatalogError(
            'ClawHub verification did not contain a bounded file manifest.',
            code='online_manifest_invalid', http_status=409)
    manifest: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            raise OnlineCatalogError(
                'ClawHub file manifest is invalid.',
                code='online_manifest_invalid', http_status=409)
        path = _text(row.get('path'), max_chars=512)
        pure = PurePosixPath(path)
        if (not path or pure.is_absolute() or '..' in pure.parts
                or '\\' in path or path in manifest):
            raise OnlineCatalogError(
                'ClawHub file manifest contains an unsafe path.',
                code='online_manifest_invalid', http_status=409)
        try:
            size = int(row.get('size'))
        except (TypeError, ValueError) as exc:
            raise OnlineCatalogError(
                'ClawHub file manifest contains an invalid size.',
                code='online_manifest_invalid', http_status=409) from exc
        digest = str(row.get('sha256') or '').lower()
        if size < 0 or not _HEX_64_RE.fullmatch(digest):
            raise OnlineCatalogError(
                'ClawHub file manifest contains an invalid digest.',
                code='online_manifest_invalid', http_status=409)
        total += size
        if total > _MAX_PACKAGE_BYTES:
            raise OnlineCatalogError(
                'ClawHub package exceeds the 25 MiB product budget.',
                code='online_package_too_large', http_status=413)
        manifest[path] = {'size': size, 'sha256': digest}
    if 'SKILL.md' not in manifest:
        raise OnlineCatalogError(
            'ClawHub package manifest does not contain SKILL.md.',
            code='online_manifest_invalid', http_status=409)
    return manifest


def _github_handoff(
    payload: dict[str, Any],
) -> tuple[str, str, str, str]:
    if payload.get('sourceRef') != 'public-github':
        raise OnlineCatalogError(
            'ClawHub returned an unsupported download descriptor.',
            code='online_download_unsupported', http_status=409)
    repo = str(payload.get('repo') or '')
    commit = str(payload.get('commit') or '').lower()
    subdir = str(payload.get('path') or '').strip('/')
    content_hash = str(payload.get('contentHash') or '').lower()
    archive_url = str(payload.get('archiveUrl') or '')
    pure = PurePosixPath(subdir)
    if (not _REPO_RE.fullmatch(repo) or not _HEX_40_RE.fullmatch(commit)
            or not subdir or pure.is_absolute() or '..' in pure.parts
            or '\\' in subdir or not _HEX_64_RE.fullmatch(content_hash)
            or not _approved_url(archive_url, _ALLOWED_ARCHIVE_HOSTS)):
        raise OnlineCatalogError(
            'ClawHub returned an invalid GitHub source handoff.',
            code='online_handoff_invalid', http_status=409)
    parsed = urlsplit(archive_url)
    expected_path = f'/{repo}/zip/{commit}'
    if parsed.path.casefold() != expected_path.casefold():
        raise OnlineCatalogError(
            'ClawHub GitHub handoff did not bind the declared commit.',
            code='online_handoff_invalid', http_status=409)
    return archive_url, subdir, commit, content_hash


def install_clawhub_skill(
    catalog_id: str,
    source_revision: str,
    *,
    owner_user_id: int,
    project_path: str | None = None,
    scope: str = 'global',
    overwrite: bool = False,
    http_get_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Install one exact ClawHub version after fresh verification."""
    parsed = parse_clawhub_catalog_id(catalog_id)
    if parsed is None:
        raise OnlineCatalogError(
            'Not a ClawHub catalog id.',
            code='online_invalid_identity', http_status=400)
    owner, slug = parsed
    version = str(source_revision or '').strip()
    if not _VERSION_RE.fullmatch(version):
        raise OnlineCatalogError(
            'An exact ClawHub source_revision from search_skills is required.',
            code='online_revision_required', http_status=400)
    owner_id = require_user_id(
        owner_user_id, context='ClawHub skill install')
    if scope not in ('project', 'global'):
        raise OnlineCatalogError(
            f'Invalid skill scope: {scope!r}',
            code='invalid_scope', http_status=400)
    if scope == 'project' and not project_path:
        raise OnlineCatalogError(
            'Project scope requires an attached project.',
            code='project_required', http_status=400)

    getter = http_get_fn or http_get
    # Never use the discovery cache here. Current moderation and the exact
    # selected release are re-evaluated immediately before bytes are fetched.
    envelope = _verification_envelope(
        owner, slug, version=version, getter=getter)
    manifest = _file_manifest(envelope)
    page_url = _canonical_page_url(envelope.get('pageUrl'))
    data, content_type = _fetch_bytes(
        _DOWNLOAD_URL,
        params={'slug': slug, 'version': version}, getter=getter,
        allowed_hosts=_ALLOWED_REGISTRY_HOSTS,
        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
        max_bytes=_DOWNLOAD_MAX_BYTES,
        accept='application/zip, application/json')

    subdir = None
    ignored_paths: set[str] = set()
    upstream_revision = ''
    upstream_content_hash = ''
    if data.startswith(b'PK\x03\x04') or content_type in {
            'application/zip', 'application/x-zip-compressed'}:
        # ClawHub injects registry display metadata into hosted downloads.
        # Tofu installs only the exact publisher file manifest that was
        # verified; generated registry files remain on the registry page.
        ignored_paths.add('_meta.json')
        card = envelope.get('card')
        if isinstance(card, dict):
            card_path = _text(card.get('path'), max_chars=512)
            if card_path and card_path not in manifest:
                ignored_paths.add(card_path)
        archive = data
    else:
        try:
            descriptor = json.loads(data.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OnlineCatalogError(
                'ClawHub download was neither a zip nor a source handoff.',
                code='online_download_invalid', http_status=502) from exc
        if not isinstance(descriptor, dict):
            raise OnlineCatalogError(
                'ClawHub source handoff was invalid.',
                code='online_download_invalid', http_status=502)
        archive_url, subdir, upstream_revision, upstream_content_hash = (
            _github_handoff(descriptor))
        archive, archive_type = _fetch_bytes(
            archive_url, params=None, getter=getter,
            allowed_hosts=_ALLOWED_ARCHIVE_HOSTS,
            timeout=_DOWNLOAD_TIMEOUT_SECONDS,
            max_bytes=_DOWNLOAD_MAX_BYTES, accept='application/zip')
        if (not archive.startswith(b'PK\x03\x04')
                and archive_type not in {
                    'application/zip', 'application/x-zip-compressed'}):
            raise OnlineCatalogError(
                'ClawHub GitHub handoff did not return a zip archive.',
                code='online_download_invalid', http_status=502)

    try:
        result = install_skill_package(
            archive,
            scope=scope,
            project_path=project_path,
            owner_user_id=owner_id,
            overwrite=bool(overwrite),
            original_filename=f'{slug}-{version}.zip',
            catalog_id=build_clawhub_catalog_id(owner, slug),
            subdir=subdir,
            expected_file_manifest=manifest,
            ignored_archive_paths=ignored_paths,
            source_revision=version,
            source_registry=_PROVIDER,
            source_url=page_url,
        )
    except InstallerError as exc:
        raise OnlineCatalogError(
            str(exc), code='online_package_rejected', http_status=400) from exc
    result.update({
        'source': _PROVIDER,
        'publisher': owner,
        'source_url': page_url,
        'upstream_revision': upstream_revision,
        'upstream_content_hash': upstream_content_hash,
        'verification': {
            'decision': 'pass',
            'security_status': 'clean',
            'checked_at': (envelope.get('security') or {}).get('checkedAt'),
        },
    })
    return result


__all__ = [
    'OnlineCatalogError',
    'build_clawhub_catalog_id',
    'close_online_catalog',
    'install_clawhub_skill',
    'online_catalog_executor_snapshot',
    'parse_clawhub_catalog_id',
    'search_online_skills',
]
