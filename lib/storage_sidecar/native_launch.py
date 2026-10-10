"""Resolve native authority paths in the child, then replace it with tofu-db.

Entry point: StorageSupervisor's private child launch. Runtime path policy is
shared with SidecarConfig; no Python process remains in the native RPC path.
The inherited empty stdin pipe retains parent-death and reexec ownership.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.projection_paths import (
    prepare_search_projection_directory,
    resolve_search_projection_paths,
    validate_search_projection_directory,
)


def _native_fastpath_namespace(authority: Path) -> str:
    """Source-path namespace shared with the native storage contract."""
    import hashlib

    return hashlib.sha256(str(authority.resolve()).encode('utf-8')).hexdigest()


def _bootstrap_fresh_authority(binary: str, authority: Path) -> None:
    """Initialize an empty native authority for a true fresh install.

    This runs only when the authority directory is entirely absent — the one
    state where no prior data exists to destroy. When the directory exists
    (even if partial or corrupt) we refuse and surface the migration path,
    because initializing over it would silently discard recoverable evidence.
    ``tofu-db init`` itself also refuses a non-empty directory, so a crash
    between creation and completion stays fail-closed on the next start.
    """
    if authority.exists():
        raise RuntimeError(
            'native authority is absent; complete offline migration first')
    namespace = _native_fastpath_namespace(authority)
    shadow = authority.parent / 'tofudb-fastpath-shadow' / namespace
    local_base = os.environ.get('TOFU_DB_FASTPATH_DIR', '').strip()
    front = (Path(local_base).resolve() / 'tofudb-fastpath' / namespace
             if local_base else None)
    if shadow.exists() or (front is not None and front.exists()):
        raise RuntimeError(
            'native fastpath recovery artifacts exist; verify and restore the '
            'configured authority instead of initializing an empty database')
    authority.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [binary, 'init', '--data-dir', str(authority)],
            check=True, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            'native authority bootstrap failed; not falling back') from exc
    if not authority.is_dir():
        raise RuntimeError('native authority bootstrap produced no authority')


def _durability_args() -> list:
    """Explicit WAL durability selection for the exec'd daemon.

    The engine defaults to ``--durability strict`` (per-commit barrier) and
    treats ``normal`` as an explicit bounded-RPO consent, never a silent
    default — so this launcher mirrors that contract: anything but the two
    documented values fails closed, and a flush interval without ``normal``
    is a configuration error, not something to silently ignore.  On
    network-mounted authorities (FUSE), strict-mode barrier waits add the
    mount's fsync latency to every command response; ``normal`` acknowledges
    after the in-memory commit and lets the background flusher own fdatasync
    (2026-09-18 FUSE-storm postmortem).
    """
    durability = os.environ.get('TOFU_DB_DURABILITY', '').strip().lower()
    if not durability or durability == 'strict':
        durability = 'strict'
    elif durability != 'normal':
        raise RuntimeError(
            'TOFU_DB_DURABILITY must be strict or normal')
    interval = os.environ.get('TOFU_DB_WAL_FLUSH_INTERVAL_MS', '').strip()
    args = []
    if durability == 'normal':
        args.extend(['--durability', 'normal'])
        if interval:
            try:
                value = int(interval)
            except ValueError as exc:
                raise RuntimeError(
                    'TOFU_DB_WAL_FLUSH_INTERVAL_MS must be an integer'
                ) from exc
            if not 1 <= value <= 1000:
                raise RuntimeError(
                    'TOFU_DB_WAL_FLUSH_INTERVAL_MS must be within 1..1000')
            args.extend(['--wal-flush-interval-ms', str(value)])
    elif interval:
        raise RuntimeError(
            'TOFU_DB_WAL_FLUSH_INTERVAL_MS requires TOFU_DB_DURABILITY=normal')
    return args


def main() -> None:
    args = sys.argv[1:]
    if len(args) not in (3, 4):
        raise ValueError('native launch expects binary, owner, tenant, and optional projection')
    binary, owner, tenant = args[:3]
    override = os.environ.get('TOFU_STORAGE_PROJECT_ROOT', '').strip()
    if override:
        if os.environ.get('TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE') != '1':
            raise RuntimeError('project-root override requires explicit authority')
        data_dir = Path(override).resolve() / 'data'
    else:
        from lib.runtime_paths import data_root

        data_dir = Path(data_root()).resolve()
    authority = data_dir / 'tofudb-authority'
    if not authority.is_dir():
        _bootstrap_fresh_authority(binary, authority)
    # A supervisor-provided fourth argument pins the launch snapshot: a save
    # between spawn and exec must not redirect the child behind its back.
    projection = (
        validate_search_projection_directory(args[3], authority)
        if len(args) == 4
        else resolve_search_projection_paths(data_dir).directory
    )
    prepare_search_projection_directory(projection, authority)
    command = [binary, 'serve', '--data-dir', str(authority),
               '--owner-id', owner, '--search-projection-dir',
               str(projection)]
    command.extend(_durability_args())
    native_fastpath = os.environ.get('TOFU_DB_FASTPATH_DIR', '').strip()
    native_interval = os.environ.get('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS', '').strip()
    if native_fastpath:
        if not Path(native_fastpath).is_absolute():
            raise RuntimeError('TOFU_DB_FASTPATH_DIR must be an absolute existing local base')
        command.extend(['--fastpath-dir', native_fastpath])
        if native_interval:
            try:
                interval = int(native_interval)
            except ValueError as exc:
                raise RuntimeError('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS must be an integer') from exc
            from lib.storage.generated_v2_semantics import (
                MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS, MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS,
            )
            if not MIN_NATIVE_FASTPATH_SYNC_INTERVAL_MS <= interval <= MAX_NATIVE_FASTPATH_SYNC_INTERVAL_MS:
                raise RuntimeError('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS must be within 1000..3600000')
            command.extend(['--fastpath-sync-interval-ms', str(interval)])
    elif native_interval:
        raise RuntimeError('TOFU_DB_FASTPATH_SYNC_INTERVAL_MS requires TOFU_DB_FASTPATH_DIR')
    if int(tenant):
        command.extend(['--tenant-id', tenant])
    parent_pid = int(os.environ['TOFU_STORAGE_PARENT_PID'])
    lease = ProjectLease(data_dir, expected_parent_pid=parent_pid,
                         owner_kind='tofudb', owner_label='Tofu-DB')
    lease.acquire()
    try:
        locator = {
            'format': 'tofu.storage-locator/v1', 'backend': 'tofudb',
            'authority_path': str(authority), 'fastpath_active': False,
        }
        if native_fastpath:
            # Match the native source-path namespace; this is a launch locator,
            # not a readiness or saved-preference witness.
            namespace = _native_fastpath_namespace(authority)
            base = Path(native_fastpath).resolve()
            locator.update({
                'authority_path': str(base / 'tofudb-fastpath' / namespace),
                'configured_path': str(authority), 'fastpath_active': True,
                'shadow_dir': str(authority.parent / 'tofudb-fastpath-shadow' / namespace),
            })
        lease.publish_storage_locator(locator)
        lease.inherit_across_exec()
        os.execv(binary, command)
    finally:
        lease.release()


if __name__ == '__main__':
    main()
