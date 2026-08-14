"""Protected storage maintenance commands; all path access stays in sidecar."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import sys

from lib.storage import StorageSupervisor
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.preflight import ProjectLease


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='storagectl')
    parser.add_argument('--backend', choices=('sqlite', 'postgres'), default=None)
    parser.add_argument('--project-root', type=Path, default=None)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name in ('preflight', 'status', 'backup', 'integrity-check'):
        subparsers.add_parser(name)
    restore = subparsers.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--confirm', action='store_true', required=True)
    handoff = subparsers.add_parser('handoff')
    handoff.add_argument('--target-host', required=True)
    handoff.add_argument('--force', action='store_true')
    handoff.add_argument('--reason', default='')
    return parser


def _configure(args) -> SidecarConfig:
    os.environ.setdefault('TOFU_STORAGE_TOKEN', secrets.token_urlsafe(48))
    if args.backend:
        os.environ['TOFU_DB_BACKEND'] = args.backend
    if args.project_root:
        os.environ['TOFU_STORAGE_PROJECT_ROOT'] = str(args.project_root.resolve())
        os.environ['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
    return SidecarConfig.from_environment()


def _online(config: SidecarConfig, command: str) -> dict:
    with StorageSupervisor(
        project_root=config.project_root,
        backend=config.backend,
        startup_timeout=30,
    ) as supervisor:
        if command == 'status':
            return supervisor.client.health()
        if command == 'preflight':
            return supervisor.client.maintenance('system.preflight', deadline=30)
        if command == 'integrity-check':
            return supervisor.client.maintenance('system.integrity_check', deadline=60)
        if command == 'backup':
            return supervisor.client.maintenance('system.backup', deadline=3600)
    raise RuntimeError('unknown online maintenance command')


def _validated_backup(config: SidecarConfig, requested: Path) -> Path:
    path = requested.resolve()
    backup_root = (config.data_dir / 'backups').resolve()
    try:
        path.relative_to(backup_root)
    except ValueError as exc:
        raise RuntimeError('backup must be inside the project data/backups directory') from exc
    if not path.exists():
        raise RuntimeError('backup does not exist')
    return path


def _fsync_file(path: Path) -> None:
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _restore_sqlite(config: SidecarConfig, backup: Path) -> dict:
    uri = f'{backup.as_uri()}?mode=ro'
    source = sqlite3.connect(uri, uri=True)
    try:
        result = source.execute('PRAGMA integrity_check').fetchone()[0]
    finally:
        source.close()
    if result != 'ok':
        raise RuntimeError('backup failed SQLite integrity_check')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    candidate = config.data_dir / f'.tofu-restore-{stamp}.new'
    previous = config.data_dir / 'backups' / f'pre-restore-{stamp}.sqlite3'
    shutil.copy2(backup, candidate)
    _fsync_file(candidate)
    moved_current = False
    moved_sidecars: list[tuple[Path, Path]] = []
    try:
        if config.sqlite_path.exists():
            os.replace(config.sqlite_path, previous)
            moved_current = True
        for suffix in ('-wal', '-shm'):
            active_sidecar = config.sqlite_path.with_name(
                config.sqlite_path.name + suffix)
            archived_sidecar = previous.with_name(previous.name + suffix)
            if active_sidecar.exists():
                os.replace(active_sidecar, archived_sidecar)
                moved_sidecars.append((active_sidecar, archived_sidecar))
        os.replace(candidate, config.sqlite_path)
        _fsync_file(config.sqlite_path)
    except BaseException:
        candidate.unlink(missing_ok=True)
        if moved_current and previous.exists() and not config.sqlite_path.exists():
            os.replace(previous, config.sqlite_path)
        for active_sidecar, archived_sidecar in moved_sidecars:
            if archived_sidecar.exists() and not active_sidecar.exists():
                os.replace(archived_sidecar, active_sidecar)
        raise
    return {
        'ok': True,
        'restored': str(backup.relative_to(config.project_root)),
        'previous': str(previous.relative_to(config.project_root)) if moved_current else None,
    }


def _pg_pid_is_live(pgdata: Path) -> bool:
    pid_path = pgdata / 'postmaster.pid'
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding='utf-8').splitlines()[0])
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, IndexError) as exc:
        raise RuntimeError('cannot prove that the PostgreSQL cluster is stopped') from exc


def _restore_postgres(config: SidecarConfig, backup: Path) -> dict:
    if not backup.is_dir() or not (backup / 'PG_VERSION').is_file():
        raise RuntimeError('invalid PostgreSQL base backup')
    if _pg_pid_is_live(config.pgdata):
        raise RuntimeError('PostgreSQL is still running; refusing restore')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    candidate = config.data_dir / f'.pgdata-restore-{stamp}.new'
    previous = config.data_dir / 'backups' / f'pre-restore-pgdata-{stamp}'
    shutil.copytree(backup, candidate, copy_function=shutil.copy2)
    moved_current = False
    try:
        if config.pgdata.exists():
            os.replace(config.pgdata, previous)
            moved_current = True
        os.replace(candidate, config.pgdata)
    except BaseException:
        if candidate.exists():
            shutil.rmtree(candidate)
        if moved_current and previous.exists() and not config.pgdata.exists():
            os.replace(previous, config.pgdata)
        raise
    return {
        'ok': True,
        'restored': str(backup.relative_to(config.project_root)),
        'previous': str(previous.relative_to(config.project_root)) if moved_current else None,
    }


def _restore(config: SidecarConfig, requested: Path) -> dict:
    backup = _validated_backup(config, requested)
    lease = ProjectLease(config.data_dir)
    lease.acquire()
    try:
        if config.backend == 'sqlite':
            return _restore_sqlite(config, backup)
        return _restore_postgres(config, backup)
    finally:
        lease.release()


def _handoff(
    config: SidecarConfig,
    target_host: str,
    force: bool,
    reason: str,
) -> dict:
    if force and not reason.strip():
        raise RuntimeError('forced handoff requires an audit reason')
    lease = ProjectLease(config.data_dir)
    lease.acquire()
    try:
        record = {
            'at': datetime.now(timezone.utc).isoformat(),
            'from_host': socket.gethostname(),
            'target_host': target_host,
            'backend': config.backend,
            'forced': bool(force),
            'reason': reason.strip() if force else '',
            'actor_pid': os.getpid(),
        }
        audit = config.data_dir / 'storage-handoff-audit.jsonl'
        with audit.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, separators=(',', ':')) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        return {'ok': True, 'handoff': record}
    finally:
        lease.release()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = _configure(args)
        if args.command in {'preflight', 'status', 'backup', 'integrity-check'}:
            result = _online(config, args.command)
        elif args.command == 'restore':
            result = _restore(config, args.backup)
        else:
            result = _handoff(
                config, args.target_host, args.force, args.reason)
        print(json.dumps(result, separators=(',', ':'), sort_keys=True))
        return 0
    except BaseException as exc:
        print(json.dumps({
            'ok': False,
            'error': type(exc).__name__,
            'message': str(exc),
        }, separators=(',', ':'), sort_keys=True), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
