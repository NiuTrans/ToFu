"""Backend switching for the database panel.

Selection persists as ``TOFU_PERSONAL_STORAGE_BACKEND`` in the project
``.env`` and takes effect through the existing restart machinery — there is
no runtime store switching (docs/STORAGE.md).  Data is never deleted by a
switch: each backend keeps its own authority location, so switching back
restores the previous state.

SQLite→TofuDB with ``migrate=True`` runs the certified offline import
(``lib.storage_authority_sql.migration.tofudb_migration_import``) against a verified online
backup artifact of the live SQLite authority, so the server keeps serving
during migration.  TofuDB→SQLite has no export tool; the TofuDB authority
is retained untouched and the SQLite side starts fresh.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from lib.log import get_logger
from lib.runtime.runtime_paths import data_root

logger = get_logger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND_ENV = 'TOFU_PERSONAL_STORAGE_BACKEND'
_VALID_BACKENDS = frozenset({'sqlite', 'tofudb'})
_IMPORT_TIMEOUT_S = 4 * 60 * 60
_CHECKPOINT_NAME = 'tofudb-import-checkpoint.json'
_AUTHORITY_UUID_RE = re.compile(r'authority_uuid=([0-9a-fA-F-]{36})')

_lock = threading.Lock()
_migration_job: dict[str, Any] | None = None


class SwitchError(RuntimeError):
    """Typed validation failure; ``code`` is stable for the UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _env_path() -> Path:
    return _REPO_ROOT / '.env'


def env_file_path() -> Path:
    """Public accessor for the project .env (routes write through it too)."""
    return _env_path()


def resolve_authority_path(raw: str) -> Path:
    """Resolve one authority-returned path (absolute, or project-relative).

    The backup operation returns paths relative to the sidecar's project
    root; in personal mode that root may be the data directory or the
    checkout, so resolve by existence against both.
    """
    path = Path(raw)
    if path.is_absolute():
        return path
    for base in (Path(data_root()), _REPO_ROOT):
        candidate = base / path
        if candidate.exists():
            return candidate.resolve()
    return (Path(data_root()) / path).resolve()


def _authority_dir() -> Path:
    return Path(data_root()) / 'tofudb-authority'


def _sqlite_db() -> Path:
    return Path(data_root()) / 'tofu.db'


def _authority_has_data(path: Path) -> bool:
    from lib.database_panel.probe import tofudb_authority_has_data

    return tofudb_authority_has_data(path)


def _is_empty_initialized_authority(path: Path) -> bool:
    """An init-only authority (no content blocks) — safe to re-init.

    A crash between ``tofu-db init`` and the import leaves exactly this
    shape; replacing it loses nothing because there is no user content.
    Anything unreadable or unexpected answers False (never destructive).
    """
    try:
        if not path.is_dir() or _authority_has_data(path):
            return False
        total = 0
        for root, _dirs, files in os.walk(path):
            for name in files:
                total += (Path(root) / name).stat().st_size
                if total > 256 * 1024:
                    return False
        return True
    except OSError:
        return False


def describe(engine: dict[str, Any] | None = None, *, full: bool = False
             ) -> dict[str, Any]:
    """Share display evidence; only mutation asks for a full data witness."""
    from lib.database_panel.probe import engine_status

    engine = engine_status() if engine is None else engine

    sqlite_db = _sqlite_db()
    sqlite_bytes = 0
    try:
        if sqlite_db.is_file():
            sqlite_bytes = sqlite_db.stat().st_size
    except OSError:
        sqlite_bytes = 0
    return {
        'current': engine['backend'],
        'sqlite': {
            **engine['engines']['sqlite'],
            'hasData': sqlite_bytes > 0 if full else engine['engines']['sqlite']['hasData'],
            'dataBytes': sqlite_bytes,
        },
        'tofudb': {
            **engine['engines']['tofudb'],
            'hasData': (_authority_has_data(_authority_dir()) if full
                        else engine['engines']['tofudb']['hasData']),
            'authorityPath': str(_authority_dir()),
        },
    }


