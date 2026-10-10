"""Native search-projection paths and next-launch configuration authority.

The projection is required but reconstructible; it is NOT the SQLite write
fastpath or durable user data. Daemon/embedded launch and the database panel
share this resolver. Only explicit apply/launch probes create directories;
status resolution never writes, migrates, copies, or removes database data.
"""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from lib.json_store import locked_path, read_json

SEARCH_PROJECTION_CONFIG_KEY = 'tofudb_search_projection_dir'
SEARCH_PROJECTION_ENV = 'TOFUDB_SEARCH_PROJECTION_DIR'
MAX_DIRECTORY_LENGTH = 4096
_UNSET = object()


class SearchProjectionEnvironmentOverrideError(ValueError):
    """An explicit launch environment prevents applying a saved preference."""


@dataclass(frozen=True)
class SearchProjectionPaths:
    authority_directory: Path
    configured_directory: str
    default_directory: Path
    directory: Path


def _settings_path(data_dir: Path) -> Path:
    return data_dir / 'config' / 'server_config.json'


def _settings_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError('Search projection settings must be a JSON object')
    return value


def configured_search_projection_directory(
    data_dir: Path, environ: Mapping[str, str] | None = None,
) -> str:
    """Environment > persisted dedicated key > automatic (empty string).

    An explicitly empty environment override selects automatic, too. Read the
    file afresh: saving changes the next launch, never a live runtime's path.
    Malformed/unreadable settings fail closed instead of overwriting evidence.
    """
    env = os.environ if environ is None else environ
    if SEARCH_PROJECTION_ENV in env:
        value = env[SEARCH_PROJECTION_ENV]
    else:
        settings = _settings_object(read_json(
            str(_settings_path(data_dir)), default={}, strict=True))
        value = settings.get(SEARCH_PROJECTION_CONFIG_KEY, '')
    if not isinstance(value, str):
        raise ValueError(f'{SEARCH_PROJECTION_CONFIG_KEY} must be a string')
    return value


def _absolute_directory(value: Any, *, field: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be a string')
    if len(value) > MAX_DIRECTORY_LENGTH or '\x00' in value:
        raise ValueError(f'{field} is too long or contains NUL')
    if not value or not Path(value).is_absolute():
        raise ValueError(f'{field} must be an absolute path')
    try:
        return Path(value).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f'{field} cannot be resolved') from exc


def _reject_authority_overlap(directory: Path, authority: Path) -> None:
    if directory.is_relative_to(authority) or authority.is_relative_to(directory):
        raise ValueError('searchProjection.directory must not overlap authority data')


def validate_search_projection_directory(directory: str, authority: Path) -> Path:
    """Validate an effective launch path, including canonical symlink overlap."""
    target = _absolute_directory(directory, field='searchProjection.directory')
    _reject_authority_overlap(target, authority.resolve())
    return target


def resolve_search_projection_paths(
    data_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    configured_directory: Any = _UNSET,
) -> SearchProjectionPaths:
    """Resolve one stable base/effective selection without filesystem writes.

    Keep the legacy default so existing deployments are never relocated
    implicitly. A custom base never directly contains search data: namespace
    it by the canonical authority identity so two installations selecting the
    same candidate cannot share an index. Canonical aliases of one authority
    intentionally resolve to the same namespace.
    """
    root = Path(data_dir).resolve()
    authority = (root / 'tofudb-authority').resolve()
    default = validate_search_projection_directory(
        str(root / 'projections' / 'tofudb-search'), authority)
    value = configured_directory
    if value is _UNSET:
        value = configured_search_projection_directory(root, environ)
    if not isinstance(value, str):
        raise ValueError('searchProjection.directory must be a string')
    if value == '':
        return SearchProjectionPaths(authority, '', default, default)
    base = _absolute_directory(value, field='searchProjection.directory')
    _reject_authority_overlap(base, authority)
    identity = hashlib.sha256(os.fsencode(os.path.normcase(str(authority)))).hexdigest()
    namespaced = base / 'tofudb-search' / identity
    effective = validate_search_projection_directory(str(namespaced), authority)
    # An existing namespace symlink must not collapse independent authorities
    # onto one index, even when it points somewhere outside authority data.
    if effective != namespaced:
        raise ValueError('searchProjection.directory namespace must not contain symlinks')
    return SearchProjectionPaths(authority, str(base), default, effective)


def prepare_search_projection_directory(directory: Path, authority: Path) -> Path:
    """Probe writability without pre-creating a native index leaf.

    Native open distinguishes absent (initialize) from existing (open). An
    empty pre-created leaf is NOT a valid index, so probe only its parent when
    absent. Never remove an existing empty/invalid index to force a rebuild.
    """
    target = validate_search_projection_directory(str(directory), authority)
    try:
        if target.exists():
            if not target.is_dir():
                raise ValueError('searchProjection.directory must be a writable directory')
            if next(target.iterdir(), None) is None:
                raise ValueError(
                    'searchProjection.directory is an existing empty index directory; '
                    'choose a new base directory')
            probe_directory = target
        else:
            probe_directory = target.parent
            probe_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # FUSE may reject writes after unlink; retain the private name until close.
        with tempfile.NamedTemporaryFile(prefix='.tofu-projection-probe-', dir=probe_directory) as probe:
            probe.write(b'tofu-search-projection-probe')
            probe.flush()
    except OSError as exc:
        raise ValueError('searchProjection.directory must be a writable directory') from exc
    return target


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _prepare_settings_directory(directory: Path) -> None:
    if not directory.is_dir():
        # Publish parents durably before creating children, including a new data root.
        _prepare_settings_directory(directory.parent)
        directory.mkdir(mode=0o700, exist_ok=True)
    # Also cover an existing entry left by a concurrent or failed creator.
    _fsync_directory(directory.parent)


def _save_projection_setting(path: Path, directory: str) -> None:
    """Strict RMW; shared JSON writers use the same thread/process lock."""
    _prepare_settings_directory(path.parent)
    with locked_path(str(path)):
        settings = dict(_settings_object(read_json(str(path), default={}, strict=True)))
        settings[SEARCH_PROJECTION_CONFIG_KEY] = directory
        text = json.dumps(settings, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
        temporary_path = None
        try:
            # A private, same-directory name remains linked through write/close on FUSE.
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=path.parent,
                prefix='.server-config-', suffix='.tmp', delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(text)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
            # Failure here is an uncertain durable commit, not permission to roll back.
            _fsync_directory(path.parent)
        finally:
            if temporary_path is not None:
                # Best-effort staging cleanup must not hide the original write failure.
                with contextlib.suppress(OSError):
                    os.unlink(temporary_path)


def save_search_projection_directory(
    data_dir: Path, directory: Any, *, environ: Mapping[str, str] | None = None,
) -> SearchProjectionPaths:
    """Validate and durably persist a next-launch preference, not live state.

    Never rewrite an operator's environment or pretend a lower-priority saved
    value took effect. Failures propagate without changing the active runtime.
    Before replacement, old settings survive byte-for-byte; after replacement,
    a directory-fsync failure leaves the new settings visible without rollback.
    """
    env = os.environ if environ is None else environ
    if SEARCH_PROJECTION_ENV in env:
        raise SearchProjectionEnvironmentOverrideError(
            f'{SEARCH_PROJECTION_ENV} overrides saved settings; remove it before applying')
    paths = resolve_search_projection_paths(data_dir, configured_directory=directory)
    prepare_search_projection_directory(paths.directory, paths.authority_directory)

    _save_projection_setting(
        _settings_path(Path(data_dir).resolve()), paths.configured_directory)
    return paths
