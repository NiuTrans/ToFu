"""Protected storage maintenance commands; all path access stays in sidecar."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sqlite3
import stat
import sys
import time
import uuid

from lib.storage import StorageSupervisor
from lib.storage_sidecar.config import SidecarConfig
from lib.storage_sidecar.backup_policy import (
    capacity_preflight,
    cleanup_job_artifacts,
    job_manifest_path,
    prune_verified_backups,
    reclaim_stale_job_artifacts,
    write_job_manifest,
)
from lib.storage_sidecar.preflight import ProjectLease
from lib.storage_sidecar.durability import (
    fsync_directory, fsync_file, load_manifest,
    sha256_file, write_json_durable,
)
from runtime_guards import storage_backup_timeout_seconds


def _maintenance_timeout_seconds(command: str) -> int:
    """Share the bounded backup budget across online and offline commands."""
    return storage_backup_timeout_seconds() if command == 'backup' else 3600


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='storagectl')
    parser.add_argument('--backend', choices=('sqlite', 'postgres'), default=None)
    parser.add_argument('--project-root', type=Path, default=None)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name in (
            'preflight', 'status', 'backup', 'integrity-check', 'baseline',
            'cutover-check'):
        subparsers.add_parser(name)
    restore = subparsers.add_parser('restore')
    restore.add_argument('backup', type=Path)
    restore.add_argument('--confirm', action='store_true', required=True)
    retire_fastpath = subparsers.add_parser('retire-fastpath')
    retire_fastpath.add_argument(
        '--confirm', action='store_true', required=True)
    handoff = subparsers.add_parser('handoff')
    handoff.add_argument('--target-host', required=True)
    handoff.add_argument('--force', action='store_true')
    handoff.add_argument('--reason', default='')
    return parser


def _configure(args) -> SidecarConfig:
    temporary_environment: dict[str, str] = {}
    if 'TOFU_STORAGE_TOKEN' not in os.environ:
        temporary_environment['TOFU_STORAGE_TOKEN'] = secrets.token_urlsafe(48)
    if args.project_root:
        temporary_environment['TOFU_STORAGE_PROJECT_ROOT'] = str(
            args.project_root.resolve())
        temporary_environment['TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE'] = '1'
    if args.backend:
        if not args.project_root:
            raise RuntimeError(
                '--backend is private maintenance authority and requires '
                '--project-root')
        temporary_environment['TOFU_STORAGE_TEST_BACKEND'] = args.backend

    previous_environment = {
        name: os.environ.get(name) for name in temporary_environment}
    try:
        os.environ.update(temporary_environment)
        return SidecarConfig.from_environment()
    finally:
        for name, previous_value in previous_environment.items():
            if previous_value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous_value


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
            return supervisor.client.maintenance(
                'system.backup',
                deadline=float(_maintenance_timeout_seconds(command)),
            )
        if command == 'baseline':
            return supervisor.client.maintenance('system.baseline', deadline=3600)
    raise RuntimeError('unknown online maintenance command')


def _refuse_if_fastpath_shadowed(config: SidecarConfig, command: str) -> None:
    """Raw-file commands must never touch a fastpath-shadowed classic file.

    When the write front runs (or ran) on local disk, ``data/tofu.db`` is a
    STALE pre-fastpath image; the durable truth is the shadow under
    ``data/fastpath-shadow/``.  Reading or replacing the classic file here
    would silently certify/migrate/restore the wrong bytes.  Recovery is
    deliberate and documented (docs/TRB-fastpath.md): stop the sidecar,
    decide the lineage, then copy the shadow snapshot+WAL onto the classic
    path (or remove the shadow) before rerunning ``storagectl {command}``.
    """
    from lib.storage_sidecar import fastpath
    manifest = fastpath.read_shadow_manifest(config.data_dir / fastpath.SHADOW_DIRNAME)
    if manifest is not None:
        raise RuntimeError(
            f'fastpath shadow present (generation={manifest.get("generation")}); '
            f'the classic data/tofu.db is stale — refusing raw-file '
            f'{command}; see docs/TRB-fastpath.md')


def _open_offline_sqlite(config: SidecarConfig, deadline_at: float):
    if not config.sqlite_path.is_file():
        raise RuntimeError('SQLite authority does not exist')
    connection = sqlite3.connect(
        f'{config.sqlite_path.as_uri()}?mode=ro', uri=True,
        isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.set_progress_handler(
        lambda: 1 if time.monotonic() >= deadline_at else 0, 10_000)
    return connection


def _offline_sqlite_baseline(config: SidecarConfig, deadline_at: float) -> dict:
    connection = _open_offline_sqlite(config, deadline_at)
    try:
        connection.execute('BEGIN')
        objects = connection.execute(
            "SELECT name, type, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
            'ORDER BY type, name').fetchall()
        tables = []
        table_names = {row['name'] for row in objects if row['type'] == 'table'}
        for name in sorted(table_names):
            if time.monotonic() >= deadline_at:
                raise RuntimeError('SQLite baseline deadline expired')
            identifier = str(name).replace('"', '""')
            count = connection.execute(
                f'SELECT COUNT(*) AS count FROM "{identifier}"').fetchone()
            tables.append({'name': name, 'rows': int(count['count'])})

        versions: dict[str, int | None] = {
            'application': None, 'storage': None,
        }
        if 'schema_meta' in table_names:
            row = connection.execute(
                'SELECT value FROM schema_meta WHERE key = ?',
                ('_schema_version',)).fetchone()
            if row is not None:
                try:
                    versions['application'] = int(row['value'])
                except (TypeError, ValueError):
                    pass
        if 'storage_meta' in table_names:
            row = connection.execute(
                'SELECT meta_value FROM storage_meta WHERE meta_key = ?',
                ('schema_version',)).fetchone()
            if row is not None:
                try:
                    versions['storage'] = int(row['meta_value'])
                except (TypeError, ValueError):
                    pass
        schema_objects = []
        for row in objects:
            item = {
                'name': row['name'], 'type': row['type'],
                'table': row['tbl_name'], 'sql': row['sql'] or '',
            }
            if row['type'] == 'table':
                identifier = str(row['name']).replace('"', '""')
                item['columns'] = [{
                    'position': int(column['cid']), 'name': column['name'],
                    'type': column['type'] or '',
                    'not_null': bool(column['notnull']),
                    'default': column['dflt_value'],
                    'primary_key_position': int(column['pk']),
                } for column in connection.execute(
                    f'PRAGMA table_info("{identifier}")').fetchall()]
                item['foreign_keys'] = [dict(foreign_key) for foreign_key in
                                        connection.execute(
                    f'PRAGMA foreign_key_list("{identifier}")').fetchall()]
            schema_objects.append(item)
        schema_encoded = json.dumps(
            schema_objects, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')
        connection.rollback()
        wal = config.sqlite_path.with_name(config.sqlite_path.name + '-wal')
        return {
            'backend': 'sqlite',
            'schema_version': versions['storage'],
            'schema_versions': versions,
            'tables': tables,
            'indexes': [
                row['name'] for row in objects if row['type'] == 'index'],
            'schema_objects': schema_objects,
            'schema_sha256': hashlib.sha256(schema_encoded).hexdigest(),
            'database_bytes': config.sqlite_path.stat().st_size,
            'wal_bytes': wal.stat().st_size if wal.exists() else 0,
            'captured_at': datetime.now(timezone.utc).isoformat(),
        }
    finally:
        connection.close()


def _offline_sqlite_integrity(config: SidecarConfig, deadline_at: float) -> dict:
    connection = _open_offline_sqlite(config, deadline_at)
    try:
        row = connection.execute('PRAGMA integrity_check').fetchone()
        result = row[0] if row else ''
        return {'ok': result == 'ok', 'result': result, 'backend': 'sqlite'}
    finally:
        connection.close()


def _offline_sqlite_backup(config: SidecarConfig, deadline_at: float) -> dict:
    backups = config.data_dir / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    reclaimed = reclaim_stale_job_artifacts(backups)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target = backups / f'storage-sqlite-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3'
    temporary = backups / f'.{target.name}.tmp-{uuid.uuid4().hex}'
    source = _open_offline_sqlite(config, deadline_at)
    destination = None
    try:
        page_count = int(source.execute('PRAGMA page_count').fetchone()[0])
        page_size = int(source.execute('PRAGMA page_size').fetchone()[0])
        capacity = capacity_preflight(backups, page_count * page_size)
        write_job_manifest(
            temporary,
            source=config.sqlite_path,
            state='copying',
            extra={'estimated_bytes': capacity['estimated_bytes']},
        )
        destination = sqlite3.connect(temporary, isolation_level=None)

        def progress(_status, remaining, total):
            if time.monotonic() >= deadline_at:
                raise RuntimeError('SQLite backup deadline expired')
            if remaining == 0:
                write_job_manifest(
                    temporary,
                    source=config.sqlite_path,
                    state='verifying',
                    extra={
                        'estimated_bytes': capacity['estimated_bytes'],
                        'total_pages': int(total),
                    },
                )

        source.backup(destination, pages=4096, progress=progress, sleep=0.01)
        result = destination.execute('PRAGMA integrity_check').fetchone()[0]
        if result != 'ok':
            raise RuntimeError('SQLite backup failed integrity_check')
        destination.close()
        destination = None
        fsync_file(temporary)
        size = temporary.stat().st_size
        checksum = sha256_file(temporary, deadline_at)
        os.replace(temporary, target)
        fsync_directory(backups)
        manifest = {
            'format': 'tofu.storage-backup.v1', 'backend': 'sqlite',
            'created_at': datetime.now(timezone.utc).isoformat(),
            'artifact': target.name, 'bytes': size, 'sha256': checksum,
            'integrity': 'ok', 'source_mode': 'offline-exclusive',
        }
        manifest_path = target.with_name(target.name + '.manifest.json')
        write_json_durable(manifest_path, manifest)
        pruned = prune_verified_backups(backups, preserve=target)
        return {
            'ok': True,
            'backup': str(target.relative_to(config.project_root)),
            'manifest': str(manifest_path.relative_to(config.project_root)),
            'bytes': size, 'sha256': checksum,
            'estimated_bytes': capacity['estimated_bytes'],
            'recovery_copy_budget_bytes': capacity[
                'recovery_copy_budget_bytes'],
            'retained_recovery_bytes': capacity[
                'retained_recovery_bytes'],
            'projected_recovery_bytes': capacity[
                'projected_recovery_bytes'],
            'same_volume_rollback_bytes': capacity[
                'same_volume_rollback_bytes'],
            'reclaimed_temp_artifacts': reclaimed,
            'pruned': pruned,
        }
    except BaseException:
        cleanup_job_artifacts(temporary)
        if target.exists() and not target.with_name(
                target.name + '.manifest.json').exists():
            target.unlink(missing_ok=True)
        raise
    finally:
        if destination is not None:
            destination.close()
        source.close()
        job_manifest_path(temporary).unlink(missing_ok=True)


def _offline_sqlite_maintenance(
    config: SidecarConfig, command: str,
) -> dict:
    lease = ProjectLease(
        config.data_dir,
        owner_kind='offline_maintenance',
        owner_label=f'Storage {command}',
    )
    lease.acquire()
    try:
        deadline_at = time.monotonic() + _maintenance_timeout_seconds(command)
        if command == 'baseline':
            result = _offline_sqlite_baseline(config, deadline_at)
            reports = config.data_dir / 'storage-maintenance'
            reports.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            path = reports / f'baseline-sqlite-{stamp}-{uuid.uuid4().hex[:8]}.json'
            write_json_durable(path, result)
            return {
                'backend': result['backend'],
                'captured_at': result['captured_at'],
                'database_bytes': result['database_bytes'],
                'index_count': len(result['indexes']),
                'report': str(path.relative_to(config.project_root)),
                'row_count': sum(table['rows'] for table in result['tables']),
                'schema_sha256': result['schema_sha256'],
                'schema_version': result['schema_version'],
                'schema_versions': result['schema_versions'],
                'table_count': len(result['tables']),
                'wal_bytes': result['wal_bytes'],
            }
        if command == 'integrity-check':
            result = _offline_sqlite_integrity(config, deadline_at)
            reports = config.data_dir / 'storage-maintenance'
            reports.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            path = reports / f'integrity-sqlite-{stamp}-{uuid.uuid4().hex[:8]}.json'
            write_json_durable(path, result)
            result['report'] = str(path.relative_to(config.project_root))
            return result
        if command == 'backup':
            return _offline_sqlite_backup(config, deadline_at)
        raise RuntimeError('unknown offline SQLite maintenance command')
    finally:
        lease.release()


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
    manifest = load_manifest(backup)
    if manifest is not None:
        if manifest.get('backend') != 'sqlite':
            raise RuntimeError('backup manifest backend does not match SQLite')
        actual_size = backup.stat().st_size
        if actual_size != int(manifest.get('bytes') or -1):
            raise RuntimeError('backup size does not match checksum manifest')
        actual_hash = sha256_file(backup, time.monotonic() + 3600)
        if actual_hash != manifest.get('sha256'):
            raise RuntimeError('backup checksum does not match manifest')
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
        fsync_directory(config.data_dir)
        fsync_directory(previous.parent)
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


def _restore(config: SidecarConfig, requested: Path) -> dict:
    if config.backend != 'sqlite':
        raise RuntimeError(
            'external PostgreSQL restore is platform-managed and cannot run '
            'through an application maintenance process')
    backup = _validated_backup(config, requested)
    lease = ProjectLease(
        config.data_dir,
        owner_kind='offline_maintenance',
        owner_label='Storage restore',
    )
    lease.acquire()
    try:
        return _restore_sqlite(config, backup)
    finally:
        lease.release()


def _retire_fastpath_sqlite(config: SidecarConfig) -> dict:
    """Materialize the durable shadow as the ordinary SQLite authority.

    The caller owns the offline project lease.  Existing classic bytes and the
    complete shadow are renamed into ``data/backups`` for rollback; no durable
    user state is deleted.  The candidate is fully WAL-checkpointed and
    integrity-checked before publication.
    """
    from lib.storage_sidecar import fastpath

    shadow_dir = config.data_dir / fastpath.SHADOW_DIRNAME
    manifest = fastpath.read_shadow_manifest(shadow_dir)
    if manifest is None:
        raise RuntimeError('no fastpath durable shadow exists to retire')
    if shadow_dir.is_symlink() or not shadow_dir.is_dir():
        raise RuntimeError('fastpath shadow must be a real project directory')
    snapshot, shadow_wal = fastpath.shadow_paths(shadow_dir)
    if not snapshot.is_file():
        raise RuntimeError('fastpath shadow snapshot is missing')
    matching_fronts = fastpath.matching_local_fronts(config.data_dir)
    if len(matching_fronts) > 1:
        choices = ', '.join(str(path) for path in matching_fronts)
        raise RuntimeError(
            f'multiple verified fastpath fronts exist; refusing to choose: '
            f'{choices}')
    if matching_fronts:
        source_database = matching_fronts[0]
        source_wal = source_database.with_name(source_database.name + '-wal')
        source_kind = 'verified_local_front'
    else:
        source_database = snapshot
        source_wal = shadow_wal
        source_kind = 'durable_shadow'

    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    candidate = config.data_dir / f'.tofu-fastpath-retire-{stamp}.new'
    backups = config.data_dir / 'backups'
    backups.mkdir(parents=True, exist_ok=True)
    previous = backups / f'pre-fastpath-retire-{stamp}.sqlite3'
    retired_shadow = backups / f'fastpath-shadow-retired-{stamp}'
    restore_temporary = candidate.with_name(candidate.name + '.restore-tmp')
    owned_paths = (
        candidate,
        candidate.with_name(candidate.name + '-wal'),
        candidate.with_name(candidate.name + '-shm'),
        restore_temporary,
        restore_temporary.with_name(restore_temporary.name + '-wal'),
        previous,
        previous.with_name(previous.name + '-wal'),
        previous.with_name(previous.name + '-shm'),
        retired_shadow,
    )
    collisions = [path.name for path in owned_paths if path.exists()]
    if collisions:
        raise RuntimeError(
            'fastpath retirement artifact collision: ' + ', '.join(collisions))

    try:
        fastpath._restore_from_shadow(
            candidate, source_database, source_wal, manifest)
        connection = sqlite3.connect(candidate, isolation_level=None)
        try:
            connection.execute('PRAGMA busy_timeout=30000')
            checkpoint = connection.execute(
                'PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise RuntimeError('fastpath retirement WAL checkpoint was busy')
            integrity = connection.execute(
                'PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError(
                    f'fastpath retirement candidate failed integrity_check: '
                    f'{integrity}')
            row = connection.execute(
                "SELECT meta_value FROM storage_meta WHERE meta_key = ?",
                ('authority_uuid',),
            ).fetchone()
            expected_uuid = str(manifest.get('authority_uuid') or '')
            if expected_uuid and (not row or str(row[0]) != expected_uuid):
                raise RuntimeError(
                    'fastpath retirement candidate authority UUID mismatch')
        finally:
            connection.close()
        for suffix in ('-wal', '-shm'):
            sidecar = candidate.with_name(candidate.name + suffix)
            if sidecar.exists() and sidecar.stat().st_size:
                raise RuntimeError(
                    f'fastpath retirement left non-empty {sidecar.name}')
            sidecar.unlink(missing_ok=True)
        fsync_file(candidate)
    except BaseException:
        for path in (
                candidate,
                candidate.with_name(candidate.name + '-wal'),
                candidate.with_name(candidate.name + '-shm'),
                restore_temporary,
                restore_temporary.with_name(restore_temporary.name + '-wal')):
            path.unlink(missing_ok=True)
        raise

    moved_previous = False
    installed_candidate = False
    moved_shadow = False
    moved_classic_sidecars: list[tuple[Path, Path]] = []
    try:
        if config.sqlite_path.exists():
            os.replace(config.sqlite_path, previous)
            moved_previous = True
        for suffix in ('-wal', '-shm'):
            stale = config.sqlite_path.with_name(config.sqlite_path.name + suffix)
            if stale.exists():
                archived = previous.with_name(previous.name + suffix)
                os.replace(stale, archived)
                moved_classic_sidecars.append((stale, archived))
        os.replace(candidate, config.sqlite_path)
        installed_candidate = True
        os.replace(shadow_dir, retired_shadow)
        moved_shadow = True
        fsync_file(config.sqlite_path)
        fsync_directory(config.data_dir)
        fsync_directory(backups)
    except BaseException:
        if moved_shadow and retired_shadow.exists() and not shadow_dir.exists():
            os.replace(retired_shadow, shadow_dir)
        if installed_candidate and config.sqlite_path.exists():
            os.replace(config.sqlite_path, candidate)
        if moved_previous and previous.exists() and not config.sqlite_path.exists():
            os.replace(previous, config.sqlite_path)
        for active, archived in moved_classic_sidecars:
            if archived.exists() and not active.exists():
                os.replace(archived, active)
        fsync_directory(config.data_dir)
        fsync_directory(backups)
        raise

    reclaimed_private_bytes = 0
    reclaimed_private_files: list[str] = []
    for private_name in (
            f'{fastpath.SNAPSHOT_NAME}.rebase-tmp',
            f'{fastpath.SNAPSHOT_NAME}.rebase-state.json',
            f'{fastpath.SNAPSHOT_NAME}.rebase-state.json.new'):
        private_path = retired_shadow / private_name
        try:
            private_status = private_path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(private_status.st_mode):
            continue
        private_path.unlink()
        reclaimed_private_bytes += max(0, int(private_status.st_size))
        reclaimed_private_files.append(private_name)
    if reclaimed_private_files:
        fsync_directory(retired_shadow)

    return {
        'ok': True,
        'authority': str(config.sqlite_path.relative_to(config.project_root)),
        'authority_uuid': str(manifest.get('authority_uuid') or ''),
        'generation': manifest.get('generation'),
        'source': source_kind,
        'previous': (
            str(previous.relative_to(config.project_root))
            if moved_previous else None),
        'retired_shadow': str(retired_shadow.relative_to(config.project_root)),
        'reclaimed_private_bytes': reclaimed_private_bytes,
        'reclaimed_private_files': reclaimed_private_files,
        'next_step': 'set TOFU_STORAGE_FASTPATH=off before starting Tofu',
    }


def _retire_fastpath(config: SidecarConfig) -> dict:
    if config.backend != 'sqlite':
        raise RuntimeError('fastpath retirement applies only to SQLite')
    lease = ProjectLease(
        config.data_dir,
        owner_kind='offline_maintenance',
        owner_label='Fastpath retirement',
    )
    lease.acquire()
    try:
        return _retire_fastpath_sqlite(config)
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
    lease = ProjectLease(
        config.data_dir,
        owner_kind='offline_maintenance',
        owner_label='Storage handoff',
    )
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
        if args.command in {'restore', 'handoff'} or (
                config.backend == 'sqlite' and args.command in {
                    'backup', 'integrity-check', 'baseline'}):
            _refuse_if_fastpath_shadowed(config, args.command)
        if args.command == 'cutover-check':
            from lib.storage_boundary import cutover_report
            result = cutover_report(config.project_root)
        elif (config.backend == 'sqlite'
              and args.command in {'backup', 'integrity-check', 'baseline'}):
            result = _offline_sqlite_maintenance(config, args.command)
        elif args.command in {
                'preflight', 'status', 'backup', 'integrity-check', 'baseline'}:
            result = _online(config, args.command)
        elif args.command == 'restore':
            result = _restore(config, args.backup)
        elif args.command == 'retire-fastpath':
            result = _retire_fastpath(config)
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