def _write_backend_env(backend: str) -> None:
    from tofu_dotenv import update_dotenv_file

    env_file = _env_path()
    if env_file.exists() and not os.access(env_file, os.W_OK):
        raise SwitchError('env_unwritable', f'.env is not writable: {env_file}')
    update_dotenv_file(env_file, {_BACKEND_ENV: backend})
    # The approval-gated restart re-execs THIS process, so the child inherits
    # this environment; boot-time dotenv loading never overrides an existing
    # variable (tofu_dotenv.load_dotenv_file). Publishing the new selection
    # here is what lets the re-exec actually land on the chosen backend.
    # Nothing re-reads the selection in the running process except a
    # crash-restart of the same supervisor — which would then simply apply
    # the user's requested switch a little earlier, with both stores intact.
    os.environ[_BACKEND_ENV] = backend
    logger.info('[DatabasePanel] wrote %s=%s to %s',
                _BACKEND_ENV, backend, env_file)


def _job_snapshot() -> dict[str, Any] | None:
    with _lock:
        if _migration_job is None:
            return None
        return {key: _migration_job[key] for key in
                ('id', 'state', 'phase', 'startedAt', 'finishedAt', 'error',
                 'resumeSupported')}


def migration_status() -> dict[str, Any]:
    return {'job': _job_snapshot()}


def _set_phase(job: dict[str, Any], phase: str) -> None:
    with _lock:
        job['phase'] = phase


