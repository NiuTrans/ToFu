"""Effective Codex CLI client version resolution.

The Codex backend gates its ``/models`` catalogue on ``client_version``.
``CODEX_CLIENT_VERSION`` in ``lib.oauth.outbound`` is the floor; a local
last-good npm version can raise it. Request preflight only reads that cache
and schedules one finite refresh, never waiting for registry I/O. Like native
Codex metadata revalidation, the demand-driven worker is owned and joined at
reset/exit; there is no additional executor, queue, or polling service.
"""

from __future__ import annotations

import atexit
import math
import re
import threading
import time
from threading import Thread

from lib.config_dir import config_path
from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_NPM_LATEST_URL = 'https://registry.npmjs.org/@openai%2Fcodex/latest'
_FETCH_TIMEOUT_S = 2.5
_CACHE_TTL_S = 24 * 3600
_RETRY_INTERVAL_S = 60
_VERSION_RE = re.compile(r'^\d+\.\d+\.\d+$')

_lock = threading.Lock()
_refresh_lock = threading.Lock()  # One network flight, including its startup.
_publish_lock = threading.Lock()  # Disk publication vs. reset, never preflight.
_lifecycle_lock = threading.Lock()  # Serialize joined reset/exit operations.
_draining = threading.Event()
_memo: dict = {}
_worker: Thread | None = None
_generation = 0
_closed = False


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
        from lib.http_transport.client import http_get
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
    if not _VERSION_RE.fullmatch(version):
        raise RuntimeError(f'npm latest-version payload invalid: {version!r}')
    return version


def _load_cache(floor: str) -> None:
    """Initialize once under _lock; only local, size-bounded cache I/O."""
    path = _cache_path()
    cached = read_json(path, default={}, max_bytes=4096)
    cached = cached if isinstance(cached, dict) else {}
    version = str(cached.get('version') or '')
    valid = bool(_VERSION_RE.fullmatch(version))
    try:
        age = time.time() - float(cached.get('fetched_at_unix'))
    except (TypeError, ValueError, OverflowError):
        age = math.inf
    remaining = (_CACHE_TTL_S - age
                 if valid and math.isfinite(age) and 0 <= age < _CACHE_TTL_S
                 else 0)
    _memo.update(version=max(floor, version, key=_parse) if valid else floor,
                 refresh_at=time.monotonic() + remaining, path=path)


def _refresh_version(generation: int, path: str) -> None:
    try:
        # A reset can fence a reserved worker before it enters the network.
        with _lock:
            if (generation != _generation or _closed or _draining.is_set()
                    or _worker is not threading.current_thread()):
                return
        fetched = _fetch_latest_version()
        fetched_at = time.time()
        with _publish_lock:
            with _lock:
                if generation != _generation or _closed or _draining.is_set():
                    return
            try:
                write_json_atomic(path, {
                    'version': fetched,
                    'fetched_at_unix': fetched_at,
                }, mode=0o600)
            except Exception as exc:
                # A cache write failure must not discard a usable fetch or
                # force another network call on every request.
                logger.debug('[CodexVersion] cache write failed (%s)',
                             type(exc).__name__)
            with _lock:
                _memo.update(version=max(_floor(), fetched, key=_parse),
                             refresh_at=time.monotonic() + _CACHE_TTL_S,
                             path=path)
    except Exception as exc:
        with _lock:
            if generation == _generation and not _closed:
                _memo['refresh_at'] = time.monotonic() + _RETRY_INTERVAL_S
        logger.debug('[CodexVersion] latest-version refresh failed (%s)',
                     type(exc).__name__)
    finally:
        with _lock:
            # A failed startup may have retired a not-yet-running thread.
            # It must never release the lock belonging to a newer flight.
            if _worker is threading.current_thread():
                _refresh_lock.release()


def _schedule_refresh(generation: int) -> None:
    global _worker
    if not _refresh_lock.acquire(blocking=False):
        return
    handed_off = False
    worker = None
    try:
        with _lock:
            if (generation != _generation or _closed or _draining.is_set()
                    or (_worker is not None and _worker.is_alive())
                    or time.monotonic() < _memo['refresh_at']):
                return
            worker = Thread(target=_refresh_version,
                            args=(generation, _memo['path']), daemon=True,
                            name='codex-version-refresh')
            _worker = worker
        # Thread.start can wait for bootstrap; do not hold the reader lock.
        worker.start()
        handed_off = True
    except Exception as exc:
        with _lock:
            if generation == _generation:
                _memo['refresh_at'] = time.monotonic() + _RETRY_INTERVAL_S
        logger.debug('[CodexVersion] refresh admission failed (%s)',
                     type(exc).__name__)
    finally:
        if not handed_off:
            with _lock:
                # start() can raise after launching the OS thread. Once it
                # has an identity, only that worker may release its flight;
                # reset/shutdown must still see and join it. If it has not
                # started yet, revoke its identity under the same lock as
                # the worker's entry check, so a late start does no I/O.
                if worker is None:
                    _refresh_lock.release()
                elif worker.ident is None:
                    _worker = None
                    _refresh_lock.release()


def codex_client_version() -> str:
    """Return cached/floor immediately; stale metadata refreshes off-thread."""
    floor = _floor()
    with _lock:
        if not _memo and not _closed and not _draining.is_set():
            _load_cache(floor)
        resolved = max(floor, _memo.get('version', floor), key=_parse)
        refresh = (not _closed and not _draining.is_set()
                   and time.monotonic() >= _memo['refresh_at'])
        generation = _generation
    if refresh:
        _schedule_refresh(generation)
    return resolved


def _reset_cache(*, shutdown: bool = False) -> None:
    """Fence late publication, drain the exact worker, then forget local state.

    Only lifecycle/test callers wait. Request readers continue using the prior
    version/floor while a bounded fetch drains. Shutdown also closes admission.
    """
    global _worker, _generation, _closed
    with _lifecycle_lock:
        with _publish_lock, _lock:
            _generation += 1
            _closed = shutdown
            _draining.set()
        # Also covers a reserved Thread whose start has not returned yet.
        with _refresh_lock:
            with _lock:
                worker = _worker
                _worker = None
            if worker is not None:
                worker.join()
            with _lock:
                _memo.clear()
                _draining.clear()


def codex_user_agent() -> str:
    """The cloaked User-Agent carrying the resolved (not pinned) version."""
    from lib.oauth.outbound import _CODEX_USER_AGENT, CODEX_CLIENT_VERSION
    resolved = codex_client_version()
    if resolved == CODEX_CLIENT_VERSION:
        return _CODEX_USER_AGENT
    return _CODEX_USER_AGENT.replace(CODEX_CLIENT_VERSION, resolved)


atexit.register(_reset_cache, shutdown=True)

__all__ = ['codex_client_version', 'codex_user_agent']
