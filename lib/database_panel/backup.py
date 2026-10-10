"""Dual-path verified backup for the database panel.

The selected authority owns the consistent snapshot (the same
``system.backup`` semantic operation the scheduler's daily task uses);
this module mirrors the verified artifact + manifest to a second,
user-chosen directory — the "stable path" half of the dual-path topology.
Only files this module previously copied are ever pruned (tracked in a
small local ledger), never anything else in the target directory.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import threading
import time
from typing import Any

from lib.json_store import read_json, write_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_LEDGER_NAME = 'tofu-panel-backups.json'
_KEEP_COPIES = 2

_lock = threading.Lock()


class BackupError(RuntimeError):
    """Typed backup failure; ``code`` is stable for the UI."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _copy_durable(source: Path, target_dir: Path) -> Path:
    target = target_dir / source.name
    temporary = target_dir / (source.name + '.tmp-copy')
    shutil.copyfile(source, temporary)
    with open(temporary, 'rb') as handle:
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    return target


def _load_ledger(target_dir: Path) -> list[dict[str, Any]]:
    raw = read_json(str(target_dir / _LEDGER_NAME), default=[])
    return raw if isinstance(raw, list) else []


def _prune(target_dir: Path, ledger: list[dict[str, Any]]) -> None:
    """Keep the newest _KEEP_COPIES ledger entries; delete only our files."""
    excess = ledger[:max(0, len(ledger) - _KEEP_COPIES)]
    keep = ledger[max(0, len(ledger) - _KEEP_COPIES):]
    for entry in excess:
        for name in entry.get('files', []):
            candidate = target_dir / name
            # Never follow symlinks or leave the target directory.
            if candidate.parent != target_dir or candidate.is_symlink():
                continue
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
    write_json_atomic(
        str(target_dir / _LEDGER_NAME), keep, mode=0o600)


def run_backup(directory: str | None = None) -> dict[str, Any]:
    """Snapshot via the authority and mirror verified files to ``directory``."""
    from lib.database_panel import config_store

    if not _lock.acquire(blocking=False):
        raise BackupError('backup_busy', 'a backup is already running')
    try:
        preferences = config_store.load_preferences()
        target_raw = (directory or '').strip() or preferences['backup']['directory']
        if not target_raw:
            raise BackupError('no_directory', 'no backup directory configured')
        target_dir = Path(target_raw).expanduser()
        if not target_dir.is_absolute():
            raise BackupError('invalid_directory', 'backup directory must be absolute')
        try:
            target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(
                'invalid_directory', f'cannot create backup directory: {exc}') from exc

        from lib.storage.service import get_storage_client
        from runtime_guards import storage_backup_timeout_seconds

        client = get_storage_client(write=True)
        summary = client.maintenance(
            'system.backup',
            deadline=float(storage_backup_timeout_seconds()))
        if (not isinstance(summary, dict) or summary.get('ok') is not True):
            raise BackupError(
                'snapshot_failed',
                str(summary.get('error') or 'authority backup failed')[:300]
                if isinstance(summary, dict) else 'authority backup failed')
        from lib.database_panel.switch import resolve_authority_path

        artifact = resolve_authority_path(str(summary.get('backup') or ''))
        manifest = resolve_authority_path(str(summary.get('manifest') or ''))
        if summary.get('artifact_kind') == 'directory' or artifact.is_dir():
            # Native (TofuDB) online backup: the artifact is a directory of
            # immutable content-addressed blocks plus snapshot manifests.
            if not artifact.is_dir() or not manifest.is_file():
                raise BackupError(
                    'snapshot_failed', 'authority backup returned missing files')
            size = int(summary.get('bytes') or 0)
            destination = target_dir / f'tofudb-backup-{int(time.time())}'
            try:
                shutil.copytree(
                    artifact,
                    destination,
                    ignore=shutil.ignore_patterns('gc-runs', '.new-*'),
                )
            except OSError as exc:
                raise BackupError(
                    'snapshot_failed', f'native backup mirror failed: {exc}') from exc
            _fsync_dir(destination / 'snapshots')
            _fsync_dir(destination)
            _fsync_dir(target_dir)
            ledger = _load_ledger(target_dir)
            ledger.append({'at': time.time(), 'bytes': size,
                           'files': [destination.name]})
            _prune(target_dir, ledger)
            result = {
                'ok': True,
                'at': time.time(),
                'backend': summary.get('backend') or 'tofudb',
                'bytes': size,
                'files': [destination.name],
                'target': str(target_dir),
                'directory': str(target_dir),
                'error': '',
                'sha256': summary.get('sha256'),
            }
            config_store.record_last_backup(result)
            logger.info('[DatabasePanel] dual-path backup mirrored to %s (%d bytes)',
                        target_dir, size)
            return result
        if not artifact.is_file() or not manifest.is_file():
            raise BackupError(
                'snapshot_failed', 'authority backup returned missing files')
        size = int(summary.get('bytes') or 0)
        free = shutil.disk_usage(target_dir).free
        needed = artifact.stat().st_size + manifest.stat().st_size
        if free < needed + 64 * 1024 * 1024:
            raise BackupError(
                'insufficient_space',
                f'backup target has {free} bytes free, needs {needed}')

        copied = [
            _copy_durable(artifact, target_dir).name,
            _copy_durable(manifest, target_dir).name,
        ]
        _fsync_dir(target_dir)
        ledger = _load_ledger(target_dir)
        ledger.append({'at': time.time(), 'bytes': size, 'files': copied})
        _prune(target_dir, ledger)
        result = {
            'ok': True, 'at': time.time(), 'bytes': size,
            'directory': str(target_dir), 'files': copied, 'error': '',
        }
        config_store.record_last_backup(result)
        logger.info('[DatabasePanel] dual-path backup mirrored to %s (%d bytes)',
                    target_dir, size)
        return result
    except BackupError as exc:
        config_store.record_last_backup({
            'ok': False, 'at': time.time(), 'bytes': 0,
            'directory': directory or '', 'error': f'{exc.code}: {exc}',
        })
        raise
    except Exception as exc:  # noqa: BLE001
        config_store.record_last_backup({
            'ok': False, 'at': time.time(), 'bytes': 0,
            'directory': directory or '',
            'error': f'{type(exc).__name__}: {str(exc)[:200]}',
        })
        raise BackupError(
            'backup_failed', f'{type(exc).__name__}: {str(exc)[:300]}') from exc
    finally:
        _lock.release()