def _uuid_from_checkpoint(checkpoint: Path) -> str:
    """Recover the bound target authority UUID from a resume journal."""
    import json as _json

    try:
        raw = _json.loads(checkpoint.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise SwitchError(
            'resume_unsupported',
            f'migration checkpoint is unreadable: {type(exc).__name__}') from exc

    def _walk(node: Any) -> str:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == 'target_authority_uuid' and isinstance(value, str):
                    return value
                found = _walk(value)
                if found:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = _walk(item)
                if found:
                    return found
        return ''

    uuid = _walk(raw)
    if not uuid:
        raise SwitchError(
            'resume_unsupported',
            'migration checkpoint carries no target authority identity')
    return uuid


def _run_migration(job: dict[str, Any], binary: str) -> None:
    """Snapshot → init → import → flip .env.  Source stays read-only."""
    global _migration_job
    authority = _authority_dir()
    checkpoint = Path(data_root()) / _CHECKPOINT_NAME
    try:
        _set_phase(job, 'snapshot')
        from lib.storage.service import get_storage_client
        from runtime_guards import storage_backup_timeout_seconds

        client = get_storage_client(write=True)
        summary = client.maintenance(
            'system.backup',
            deadline=float(storage_backup_timeout_seconds()))
        if (not isinstance(summary, dict) or summary.get('ok') is not True
                or not summary.get('backup')):
            raise SwitchError('snapshot_failed', 'verified backup returned an invalid result')
        source = str(resolve_authority_path(str(summary['backup'])))
        job['snapshotBytes'] = int(summary.get('bytes') or 0)

        def _init_authority() -> str:
            init = subprocess.run(
                [binary, 'init', '--data-dir', str(authority)],
                capture_output=True, text=True, timeout=300)
            if init.returncode != 0:
                raise SwitchError(
                    'init_failed',
                    f'tofu-db init failed: {(init.stderr or init.stdout)[:300]}')
            match = _AUTHORITY_UUID_RE.search(init.stdout or '')
            if not match:
                raise SwitchError(
                    'init_failed', 'tofu-db init reported no authority_uuid')
            return match.group(1)

        authority_present = authority.is_dir() and any(authority.iterdir())
        if not authority_present:
            _set_phase(job, 'init')
            authority_uuid = _init_authority()
        elif checkpoint.is_file():
            # Resume mode: the checkpoint journal binds source and target;
            # the import re-verifies the restored prefix before continuing.
            authority_uuid = _uuid_from_checkpoint(checkpoint)
        elif _is_empty_initialized_authority(authority):
            # Crash window between init and import: no user content exists,
            # so re-initializing loses nothing.
            _set_phase(job, 'init')
            import shutil as _shutil

            _shutil.rmtree(authority)
            authority_uuid = _init_authority()
        else:
            raise SwitchError(
                'authority_not_empty',
                'a TofuDB authority with user content already exists')
        _set_phase(job, 'import')
        from lib.storage.supervisor import _tofudb_scope_ids

        owner_id, tenant_id = _tofudb_scope_ids()
        command = [
            sys.executable, '-m',
            'lib.storage_authority_sql.migration.tofudb_migration_import',
            '--source', source,
            '--checkpoint', str(checkpoint),
            '--binary', binary,
            '--target', str(authority),
            '--owner-id', str(owner_id),
            '--tenant-id', str(tenant_id or 1),
        ]
        if authority_uuid:
            command += ['--authority-uuid', authority_uuid]
        result = subprocess.run(
            command, cwd=str(_REPO_ROOT), capture_output=True, text=True,
            timeout=_IMPORT_TIMEOUT_S)
        if result.returncode != 0:
            with _lock:
                job['resumeSupported'] = checkpoint.is_file()
            raise SwitchError(
                'import_failed',
                f'migration import exited {result.returncode}: '
                f'{(result.stderr or result.stdout)[-400:]}')
        _set_phase(job, 'switch')
        _write_backend_env('tofudb')
        with _lock:
            job['state'] = 'done'
            job['finishedAt'] = time.time()
        logger.info('[DatabasePanel] sqlite→tofudb migration complete')
    except Exception as exc:  # noqa: BLE001 - job failure must surface
        logger.warning('[DatabasePanel] migration failed: %s: %s',
                       type(exc).__name__, str(exc)[:400])
        with _lock:
            job['state'] = 'failed'
            job['error'] = (f'{getattr(exc, "code", type(exc).__name__)}: '
                            f'{str(exc)[:400]}')
            job['finishedAt'] = time.time()
            job['resumeSupported'] = (
                job.get('resumeSupported') or checkpoint.is_file())


def request_switch(target: str, *, migrate: bool = False) -> dict[str, Any]:
    """Validate and apply a backend switch; returns restart instructions."""
    global _migration_job
    from lib.database_panel.probe import configured_backend, resolve_tofudb_binary

    target = (target or '').strip().lower()
    if target not in _VALID_BACKENDS:
        raise SwitchError('invalid_backend', f'unknown backend: {target!r}')
    current = configured_backend()
    if target == current:
        raise SwitchError('already_active', f'{target} is already active')

    snapshot = _job_snapshot()
    if snapshot is not None and snapshot['state'] == 'running':
        raise SwitchError('migration_running', 'a migration is already running')

    notices: list[str] = []
    if target == 'tofudb':
        binary = resolve_tofudb_binary()
        if binary is None:
            raise SwitchError(
                'engine_missing',
                'tofu-db engine binary is not installed on this deployment')
        snapshot = describe(full=True)
        sqlite_has_data = snapshot['sqlite']['hasData']
        authority_ready = snapshot['tofudb']['hasData']
        checkpoint = Path(data_root()) / _CHECKPOINT_NAME
        # Migration covers three shapes: no authority, an init-only empty
        # authority (safe to re-init), and a checkpoint-bound resume.  An
        # authority holding user content is always reused, never migrated
        # over.
        if migrate and sqlite_has_data and (
                not authority_ready or checkpoint.is_file()):
            job = {
                'id': uuid.uuid4().hex[:12],
                'state': 'running',
                'phase': 'prepare',
                'startedAt': time.time(),
                'finishedAt': 0.0,
                'error': '',
                'resumeSupported': False,
            }
            with _lock:
                _migration_job = job
            threading.Thread(
                target=_run_migration, args=(job, binary),
                name='database-panel-migration', daemon=True).start()
            return {
                'ok': True, 'backend': target, 'restartRequired': False,
                'migration': 'running', 'migrationId': job['id'],
                'notices': ['migration_then_restart'],
            }
        if authority_ready:
            notices.append('tofudb_authority_reused')
        elif sqlite_has_data:
            notices.append('sqlite_data_not_migrated')
        _write_backend_env('tofudb')
        return {
            'ok': True, 'backend': target, 'restartRequired': True,
            'migration': 'none', 'notices': notices,
        }

    # target == 'sqlite': no TofuDB→SQLite export exists; the native
    # authority stays untouched so switching back restores it.
    if describe(full=True)['tofudb']['hasData']:
        notices.append('tofudb_data_retained')
    _write_backend_env('sqlite')
    return {
        'ok': True, 'backend': target, 'restartRequired': True,
        'migration': 'none', 'notices': notices,
    }
