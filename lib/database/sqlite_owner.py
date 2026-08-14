"""Cross-host write ownership guard for the authoritative SQLite file.

SQLite/WAL coordinates local processes, but a shared BeeGFS project can be
mounted by two hosts whose lock behaviour must not be trusted as the only
split-brain barrier.  A small, atomically refreshed marker makes ownership
explicit and fail-closed before a process enters the writer lane.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

OWNER_FILE = '.tofu_db_owner'
LOCK_DIR = '.tofu_db_owner.lock'
REFRESH_S = 30.0
TTL_S = 120.0


class SQLiteOwnershipError(RuntimeError):
    """Raised when another host has a fresh claim on the SQLite authority."""


_state_lock = threading.RLock()
_claim: dict | None = None
_lost_reason = ''
_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None
_instance_id = uuid.uuid4().hex
_last_verified_wall = 0.0
_write_authority_local = threading.local()


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _host_id() -> str:
    return (os.environ.get('TOFU_DB_HOST_ID') or socket.gethostname()).strip()


def guard_required(db_path: str, canonical_path: str) -> bool:
    """Whether this process/path represents the writable SQLite authority."""
    explicit_guard = os.environ.get('TOFU_SQLITE_OWNER_GUARD')
    if not _truthy(explicit_guard, default=True):
        return False
    try:
        is_canonical = Path(db_path).resolve() == Path(canonical_path).resolve()
    except OSError as exc:
        logger.debug('[DB] SQLite owner canonical-path resolve failed: %s', exc)
        is_canonical = os.path.abspath(db_path) == os.path.abspath(canonical_path)
    # Some tests import server.py during collection, which leaves its
    # process-wide TOFU_SERVER_PROCESS=1 behind even when later fixtures
    # repoint DB_PATH to a per-test temp file.  Unless a test explicitly opts
    # into this guard, only the canonical authority is guarded under pytest.
    if 'PYTEST_CURRENT_TEST' in os.environ and explicit_guard is None:
        return is_canonical
    return is_canonical or _truthy(os.environ.get('TOFU_SERVER_PROCESS'), False)


def _is_canonical_path(db_path: str, canonical_path: str) -> bool:
    try:
        return Path(db_path).resolve() == Path(canonical_path).resolve()
    except OSError as exc:
        logger.debug('[DB] canonical owner path resolution failed: %s', exc)
        return os.path.abspath(db_path) == os.path.abspath(canonical_path)


def _maintenance_authorized() -> bool:
    return bool(getattr(_write_authority_local, 'depth', 0))


def write_role_authorized() -> bool:
    """Whether this thread may acquire an authoritative SQLite writer."""
    return (_truthy(os.environ.get('TOFU_SERVER_PROCESS'), False)
            or _maintenance_authorized())


@contextmanager
def maintenance_write_authority(purpose: str):
    """Explicitly authorize one offline tool to mutate canonical SQLite.

    Normal application writes are authorized by ``TOFU_SERVER_PROCESS=1``.
    Installers and reviewed maintenance commands must enter this thread-local
    scope instead; a random debug/import process can therefore read the
    authority but fails closed on its first attempted write.

    This is an authorization boundary, not a transaction boundary. Callers
    must still use the shared write transaction/tooling primitive.
    """
    purpose = str(purpose or '').strip()
    if not purpose:
        raise ValueError('SQLite maintenance write purpose must not be empty')
    depth = int(getattr(_write_authority_local, 'depth', 0) or 0)
    purposes = list(getattr(_write_authority_local, 'purposes', ()) or ())
    _write_authority_local.depth = depth + 1
    _write_authority_local.purposes = (*purposes, purpose)
    try:
        yield
    finally:
        _write_authority_local.depth = depth
        _write_authority_local.purposes = tuple(purposes)


def assert_write_authorized(
        db_path: str, canonical_path: str, *, authority_path: str | None = None
) -> None:
    """Reject canonical writes from undeclared non-server processes."""
    target = authority_path or canonical_path
    if not _is_canonical_path(db_path, target):
        return
    # Pytest frequently repoints the active DB to a throwaway path. Preserve
    # fixture ergonomics unless a test explicitly opts into owner enforcement;
    # a real process has no PYTEST_CURRENT_TEST marker and always fails closed.
    if (os.environ.get('PYTEST_CURRENT_TEST')
            and os.environ.get('TOFU_SQLITE_OWNER_GUARD') is None
            and not _is_canonical_path(db_path, canonical_path)):
        return
    if write_role_authorized():
        return
    raise SQLiteOwnershipError(
        'canonical SQLite writes are restricted to the server process or an '
        'explicit maintenance_write_authority() scope; refusing an ambient '
        f'process write to {Path(db_path).resolve()}')


def _paths(db_path: str) -> tuple[Path, Path]:
    parent = Path(db_path).resolve().parent
    return parent / OWNER_FILE, parent / LOCK_DIR


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f'.tmp-{os.getpid()}-{uuid.uuid4().hex}')
    try:
        with tmp.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError as exc:
            logger.debug('[DB] SQLite owner temp already absent: %s', exc)
            pass


def _read(path: Path) -> tuple[dict | None, float | None, Exception | None]:
    try:
        stat = path.stat()
    except FileNotFoundError as exc:
        logger.debug('[DB] SQLite owner marker absent: %s', exc)
        return None, None, None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('owner marker is not a JSON object')
        return value, stat.st_mtime, None
    except Exception as exc:
        logger.debug('[DB] SQLite owner marker parse/read failed: %s', exc)
        return None, stat.st_mtime, exc


def _fresh(mtime: float | None, now: float | None = None) -> bool:
    if mtime is None:
        return False
    age = (time.time() if now is None else now) - mtime
    # Negative age means clock skew; fail closed and treat it as fresh.
    return age <= TTL_S


@contextmanager
def _marker_lock(lock_dir: Path, timeout_s: float = 10.0):
    """Serialize marker compare/replace with atomic mkdir on the shared FS."""
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            try:
                age = time.time() - lock_dir.stat().st_mtime
            except FileNotFoundError as exc:
                logger.debug('[DB] SQLite marker lock vanished during stat: %s',
                             exc)
                continue
            if age > TTL_S:
                stale = lock_dir.with_name(
                    lock_dir.name + f'.stale-{os.getpid()}-{uuid.uuid4().hex}')
                try:
                    os.replace(lock_dir, stale)
                    stale.rmdir()
                    continue
                except (FileNotFoundError, OSError) as exc:
                    logger.debug('[DB] SQLite stale marker-lock takeover race: %s',
                                 exc)
                    pass
            if time.monotonic() >= deadline:
                raise SQLiteOwnershipError(
                    f'timed out acquiring SQLite owner marker lock {lock_dir}')
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            lock_dir.rmdir()
        except FileNotFoundError as exc:
            logger.debug('[DB] SQLite marker lock already released: %s', exc)
            pass


def _new_record(db_path: str, existing: dict | None = None) -> dict:
    now = time.time()
    members = {}
    if existing and existing.get('host') == _host_id():
        old_members = existing.get('members')
        if isinstance(old_members, dict):
            for key, value in old_members.items():
                if (isinstance(value, dict)
                        and now - float(value.get('ts', 0)) <= TTL_S):
                    members[str(key)] = value
    members[_instance_id] = {'pid': os.getpid(), 'ts': now}
    return {
        'version': 1,
        'host': _host_id(),
        'updated_at': now,
        'db': str(Path(db_path).resolve()),
        'members': members,
    }


def _claim_or_refresh(db_path: str) -> dict:
    owner_path, lock_dir = _paths(db_path)
    with _marker_lock(lock_dir):
        existing, mtime, error = _read(owner_path)
        if error is not None and _fresh(mtime):
            raise SQLiteOwnershipError(
                f'fresh SQLite owner marker is unreadable; refusing write mode: {error}')
        if (existing and _fresh(mtime)
                and existing.get('host') not in (None, '', _host_id())):
            raise SQLiteOwnershipError(
                f'SQLite authority is owned by host={existing.get("host")!r}; '
                f'marker={owner_path}')
        record = _new_record(db_path, existing)
        _atomic_write(owner_path, record)
        return record


def _heartbeat_loop(db_path: str) -> None:
    global _claim, _lost_reason, _last_verified_wall
    while not _stop.wait(REFRESH_S):
        try:
            record = _claim_or_refresh(db_path)
            with _state_lock:
                _claim = record
                _last_verified_wall = time.time()
        except Exception as exc:
            with _state_lock:
                _lost_reason = str(exc)
            logger.critical(
                '[DB] SQLite write ownership LOST; all new writes will fail: %s',
                exc)
            return


def claim_owner(db_path: str) -> dict:
    """Claim/refresh ownership and start the process heartbeat."""
    global _claim, _lost_reason, _heartbeat_thread, _last_verified_wall
    resolved = str(Path(db_path).resolve())
    with _state_lock:
        if _claim and _claim.get('db') != resolved:
            raise SQLiteOwnershipError(
                f'process already owns a different SQLite authority: {_claim.get("db")}')
        if _claim and not _lost_reason:
            return _claim
    record = _claim_or_refresh(resolved)
    with _state_lock:
        _claim = record
        _lost_reason = ''
        _last_verified_wall = time.time()
        _stop.clear()
        if _heartbeat_thread is None or not _heartbeat_thread.is_alive():
            _heartbeat_thread = threading.Thread(
                target=_heartbeat_loop, args=(resolved,), daemon=True,
                name='sqlite-owner-heartbeat')
            _heartbeat_thread.start()
    logger.info('[DB] SQLite write ownership claimed: host=%s pid=%d marker=%s',
                _host_id(), os.getpid(), _paths(resolved)[0])
    return record


def assert_owner(
        db_path: str, canonical_path: str, *, authority_path: str | None = None
) -> None:
    """Fail closed before a write if this host does not own the authority."""
    global _claim, _lost_reason, _last_verified_wall
    # This authorization gate is independent from the optional cross-host
    # marker knob. Disabling marker coordination for a local filesystem must
    # never make a bare debug process an implicit canonical writer.
    assert_write_authorized(
        db_path, canonical_path, authority_path=authority_path)
    if not guard_required(db_path, canonical_path):
        return
    with _state_lock:
        claim = _claim
        lost = _lost_reason
        last_verified = _last_verified_wall
    if lost:
        raise SQLiteOwnershipError(f'SQLite write ownership lost: {lost}')
    if claim is None:
        claim_owner(db_path)
        return
    if claim.get('db') != str(Path(db_path).resolve()):
        raise SQLiteOwnershipError('SQLite owner claim does not match active DB path')
    # Wall time advances across VM/container pause. Validate immediately after
    # resume, while keeping the hot path to at most one marker read per 5s.
    if time.time() - last_verified < 5.0:
        return
    owner_path, _lock_dir = _paths(db_path)
    existing, mtime, error = _read(owner_path)
    if (error is not None or not existing or not _fresh(mtime)
            or existing.get('host') != _host_id()):
        try:
            record = _claim_or_refresh(db_path)
        except Exception as exc:
            with _state_lock:
                _lost_reason = str(exc)
            raise SQLiteOwnershipError(
                f'SQLite write ownership validation failed: {exc}') from exc
        with _state_lock:
            _claim = record
    with _state_lock:
        _last_verified_wall = time.time()


def release_owner() -> None:
    """Remove only this process member; preserve same-host peer claims."""
    global _claim, _heartbeat_thread
    _stop.set()
    thread = _heartbeat_thread
    if thread and thread is not threading.current_thread():
        thread.join(timeout=1.0)
    with _state_lock:
        claim = _claim
        _claim = None
        _heartbeat_thread = None
    if not claim:
        return
    owner_path, lock_dir = _paths(claim['db'])
    try:
        with _marker_lock(lock_dir, timeout_s=1.0):
            existing, _mtime, _error = _read(owner_path)
            if not existing or existing.get('host') != _host_id():
                return
            members = existing.get('members')
            if not isinstance(members, dict):
                members = {}
            members.pop(_instance_id, None)
            now = time.time()
            members = {
                key: value for key, value in members.items()
                if isinstance(value, dict)
                and now - float(value.get('ts', 0)) <= TTL_S
            }
            if members:
                existing['members'] = members
                existing['updated_at'] = max(
                    float(value.get('ts', 0)) for value in members.values())
                _atomic_write(owner_path, existing)
            else:
                owner_path.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning('[DB] Could not release SQLite owner marker: %s', exc)


atexit.register(release_owner)


__all__ = [
    'SQLiteOwnershipError', 'assert_owner', 'claim_owner', 'release_owner',
    'assert_write_authorized', 'guard_required',
    'maintenance_write_authority', 'write_role_authorized',
    'OWNER_FILE', 'REFRESH_S', 'TTL_S',
]
