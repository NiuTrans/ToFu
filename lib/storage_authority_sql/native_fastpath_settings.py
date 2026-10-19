"""Native fastpath next-launch settings; never relocate/open authority data.

Panel entry points: native_fastpath_state and save_native_fastpath_settings.
Runtime readiness is the active witness; FASTPATH is an irreversible selection
fence until an operator performs the documented offline fastpath-export.
"""
from __future__ import annotations

import hashlib

import json
import os

import stat
from pathlib import Path
import threading
from collections.abc import Mapping, MutableMapping
from typing import Any

from tofu_dotenv import read_dotenv_values, update_dotenv_file
from lib.storage_authority_sql.storage_capabilities import describe_mount

FASTPATH_ENV = 'TOFU_DB_FASTPATH_DIR'
_LOCK = threading.RLock()
_LOCK_REASON = ('Native fastpath authority is retained. Stop the application and use '
                'offline tofu-db fastpath-export before disabling or changing its base; '
                'the retained seed must not be served as the live authority.')


class NativeFastpathSettingsError(ValueError):
    """Stable conflict taxonomy for the panel HTTP adapter."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _selection(data_dir: Path, base: str) -> tuple[Path, str, str]:
    authority = (data_dir / 'tofudb-authority').resolve()
    namespace = hashlib.sha256(str(authority).encode('utf-8')).hexdigest()
    front = str(Path(base).resolve() / 'tofudb-fastpath' / namespace) if base else ''
    shadow = str(authority.parent / 'tofudb-fastpath-shadow' / namespace)
    return authority, front, shadow


def _retained_front(authority: Path) -> str:
    for name in ('FASTPATH', 'FASTPATH_PENDING'):
        marker = authority / name
        if os.path.lexists(marker):
            try:
                # A byte cap does not bound FIFO open/read. No-follow plus
                # nonblocking open and descriptor-level verification fence
                # symlink/FIFO replacement races without holding the save lock.
                if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_NONBLOCK'):
                    return ''
                fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        return ''
                    raw = os.read(fd, 16385)
                finally:
                    os.close(fd)
                if len(raw) > 16384:
                    return ''
                value = json.loads(raw)
                return str(value.get('local_authority') or '') if isinstance(value, dict) else ''
            except (OSError, ValueError):
                return ''
    return ''


def native_fastpath_state(data_dir: Path, runtime: Mapping[str, Any],
                          environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if environ is None else environ
    base = env.get(FASTPATH_ENV, '').strip()
    authority, pending, _ = _selection(data_dir, base)
    witness = runtime.get('native_fastpath')
    ready = bool(runtime.get('ready')) and runtime.get('backend') == 'tofudb'
    active = (isinstance(witness, dict) if ready else None)
    front = str(witness.get('localAuthority') or '') if active else ''
    shadow = str(witness.get('shadowDirectory') or '') if active else ''
    # lstat fences even a damaged/dangling marker; never reinterpret it as off.
    locked = bool(active) or os.path.lexists(authority / 'FASTPATH') or os.path.lexists(authority / 'FASTPATH_PENDING')
    return {
        'supported': True, 'kind': 'native', 'mode': 'on' if base else 'off',
        'enabled': bool(base), 'directory': base, 'active': active,
        'activeDirectory': front if active else (str(authority) if active is False else ''),
        'restartRequired': (pending != front if active else bool(base)),
        'locked': locked, 'lockReason': _LOCK_REASON if locked else '',
        'authorityPath': front if active else (str(authority) if active is False else ''),
        'shadowPath': shadow,
    }


def _validate_base(base: str, authority: Path) -> str:
    if not isinstance(base, str) or not base or len(base) > 4096 or '\x00' in base:
        raise ValueError('Native fastPath requires an explicit directory')
    path = Path(base)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError('Native fastPath.directory must be an existing absolute directory')
    path = path.resolve()
    if path.is_relative_to(authority) or authority.is_relative_to(path):
        raise ValueError('Native fastPath.directory must not overlap durable authority')
    mount = describe_mount(path)
    if mount.storage_class != 'local-block' or mount.persistence != 'persistent':
        raise ValueError('Native fastPath.directory requires observed local persistent non-memory storage')
    if not os.access(path, os.W_OK | os.X_OK):
        raise ValueError('Native fastPath.directory must be writable')
    # No mkdir, leaf probe, source rewrite or relocation at save. Native launch
    # performs measured capacity/fsync admission before activating this consent.
    _, front, shadow = _selection(authority.parent, str(path))

    front_path, shadow_path = Path(front).resolve(), Path(shadow).resolve()
    if front_path.is_relative_to(shadow_path) or shadow_path.is_relative_to(front_path):
        raise ValueError('Native fastpath front must not overlap its recovery shadow')
    for target in (Path(front).parent, Path(front), Path(shadow)):
        if os.path.lexists(target) and target.resolve() != target:
            raise ValueError('Native fastpath namespace must not contain symlinks')
    return str(path)


def _validate_launch_constraints(env: Mapping[str, str]) -> None:
    if env.get('TOFUDB_TRANSPORT', 'auto').strip().lower() == 'embedded':
        raise ValueError('Native fastpath requires daemon transport; explicit embedded mode cannot enable it')
    if env.get('TOFU_DB_DURABILITY', 'strict').strip().lower() not in ('', 'strict') or env.get('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '').strip():
        raise ValueError('Native fastpath requires Strict WAL durability without a flush interval')
    interval = env.get('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', '').strip()
    if interval:
        from lib.storage.generated_v2_semantics import (
            MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS, MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS,
        )
        try:
            cadence = int(interval)
        except ValueError as exc:
            raise ValueError('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS must be an integer') from exc
        if not MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS <= cadence <= MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS:
            raise ValueError('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS is outside native contract bounds')


def save_native_fastpath_settings(data_dir: Path, env_path: Path, enabled: bool,
                                  directory: str, runtime: Mapping[str, Any], *,
                                  environ: MutableMapping[str, str] | None = None) -> dict[str, Any]:
    """Serialize file/environment publication without mutating live authority."""
    env = os.environ if environ is None else environ
    with _LOCK:
        state = native_fastpath_state(data_dir, runtime, env)

        if enabled:
            _validate_launch_constraints(env)
        current = state['directory']
        requested = directory if enabled else ''
        authority, _, _ = _selection(data_dir, current)
        same = bool(requested) and bool(current) and Path(requested).is_absolute() and Path(requested).resolve() == Path(current).resolve()
        if state['locked']:
            witness = runtime.get('native_fastpath')
            if isinstance(witness, dict) and witness.get('localAuthority'):
                _, candidate, _ = _selection(data_dir, requested)
                same = bool(requested) and Path(requested).is_absolute() and candidate == str(witness['localAuthority'])
            retained = _retained_front(authority)
            if os.path.lexists(authority / 'FASTPATH') or os.path.lexists(authority / 'FASTPATH_PENDING'):
                _, candidate, _ = _selection(data_dir, requested)
                same = bool(requested) and Path(requested).is_absolute() and bool(retained) and candidate == retained
            if not same:
                raise NativeFastpathSettingsError('fastpath_locked', _LOCK_REASON)
            if current and Path(current).resolve() == Path(requested).resolve():
                return state
            saved = read_dotenv_values(env_path)
            if FASTPATH_ENV in env and env[FASTPATH_ENV] != saved.get(FASTPATH_ENV, ''):
                raise NativeFastpathSettingsError('fastpath_env_override',
                    'TOFU_DB_FASTPATH_DIR is controlled by an inherited environment override')
            requested = str(Path(requested).resolve())
            update_dotenv_file(env_path, {FASTPATH_ENV: requested})
            env[FASTPATH_ENV] = requested
            return native_fastpath_state(data_dir, runtime, env)
        saved = read_dotenv_values(env_path)
        if FASTPATH_ENV in env and (FASTPATH_ENV not in saved or env[FASTPATH_ENV] != saved[FASTPATH_ENV]):
            raise NativeFastpathSettingsError('fastpath_env_override',
                'TOFU_DB_FASTPATH_DIR is controlled by an inherited environment override; change it at the deployment boundary')
        if enabled:
            requested = _validate_base(requested, authority)
        elif env.get('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', '').strip():
            raise ValueError('Remove TOFU_DB_FASTPATH_SYNC_INTERVAL_MS before disabling native fastpath')
        update_dotenv_file(env_path, {FASTPATH_ENV: requested})
        env[FASTPATH_ENV] = requested
        return native_fastpath_state(data_dir, runtime, env)
