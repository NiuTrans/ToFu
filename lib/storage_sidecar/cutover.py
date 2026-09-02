"""Fail-closed activation boundary for a verified PG→SQLite migration.

Migration and activation are deliberately separate operations.  The migrator
writes only a new candidate and a row-digest report; this module is the small,
auditable maintenance-window operation that archives the stale fallback,
promotes the candidate, and creates the authority attestation checked at every
future forced-SQLite startup while PostgreSQL history still exists.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from lib.log import get_logger


AUTHORITY_FILE = '.tofu_sqlite_authority.json'
CUTOVER_LOCK_DIR = '.tofu_sqlite_cutover.lock'
_LOCK_STALE_S = 60 * 60
logger = get_logger(__name__)


class SQLiteCutoverError(RuntimeError):
    """Raised when migration evidence is incomplete or activation is unsafe."""


def _resolved_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError) as exc:
        logger.debug('cutover path containment check failed: %s', exc)
        return False


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SQLiteCutoverError(f'cannot read JSON evidence {path}: {exc}') from exc
    if not isinstance(value, dict):
        raise SQLiteCutoverError(f'JSON evidence is not an object: {path}')
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _fsync_file_and_dir(path: Path) -> None:
    with path.open('rb') as handle:
        os.fsync(handle.fileno())
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        # Network filesystems can reject directory fsync.  File fsync and the
        # atomic same-directory rename still provide the strongest available
        # boundary on those mounts.
        logger.debug('directory fsync unsupported for %s: %s', path.parent, exc)


def _atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + f'.tmp-{os.getpid()}-{uuid.uuid4().hex}')
    try:
        with temp.open('w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        _fsync_file_and_dir(path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError as exc:
            logger.debug('cutover temp already absent: %s', exc)


def pg_history_exists(pgdata: str | Path, data_dir: str | Path) -> bool:
    """Return whether falling into an old canonical SQLite file is unsafe."""
    pg_path = Path(pgdata)
    data = Path(data_dir)
    probes = (
        pg_path / 'PG_VERSION',
        pg_path / 'postgresql.conf',
        data / 'pgdata' / 'PG_VERSION',
        data / 'pgdata' / 'postgresql.conf',
    )
    if any(path.is_file() for path in probes):
        return True
    # Logical/base backups are recoverable PG authority history too.  Keep the
    # probe bounded to known artifact names rather than recursively scanning a
    # potentially hundreds-of-GiB backup tree at import time.
    for name in ('pg_backup.sql', 'chatui_pre_rename_backup.sql'):
        artifact = data / name
        if artifact.is_file() and artifact.stat().st_size:
            return True
    for name in ('pg_backups', 'pg_emergency_backup'):
        artifact_dir = data / name
        if artifact_dir.is_dir():
            try:
                if any(path.is_file() and path.stat().st_size
                       for path in artifact_dir.iterdir()):
                    return True
            except OSError as exc:
                # Unreadable recoverable history is not proof of a fresh host;
                # fail closed instead of treating it as absent.
                logger.warning('cannot inspect PostgreSQL history %s: %s',
                               artifact_dir, exc)
                return True
    return False


def _table_evidence_ok(entry: dict) -> bool:
    if entry.get('status') != 'verified':
        return False
    source = entry.get('source') or {}
    target = entry.get('target') or {}
    keys = ('rows', 'xor_sha256', 'sum_sha256')
    return all(source.get(key) == target.get(key) for key in keys)


def validate_candidate(candidate: str | Path, report_path: str | Path,
                       data_dir: str | Path) -> dict:
    """Validate immutable migration evidence without changing either engine."""
    candidate = Path(candidate).resolve()
    report_path = Path(report_path).resolve()
    data_dir = Path(data_dir).resolve()
    for label, path in (('candidate', candidate), ('report', report_path)):
        if not _resolved_within(path, data_dir):
            raise SQLiteCutoverError(
                f'{label} must stay inside the project data directory: {path}')
    if not candidate.is_file():
        raise SQLiteCutoverError(f'candidate database does not exist: {candidate}')
    report = _read_json(report_path)
    if report.get('status') != 'verified' or report.get('cutover_ready') is not True:
        raise SQLiteCutoverError(
            'migration report is not cutover-ready; a live writable-source '
            'snapshot must never be promoted')
    if report.get('selected_tables') != 'all':
        raise SQLiteCutoverError('partial-table migration cannot be activated')
    try:
        reported_target = Path(report['target']).resolve()
    except (KeyError, TypeError, OSError) as exc:
        raise SQLiteCutoverError('migration report has no valid target path') from exc
    if reported_target != candidate:
        raise SQLiteCutoverError(
            f'report target {reported_target} does not match candidate {candidate}')
    if report.get('integrity_check') != 'ok':
        raise SQLiteCutoverError('migration integrity_check is not ok')
    if report.get('cross_reopen_check') != 'ok':
        raise SQLiteCutoverError('migration cross-reopen check is not ok')
    if report.get('foreign_key_check') != 'ok':
        raise SQLiteCutoverError('migration foreign_key_check is not ok')
    tables = report.get('tables')
    if not isinstance(tables, dict) or not tables:
        raise SQLiteCutoverError('migration report contains no table evidence')
    bad_tables = sorted(
        str(name) for name, entry in tables.items()
        if not isinstance(entry, dict) or not _table_evidence_ok(entry))
    if bad_tables:
        raise SQLiteCutoverError(
            f'table digest evidence is incomplete: {bad_tables[:10]}')
    stat = candidate.stat()
    if int(report.get('target_size_bytes', -1)) != stat.st_size:
        raise SQLiteCutoverError('candidate size changed after verification')
    reported_mtime = report.get('target_mtime_ns')
    if reported_mtime is None or int(reported_mtime) != stat.st_mtime_ns:
        raise SQLiteCutoverError('candidate mtime changed after verification')
    for suffix in ('-wal', '-shm'):
        sidecar = Path(str(candidate) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise SQLiteCutoverError(
                f'candidate still depends on non-empty SQLite sidecar: {sidecar}')

    # A tiny independent reopen catches a wrong/non-SQLite path without
    # repeating the full multi-hour integrity scan already attested above.
    uri = candidate.as_uri() + '?mode=ro&immutable=1'
    db = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        if db.execute('PRAGMA schema_version').fetchone() is None:
            raise SQLiteCutoverError('candidate SQLite header is unreadable')
    finally:
        db.close()
    return {
        'ok': True,
        'candidate': str(candidate),
        'report': str(report_path),
        'tables': len(tables),
        'rows': sum(int(entry['source']['rows']) for entry in tables.values()),
        'size_bytes': stat.st_size,
        'report_sha256': _sha256_file(report_path),
        'source_snapshot': report.get('source_snapshot'),
    }


def validate_authority_marker(db_path: str | Path,
                              data_dir: str | Path) -> dict:
    """Validate the persistent attestation used by SQLite startup."""
    db_path = Path(db_path).resolve()
    data_dir = Path(data_dir).resolve()
    marker_path = data_dir / AUTHORITY_FILE
    marker = _read_json(marker_path)
    if marker.get('version') != 1 or marker.get('status') != 'active':
        raise SQLiteCutoverError('SQLite authority marker is not active')
    try:
        marked_db = Path(marker['canonical_path']).resolve()
        report_path = Path(marker['report_path']).resolve()
    except (KeyError, TypeError, OSError) as exc:
        raise SQLiteCutoverError('SQLite authority marker paths are invalid') from exc
    if marked_db != db_path:
        raise SQLiteCutoverError(
            f'authority marker points to {marked_db}, not {db_path}')
    if not _resolved_within(marked_db, data_dir) or not _resolved_within(
            report_path, data_dir):
        raise SQLiteCutoverError('authority evidence escapes project data directory')
    if not db_path.is_file():
        raise SQLiteCutoverError(f'canonical SQLite authority is missing: {db_path}')
    actual_hash = _sha256_file(report_path)
    if actual_hash != marker.get('report_sha256'):
        raise SQLiteCutoverError('migration report changed after activation')
    report = _read_json(report_path)
    if report.get('status') != 'verified' or report.get('cutover_ready') is not True:
        raise SQLiteCutoverError('authority report is no longer cutover-ready')
    return marker


@contextmanager
def _cutover_lock(data_dir: Path):
    lock = data_dir / CUTOVER_LOCK_DIR
    try:
        lock.mkdir()
    except FileExistsError as exc:
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError as exc:
            logger.debug('cannot stat existing cutover lock %s: %s', lock, exc)
            age = 0
        hint = ('; lock is stale and may be reviewed manually'
                if age > _LOCK_STALE_S else '')
        raise SQLiteCutoverError(
            f'another SQLite cutover holds {lock}{hint}') from exc
    try:
        yield
    finally:
        try:
            lock.rmdir()
        except FileNotFoundError as exc:
            logger.debug('cutover lock already absent: %s', exc)


def activate_candidate(*, candidate: str | Path, report_path: str | Path,
                       canonical_path: str | Path, data_dir: str | Path,
                       owner_approved: bool,
                       source_still_read_only: bool) -> dict:
    """Atomically promote a verified candidate, preserving rollback material."""
    if not owner_approved:
        raise SQLiteCutoverError('explicit owner approval is required')
    if not source_still_read_only:
        raise SQLiteCutoverError(
            'PostgreSQL is not still quiesced/read-only at activation time')
    candidate = Path(candidate).resolve()
    report_path = Path(report_path).resolve()
    canonical = Path(canonical_path).resolve()
    data_dir = Path(data_dir).resolve()
    if canonical.parent != data_dir or canonical.name != 'tofu.db':
        raise SQLiteCutoverError(
            f'canonical path must be exactly {data_dir / "tofu.db"}')
    evidence = validate_candidate(candidate, report_path, data_dir)
    if candidate == canonical:
        raise SQLiteCutoverError('candidate is already the canonical path')
    if candidate.parent != data_dir:
        raise SQLiteCutoverError(
            'candidate and canonical DB must share data/ for atomic rename')

    with _cutover_lock(data_dir):
        live_owner = data_dir / '.tofu_db_owner'
        if live_owner.exists() and time.time() - live_owner.stat().st_mtime <= 120:
            raise SQLiteCutoverError(
                'a live SQLite owner marker exists; stop that process first')
        for path in (canonical, candidate):
            for suffix in ('-wal', '-shm'):
                sidecar = Path(str(path) + suffix)
                if sidecar.exists() and sidecar.stat().st_size:
                    raise SQLiteCutoverError(
                        f'non-empty SQLite sidecar prevents safe rename: {sidecar}')

        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        archive = data_dir / f'tofu.db.pre-pg-archive-{stamp}'
        if archive.exists():
            raise SQLiteCutoverError(f'archive collision: {archive}')
        old_moved = False
        candidate_moved = False
        marker_path = data_dir / AUTHORITY_FILE
        try:
            if canonical.exists():
                os.replace(canonical, archive)
                old_moved = True
            os.replace(candidate, canonical)
            candidate_moved = True
            _fsync_file_and_dir(canonical)
            marker = {
                'version': 1,
                'status': 'active',
                'activated_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'canonical_path': str(canonical),
                'report_path': str(report_path),
                'report_sha256': evidence['report_sha256'],
                'source_snapshot': evidence.get('source_snapshot'),
                'tables': evidence['tables'],
                'rows': evidence['rows'],
                'size_bytes_at_activation': evidence['size_bytes'],
                'previous_sqlite_archive': str(archive) if old_moved else None,
                'postgresql_retained_for_rollback': True,
            }
            _atomic_json(marker_path, marker)
            return marker
        except Exception as activation_exc:
            # Keep a failed activation recoverable: put the candidate and old
            # fallback back at their exact pre-call paths when possible.
            rollback_errors = []
            marker_removed = True
            try:
                marker_path.unlink(missing_ok=True)
            except OSError as exc:
                rollback_errors.append(f'marker cleanup: {exc}')
                marker_removed = False
            # If an authority marker cannot be removed, do not put the stale
            # pre-PG fallback back under the canonical name: that marker could
            # then attest the wrong file on next startup. Preserve the promoted
            # candidate at canonical + old file at archive for manual repair.
            candidate_restored = not candidate_moved
            if marker_removed and candidate_moved and canonical.exists():
                try:
                    os.replace(canonical, candidate)
                    candidate_restored = True
                except OSError as exc:
                    rollback_errors.append(f'candidate restore: {exc}')
            # Never overwrite the newly-promoted candidate with the old file
            # if moving it back failed. In that case both valuable copies stay
            # present (candidate at canonical, old at archive) for manual
            # recovery, and startup still fails closed without a marker.
            if (marker_removed and old_moved and archive.exists()
                    and candidate_restored):
                try:
                    os.replace(archive, canonical)
                except OSError as exc:
                    rollback_errors.append(f'old authority restore: {exc}')
            if rollback_errors:
                logger.critical(
                    'SQLite cutover failed and rollback was incomplete: %s',
                    '; '.join(rollback_errors))
                raise SQLiteCutoverError(
                    'activation failed and rollback was incomplete; inspect '
                    f'{canonical}, {candidate}, and {archive}: '
                    + '; '.join(rollback_errors)) from activation_exc
            raise


__all__ = [
    'AUTHORITY_FILE', 'SQLiteCutoverError', 'activate_candidate',
    'pg_history_exists', 'validate_authority_marker', 'validate_candidate',
]
