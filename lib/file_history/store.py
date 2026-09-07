"""Disk-layer primitives for the file-history store.

Serialises to ``<base_path>/.tofu/file-history/`` with four data classes:

* ``snapshots.jsonl`` — authoritative append-only snapshot log. Legacy full
  rows and v2 base-guarded deltas materialize through one reader.
* ``snapshot-tail.json`` — bounded, fingerprint-validated, disposable cache of
  the latest full snapshot and its reverse transition.
* ``tracked.json`` — single-shot persisted set of currently-tracked
  ``rel_path``s plus their latest version number (so we don't have to
  re-scan ``backups/`` on every call).
* ``backups/<sha256(rel)[:2]>/<sha256(rel)>@v<n>`` — copy backup blobs.

Mutations take a per-project ``RLock``; snapshot appends and rewrites also take
the shared JSON-store advisory file lock. Different projects do not contend.
``snapshot_codec`` owns representation only; this module owns filesystem
durability, cache validation, backup lifecycle, and compaction.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from typing import Iterable

from lib.file_history.snapshot_codec import (
    SNAPSHOT_DELTA_ANCHOR_EVERY,
    apply_files_delta,
    build_reverse_files_delta,
    compact_json_bytes,
    encode_snapshot_record,
    materialize_snapshot_record,
)
from lib.log import get_logger
from lib.weak_lock_pool import WeakLockPool

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  Tunables
# ═══════════════════════════════════════════════════════════════════

#: Per-file version cap.  When exceeded we drop the oldest backups
#: except the earliest one (preserves "rewind to start of session").
MAX_VERSIONS_PER_FILE = 20

#: Hard cap on a single backup's size.  Files larger than this are NOT
#: backed up — the snapshot records ``{rel: None}`` so a rewind through
#: that snapshot will leave the file untouched (with a warning).
MAX_BACKUP_SIZE_BYTES = 16 * 1024 * 1024

#: Coarse FileHistory planning budget.  The snapshot-log rewrite threshold is
#: derived from it; backup retention remains governed by version pins/caps so
#: compaction never silently discards a still-addressable undo blob.
SOFT_DISK_BUDGET_BYTES = 256 * 1024 * 1024

#: Rewrite the snapshot log once it alone consumes one quarter of the entire
#: FileHistory soft budget.  A v2 rewrite preserves every retained snapshot,
#: but replaces repeated full maps with periodic anchors plus small deltas.
SNAPSHOT_LOG_REWRITE_BYTES = SOFT_DISK_BUDGET_BYTES // 4

#: Hard bounds for one JSONL record and its disposable latest-snapshot index.
#: Oversized snapshots remain authoritative in JSONL but skip the cache; a
#: record above the larger limit is rejected instead of creating unbounded I/O.
MAX_SNAPSHOT_RECORD_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_TAIL_BYTES = 8 * 1024 * 1024
SNAPSHOT_TAIL_READ_CHUNK_BYTES = 64 * 1024

#: The in-process pinned-version cache eliminates one full log replay per file
#: once version GC begins.  It is LRU-bounded by project and distinct pairs.
MAX_REFERENCE_CACHE_PROJECTS = 8
MAX_REFERENCE_CACHE_PAIRS = 100_000

#: Retained-row target.  At the next ``COMPACT_CHECK_EVERY`` boundary the log
#: rewrites to this many newest snapshots and GCs blobs no survivor pins.
#: Therefore a healthy log is bounded below 2,200 rows with current defaults,
#: while avoiding an O(history) rewrite after every append beyond 2,000.
MAX_SNAPSHOTS = 2000

#: ``make_snapshot`` calls ``maybe_compact_store`` every this-many
#: snapshots (cheap modulo gate; the full size/row scan only runs then).
COMPACT_CHECK_EVERY = 200


# ═══════════════════════════════════════════════════════════════════
#  Per-project lock (mirrors the per-repo RLock pattern)
# ═══════════════════════════════════════════════════════════════════

_PROJECT_LOCKS = WeakLockPool(threading.RLock)

_REFERENCE_CACHE: OrderedDict[str, dict] = OrderedDict()
_REFERENCE_CACHE_MUTEX = threading.Lock()


def _project_lock(base_path: str) -> threading.RLock:
    key = os.path.abspath(base_path)
    return _PROJECT_LOCKS.lock_for(key)


def with_project_lock(f):
    """Serialise mutations to the on-disk store for one project."""
    @functools.wraps(f)
    def wrapper(base_path, *args, **kwargs):
        with _project_lock(base_path):
            return f(base_path, *args, **kwargs)
    return wrapper


# ═══════════════════════════════════════════════════════════════════
#  Path helpers
# ═══════════════════════════════════════════════════════════════════

def store_dir(base_path: str) -> str:
    return os.path.join(os.path.abspath(base_path), '.tofu', 'file-history')


def snapshots_path(base_path: str) -> str:
    return os.path.join(store_dir(base_path), 'snapshots.jsonl')


def tracked_path(base_path: str) -> str:
    return os.path.join(store_dir(base_path), 'tracked.json')


def snapshot_tail_path(base_path: str) -> str:
    """Path to the bounded, reconstructible latest-snapshot index."""
    return os.path.join(store_dir(base_path), 'snapshot-tail.json')


def backups_dir(base_path: str) -> str:
    return os.path.join(store_dir(base_path), 'backups')


def _hash_rel_path(rel_path: str) -> str:
    """Stable filesystem-safe key for a project-relative path."""
    norm = rel_path.replace('\\', '/').lstrip('/').strip()
    return hashlib.sha256(norm.encode('utf-8', 'replace')).hexdigest()


def backup_blob_path(base_path: str, rel_path: str, version: int) -> str:
    h = _hash_rel_path(rel_path)
    return os.path.join(backups_dir(base_path), h[:2], f'{h}@v{int(version)}')


def ensure_store(base_path: str) -> str:
    """Idempotent bootstrap of the on-disk store.  Returns the store dir."""
    sd = store_dir(base_path)
    os.makedirs(os.path.join(sd, 'backups'), exist_ok=True)
    # Touch a marker file so casual ``ls`` sees the dir is intentional.
    readme = os.path.join(sd, 'README.txt')
    if not os.path.exists(readme):
        try:
            with open(readme, 'w', encoding='utf-8') as f:
                f.write(
                    'Tofu file-history store.  Tracks per-file copy backups\n'
                    'so file edits made by the assistant can be undone or\n'
                    'redone round-by-round.  Safe to delete — you will lose\n'
                    'the in-session undo history but your project files are\n'
                    'unaffected.\n')
        except OSError as e:
            logger.debug('[FileHistory] could not create README at %s: %s',
                         readme, e)
    return sd


# ═══════════════════════════════════════════════════════════════════
#  Atomic writes — delegated to lib.json_store
# ═══════════════════════════════════════════════════════════════════

def _atomic_write_bytes(path: str, data: bytes) -> None:
    """Atomically write bytes (file-history backup blobs).

    Kept as a thin wrapper so callers don't need to reach into
    json_store for binary writes — they can stay in this module.
    """
    dn = os.path.dirname(path)
    os.makedirs(dn, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dn, prefix='.fh-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
            # Backup blobs are write-once (re-read only on rewind) — drop the
            # just-written pages from the page cache (shared-cgroup relief).
            try:
                os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
            except (OSError, AttributeError) as e:
                logger.debug('[FileHistory] fadvise DONTNEED skipped: %s', e)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_json(path: str, payload) -> None:
    """Atomically write JSON. Delegates to lib.json_store."""
    from lib.json_store import write_json_atomic
    write_json_atomic(path, payload, fsync=True, indent=2)


def _atomic_write_compact_json(path: str, payload) -> None:
    """Atomically write a bounded reconstructible cache without indentation."""
    from lib.json_store import write_json_atomic
    write_json_atomic(path, payload, fsync=True, indent=None)


# ═══════════════════════════════════════════════════════════════════
#  Tracked-files index
# ═══════════════════════════════════════════════════════════════════

def load_tracked(base_path: str) -> dict:
    """Return ``{rel_path: {latest_version, deleted, mtime, size}}``.

    Empty dict when no store yet.  Caller must hold the project lock.
    """
    p = tracked_path(base_path)
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding='utf-8') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning('[FileHistory] tracked.json malformed (not a dict) at %s — resetting', p)
            return {}
        return data
    except Exception as e:
        logger.warning('[FileHistory] tracked.json corrupt at %s (%s) — resetting', p, e)
        return {}


def save_tracked(base_path: str, tracked: dict) -> None:
    _atomic_write_json(tracked_path(base_path), tracked)


# ═══════════════════════════════════════════════════════════════════
#  Backup helpers
# ═══════════════════════════════════════════════════════════════════

def _stat_or_none(abs_path: str):
    try:
        return os.stat(abs_path)
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as e:
        logger.debug('[FileHistory] stat failed for %s: %s', abs_path, e)
        return None


def _file_sha256(abs_path: str, *, max_bytes: int) -> str | None:
    h = hashlib.sha256()
    n = 0
    try:
        with open(abs_path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                n += len(chunk)
                if n > max_bytes:
                    return None
                h.update(chunk)
        return h.hexdigest()
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as e:
        logger.debug('[FileHistory] sha256 read failed for %s: %s', abs_path, e)
        return None


def _copy_backup(abs_src: str, dst: str) -> bool:
    """Copy ``abs_src`` to ``dst`` atomically (tempfile + rename).

    Returns True on success.  Logs and returns False on failure — never
    raises (the caller treats backup failures as "skip this version").
    """
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dst),
                                   prefix='.fh-blob-', suffix='.tmp')
        os.close(fd)
        shutil.copyfile(abs_src, tmp)
        # The copied blob is write-once — hint its pages out of the cache.
        try:
            from lib.cgroup_guard import fadvise_dontneed
            fadvise_dontneed(tmp)
        except Exception as e:
            logger.debug('[FileHistory] fadvise on copy backup skipped: %s', e)
        os.replace(tmp, dst)
        return True
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError) as e:
        logger.debug('[FileHistory] copy backup %s → %s failed: %s', abs_src, dst, e)
        with contextlib.suppress(OSError):
            os.unlink(tmp)  # type: ignore[name-defined]
        return False


def stage_backup(base_path: str, rel_path: str,
                 *, explicit_content: bytes | str | None = None,
                 task_id: str | None = None) -> int | None:
    """Record the contents of ``rel_path`` as the next version.

    By default reads the current on-disk contents.  ``explicit_content``
    overrides that with caller-provided bytes/str — used when the write
    tool already has the pre-write content in memory and the on-disk
    file has already been overwritten.

    ``task_id`` is recorded as ``last_writer_task_id`` on the tracked
    entry whenever a NEW backup version is created (no-op when the
    version is unchanged).  Used by the orchestrator's fh side-channel
    to filter out file mutations attributable to other concurrent
    tasks on the same project root.

    Returns the version number written, or ``None`` if no backup was
    needed (file unchanged since the last backed-up version) or if
    backup was skipped (file too large, missing, etc.).

    Caller must hold the project lock.
    """
    if explicit_content is not None:
        return _stage_explicit(base_path, rel_path, explicit_content,
                               task_id=task_id)
    abs_p = os.path.join(os.path.abspath(base_path), rel_path)
    st = _stat_or_none(abs_p)
    tracked = load_tracked(base_path)
    entry = tracked.get(rel_path) or {}
    latest = int(entry.get('latest_version') or 0)

    if st is None:
        # File doesn't exist on disk.  Record a tombstone version if the
        # file was previously tracked AND the previous state was "exists".
        if entry.get('deleted'):
            return None
        if latest == 0:
            # Never seen before AND already absent — nothing to record.
            tracked[rel_path] = {
                'latest_version': 0,
                'deleted': True,
                'mtime': 0,
                'size': 0,
                'first_seen': time.time(),
                'last_writer_task_id': task_id or '',
            }
            save_tracked(base_path, tracked)
            return 0
        new_v = latest + 1
        tracked[rel_path] = {
            **entry,
            'latest_version': new_v,
            'deleted': True,
            'mtime': 0,
            'size': 0,
            'last_writer_task_id': task_id or '',
        }
        save_tracked(base_path, tracked)
        return new_v

    if st.st_size > MAX_BACKUP_SIZE_BYTES:
        logger.info('[FileHistory] skipping backup of %s (%d bytes > cap %d)',
                    rel_path, st.st_size, MAX_BACKUP_SIZE_BYTES)
        # Mark tracked but with no blob — rewind through this version
        # will leave the file untouched.
        new_v = latest + 1
        tracked[rel_path] = {
            **entry,
            'latest_version': new_v,
            'deleted': False,
            'mtime': st.st_mtime,
            'size': st.st_size,
            'too_large': True,
            'last_writer_task_id': task_id or '',
        }
        save_tracked(base_path, tracked)
        return new_v

    # Dedup: if mtime+size+sha unchanged from latest version, skip.
    if (latest > 0
            and not entry.get('deleted')
            and not entry.get('too_large')
            and entry.get('size') == st.st_size
            and abs(float(entry.get('mtime') or 0) - st.st_mtime) < 1e-3):
        return None

    new_v = latest + 1
    dst = backup_blob_path(base_path, rel_path, new_v)
    if not _copy_backup(abs_p, dst):
        return None
    tracked[rel_path] = {
        **entry,
        'latest_version': new_v,
        'deleted': False,
        'mtime': st.st_mtime,
        'size': st.st_size,
        'first_seen': entry.get('first_seen') or time.time(),
        'last_writer_task_id': task_id or '',
    }
    save_tracked(base_path, tracked)
    _gc_old_versions(base_path, rel_path, new_v)
    return new_v


def _stage_explicit(base_path: str, rel_path: str,
                    content: bytes | str,
                    *, task_id: str | None = None) -> int | None:
    """Stage a backup blob from caller-provided content.

    Used by ``track_edit(... pre_content=...)`` so write tools can record
    the pre-write snapshot AFTER they've overwritten the file (the
    common case in this codebase — ``_record_modification`` runs after
    the write).  The version is bumped unconditionally; we don't have a
    cheap dedup check (no stat to compare against).  ``mtime``/``size``
    in the tracked index are set to 0 so the next on-disk-driven
    ``stage_backup`` call will re-snapshot if needed.
    """
    if isinstance(content, str):
        data = content.encode('utf-8', 'replace')
    elif isinstance(content, (bytes, bytearray)):
        data = bytes(content)
    else:
        logger.debug('[FileHistory] _stage_explicit: unsupported type %s for %s',
                     type(content).__name__, rel_path)
        return None
    if len(data) > MAX_BACKUP_SIZE_BYTES:
        logger.info('[FileHistory] skipping explicit backup of %s (%d bytes > cap %d)',
                    rel_path, len(data), MAX_BACKUP_SIZE_BYTES)
        return None
    tracked = load_tracked(base_path)
    entry = tracked.get(rel_path) or {}
    new_v = int(entry.get('latest_version') or 0) + 1
    dst = backup_blob_path(base_path, rel_path, new_v)
    try:
        _atomic_write_bytes(dst, data)
    except OSError as e:
        logger.warning('[FileHistory] _stage_explicit write failed for %s@v%d: %s',
                       rel_path, new_v, e)
        return None
    tracked[rel_path] = {
        **entry,
        'latest_version': new_v,
        'deleted': False,
        'mtime': 0,
        'size': len(data),
        'first_seen': entry.get('first_seen') or time.time(),
        'last_writer_task_id': task_id or '',
    }
    save_tracked(base_path, tracked)
    _gc_old_versions(base_path, rel_path, new_v)
    return new_v


def _gc_old_versions(base_path: str, rel_path: str, latest: int) -> None:
    """Delete oldest backup blobs beyond ``MAX_VERSIONS_PER_FILE``.

    Always preserves version 1 if present (so rewind to round 1 stays
    possible), and any reference held by an existing snapshot.
    """
    keep_above = latest - (MAX_VERSIONS_PER_FILE - 1)
    if keep_above <= 1:
        return
    # Find versions actually present on disk for this path.
    h = _hash_rel_path(rel_path)
    bucket = os.path.join(backups_dir(base_path), h[:2])
    if not os.path.isdir(bucket):
        return
    referenced = _versions_referenced_by_snapshots(base_path, rel_path)
    if referenced is None:
        logger.debug(
            '[FileHistory] version GC deferred for %s: snapshot refs uncertain',
            rel_path,
        )
        return
    for name in os.listdir(bucket):
        if not name.startswith(h + '@v'):
            continue
        try:
            v = int(name.rsplit('@v', 1)[-1])
        except ValueError as _e_audit:
            logger.debug('[store] _gc_old_versions caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue
        if v == 1:
            continue
        if v >= keep_above:
            continue
        if v in referenced:
            continue
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(bucket, name))


def _versions_referenced_by_snapshots(
    base_path: str,
    rel_path: str,
) -> set[int] | None:
    """Versions pinned by history, cached once per unchanged project log.

    ``None`` means the scan raced a writer or failed, and instructs GC to fail
    closed.  Returning an empty set on an uncertain read could delete a blob
    that an older retained snapshot still needs.
    """
    cached = _reference_cache_get(base_path)
    if cached is not None:
        return set(cached['references'].get(rel_path, ()))

    fingerprint_before = _snapshot_log_fingerprint(base_path)
    references: dict[str, set[int]] = {}
    target_versions: set[int] = set()
    cacheable = True
    pair_count = 0
    try:
        for snapshot in iter_snapshots(base_path):
            for path, version in (snapshot.get('files') or {}).items():
                if not isinstance(version, int) or version <= 0:
                    continue
                if path == rel_path:
                    target_versions.add(version)
                if not cacheable:
                    continue
                versions = references.setdefault(path, set())
                if version not in versions:
                    versions.add(version)
                    pair_count += 1
                    if pair_count > MAX_REFERENCE_CACHE_PAIRS:
                        references.clear()
                        cacheable = False
        fingerprint_after = _snapshot_log_fingerprint(base_path)
        if fingerprint_before != fingerprint_after:
            return None
        if cacheable:
            _reference_cache_put(base_path, fingerprint_after, references)
        return target_versions
    except Exception as error:
        logger.debug('[FileHistory] snapshot scan for refs failed: %s', error)
        return None


# ═══════════════════════════════════════════════════════════════════
#  Snapshots log (append-only JSONL)
# ═══════════════════════════════════════════════════════════════════

_SNAPSHOT_TAIL_SCHEMA_VERSION = 1


def _snapshot_log_fingerprint(base_path: str) -> dict | None:
    """Identity used to reject stale disposable indexes and caches."""
    try:
        stat = os.stat(snapshots_path(base_path))
    except OSError:
        return None
    return {
        'device': int(stat.st_dev),
        'inode': int(stat.st_ino),
        'size': int(stat.st_size),
        'mtimeNs': int(stat.st_mtime_ns),
    }


def _load_snapshot_tail_index(base_path: str) -> dict | None:
    """Load the latest-snapshot cache only when its log fingerprint matches."""
    path = snapshot_tail_path(base_path)
    try:
        if os.path.getsize(path) > MAX_SNAPSHOT_TAIL_BYTES:
            return None
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get('schemaVersion') != _SNAPSHOT_TAIL_SCHEMA_VERSION:
        return None
    if payload.get('logFingerprint') != _snapshot_log_fingerprint(base_path):
        return None
    latest = payload.get('latest')
    count = payload.get('snapshotCount')
    depth = payload.get('deltaDepth')
    if not isinstance(latest, dict) or not isinstance(latest.get('files'), dict):
        return None
    if not isinstance(latest.get('id'), str) or not latest.get('id'):
        return None
    if not isinstance(count, int) or count <= 0:
        return None
    if not isinstance(depth, int) or not 0 <= depth < SNAPSHOT_DELTA_ANCHOR_EVERY:
        return None
    return payload


def _write_snapshot_tail_index(
    base_path: str,
    *,
    snapshot_count: int,
    latest: dict | None,
    previous: dict | None,
    delta_depth: int,
) -> None:
    """Publish a bounded cache after the authoritative JSONL write is durable."""
    path = snapshot_tail_path(base_path)
    fingerprint = _snapshot_log_fingerprint(base_path)
    if not latest or fingerprint is None or snapshot_count <= 0:
        with contextlib.suppress(OSError):
            os.unlink(path)
        return
    latest_files = latest.get('files')
    if not isinstance(latest_files, dict):
        return
    previous_files = (
        previous.get('files') if isinstance(previous, dict) else None
    )
    previous_id = previous.get('id') if isinstance(previous, dict) else None
    payload = {
        'schemaVersion': _SNAPSHOT_TAIL_SCHEMA_VERSION,
        'logFingerprint': fingerprint,
        'snapshotCount': int(snapshot_count),
        'deltaDepth': int(delta_depth),
        'latest': latest,
        'previousId': previous_id,
        'previousFilesDelta': build_reverse_files_delta(
            previous_files if isinstance(previous_files, dict) else None,
            latest_files,
        ),
    }
    if len(compact_json_bytes(payload)) > MAX_SNAPSHOT_TAIL_BYTES:
        logger.info(
            '[FileHistory] latest-snapshot cache skipped (%d files exceeds %d-byte cap)',
            len(latest_files), MAX_SNAPSHOT_TAIL_BYTES,
        )
        with contextlib.suppress(OSError):
            os.unlink(path)
        return
    try:
        _atomic_write_compact_json(path, payload)
    except Exception as error:
        # The JSONL append is already durable.  A missing cache only makes the
        # next access rebuild; it must never turn a successful snapshot into a
        # phantom failure.
        logger.debug('[FileHistory] snapshot tail cache write skipped: %s', error)


def _iter_stored_snapshot_records(base_path: str) -> Iterable[dict]:
    path = snapshots_path(base_path)
    if not os.path.exists(path):
        return
    try:
        with open(path, 'rb') as handle:
            while True:
                raw = handle.readline(MAX_SNAPSHOT_RECORD_BYTES + 2)
                if not raw:
                    break
                content_size = len(raw.rstrip(b'\r\n'))
                if (
                    content_size > MAX_SNAPSHOT_RECORD_BYTES
                    or (
                        len(raw) == MAX_SNAPSHOT_RECORD_BYTES + 2
                        and not raw.endswith(b'\n')
                    )
                ):
                    # Discard the remainder in bounded chunks.  Iterating a
                    # binary file directly would allocate one attacker-sized
                    # line before the post-read length check could run.
                    while raw and not raw.endswith(b'\n'):
                        raw = handle.readline(65536)
                    logger.warning(
                        '[FileHistory] oversized snapshot line skipped (> %d bytes)',
                        MAX_SNAPSHOT_RECORD_BYTES,
                    )
                    continue
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    stored = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError) as error:
                    logger.debug(
                        '[FileHistory] malformed snapshot line skipped: %s', error,
                    )
                    continue
                if isinstance(stored, dict):
                    yield stored
                else:
                    logger.debug('[FileHistory] non-object snapshot line skipped')
    except OSError as error:
        logger.warning('[FileHistory] could not read %s: %s', path, error)


def _iter_materialized_snapshot_entries(
    base_path: str,
) -> Iterable[tuple[dict, int]]:
    """Yield full public records plus their current delta-chain depth."""
    base_id: str | None = None
    base_files: dict | None = None
    delta_depth = 0
    for stored in _iter_stored_snapshot_records(base_path):
        decoded = materialize_snapshot_record(
            stored, base_id, base_files, delta_depth,
        )
        if decoded is None:
            logger.debug(
                '[FileHistory] snapshot delta rejected id=%s base=%s',
                stored.get('id'), base_id,
            )
            continue
        materialized, base_id, base_files, delta_depth = decoded
        yield materialized, delta_depth


def iter_snapshots(base_path: str) -> Iterable[dict]:
    """Yield materialized snapshots oldest-first; malformed chains fail closed."""
    for snapshot, _depth in _iter_materialized_snapshot_entries(base_path):
        yield snapshot


def _scan_snapshot_log_summary(base_path: str) -> dict:
    """One bounded-memory replay returning count and the final two snapshots."""
    count = 0
    previous: dict | None = None
    latest: dict | None = None
    delta_depth = 0
    for snapshot, delta_depth in _iter_materialized_snapshot_entries(base_path):
        previous = latest
        latest = snapshot
        count += 1
    return {
        'snapshotCount': count,
        'latest': latest,
        'previous': previous,
        'deltaDepth': delta_depth,
    }


def _iter_snapshot_lines_newest_first(base_path: str) -> Iterable[bytes]:
    """Read JSONL backwards in 64 KiB chunks, bounded by one record."""
    path = snapshots_path(base_path)
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return
        with open(path, 'rb') as handle:
            position = size
            partial = b''
            discarding_oversized_line = False
            while position > 0:
                chunk_size = min(SNAPSHOT_TAIL_READ_CHUNK_BYTES, position)
                position -= chunk_size
                handle.seek(position)
                chunk = handle.read(chunk_size)
                if discarding_oversized_line:
                    separator = chunk.rfind(b'\n')
                    if separator < 0:
                        continue
                    chunk = chunk[:separator]
                    discarding_oversized_line = False
                data = chunk + partial
                while b'\n' in data:
                    data, raw = data.rsplit(b'\n', 1)
                    raw = raw.rstrip(b'\r')
                    if raw and len(raw) <= MAX_SNAPSHOT_RECORD_BYTES:
                        yield raw
                partial = data
                if len(partial) > MAX_SNAPSHOT_RECORD_BYTES:
                    partial = b''
                    discarding_oversized_line = True
            if not discarding_oversized_line:
                partial = partial.rstrip(b'\r')
                if partial and len(partial) <= MAX_SNAPSHOT_RECORD_BYTES:
                    yield partial
    except OSError:
        return


def _read_last_stored_snapshot(base_path: str) -> dict | None:
    """Return the newest valid JSON object from the bounded reverse reader."""
    for raw in _iter_snapshot_lines_newest_first(base_path):
        try:
            candidate = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _read_final_stored_snapshot(base_path: str) -> dict | None:
    """Return the actual final non-empty row, never skipping a torn suffix."""
    for raw in _iter_snapshot_lines_newest_first(base_path):
        try:
            candidate = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return candidate if isinstance(candidate, dict) else None
    return None


def _count_nonempty_snapshot_lines(base_path: str) -> int:
    count = 0
    pending_nonspace = False
    try:
        with open(snapshots_path(base_path), 'rb') as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                for part_index, part in enumerate(chunk.split(b'\n')):
                    if part_index == 0:
                        pending_nonspace = pending_nonspace or bool(part.strip())
                    else:
                        if pending_nonspace:
                            count += 1
                        pending_nonspace = bool(part.strip())
        if pending_nonspace:
            count += 1
    except OSError:
        return 0
    return count


def _load_append_state(base_path: str) -> dict:
    tail = _load_snapshot_tail_index(base_path)
    if tail is not None:
        return {
            'snapshotCount': tail['snapshotCount'],
            'latest': tail['latest'],
            'previous': None,
            'deltaDepth': tail['deltaDepth'],
        }
    # Legacy logs end in a self-contained full row.  Counting raw newlines is
    # far cheaper than parsing hundreds of megabytes merely to append the first
    # v2 delta after an upgrade.
    stored = _read_final_stored_snapshot(base_path)
    if isinstance(stored, dict) and isinstance(stored.get('files'), dict):
        decoded = materialize_snapshot_record(stored, None, None, 0)
        if decoded is not None:
            latest, _snapshot_id, _files, depth = decoded
            return {
                'snapshotCount': _count_nonempty_snapshot_lines(base_path),
                'latest': latest,
                'previous': None,
                'deltaDepth': depth,
            }
    # A missing/stale cache in a v2 chain requires an authoritative replay.
    return _scan_snapshot_log_summary(base_path)


def _reference_cache_get(base_path: str) -> dict | None:
    key = os.path.abspath(base_path)
    fingerprint = _snapshot_log_fingerprint(base_path)
    with _REFERENCE_CACHE_MUTEX:
        entry = _REFERENCE_CACHE.get(key)
        if entry is None or entry.get('fingerprint') != fingerprint:
            _REFERENCE_CACHE.pop(key, None)
            return None
        _REFERENCE_CACHE.move_to_end(key)
        return entry


def _reference_cache_put(
    base_path: str,
    fingerprint: dict | None,
    references: dict[str, set[int]],
) -> None:
    pair_count = sum(len(versions) for versions in references.values())
    if fingerprint is None or pair_count > MAX_REFERENCE_CACHE_PAIRS:
        return
    key = os.path.abspath(base_path)
    entry = {
        'fingerprint': fingerprint,
        'references': references,
        'pairCount': pair_count,
    }
    with _REFERENCE_CACHE_MUTEX:
        _REFERENCE_CACHE[key] = entry
        _REFERENCE_CACHE.move_to_end(key)
        while (
            len(_REFERENCE_CACHE) > MAX_REFERENCE_CACHE_PROJECTS
            or sum(
                int(item.get('pairCount') or 0)
                for item in _REFERENCE_CACHE.values()
            ) > MAX_REFERENCE_CACHE_PAIRS
        ):
            _REFERENCE_CACHE.popitem(last=False)


def _reference_cache_advance(
    base_path: str,
    old_fingerprint: dict | None,
    new_fingerprint: dict | None,
    files: dict,
) -> None:
    key = os.path.abspath(base_path)
    with _REFERENCE_CACHE_MUTEX:
        entry = _REFERENCE_CACHE.get(key)
        if entry is None or entry.get('fingerprint') != old_fingerprint:
            _REFERENCE_CACHE.pop(key, None)
            return
        references = entry['references']
        pair_count = int(entry.get('pairCount') or 0)
        for rel_path, version in files.items():
            if not isinstance(version, int) or version <= 0:
                continue
            versions = references.setdefault(rel_path, set())
            if version not in versions:
                versions.add(version)
                pair_count += 1
        if new_fingerprint is None or pair_count > MAX_REFERENCE_CACHE_PAIRS:
            _REFERENCE_CACHE.pop(key, None)
            return
        entry['fingerprint'] = new_fingerprint
        entry['pairCount'] = pair_count
        _REFERENCE_CACHE.move_to_end(key)
        while (
            len(_REFERENCE_CACHE) > MAX_REFERENCE_CACHE_PROJECTS
            or sum(
                int(item.get('pairCount') or 0)
                for item in _REFERENCE_CACHE.values()
            ) > MAX_REFERENCE_CACHE_PAIRS
        ):
            _REFERENCE_CACHE.popitem(last=False)


@with_project_lock
def append_snapshot_record(base_path: str, record: dict) -> int:
    """Durably append one snapshot and return its 1-based log position.

    The JSONL line is authoritative.  ``snapshot-tail.json`` is published only
    after its fsync and is validated against inode/size/mtime before every use.
    A torn prior line receives a separator before the new row, so one failed
    append cannot poison every later snapshot.
    """
    ensure_store(base_path)
    path = snapshots_path(base_path)
    from lib.json_store import locked_path

    with locked_path(path):
        old_fingerprint = _snapshot_log_fingerprint(base_path)
        state = _load_append_state(base_path)
        previous = state.get('latest')
        stored, delta_depth = encode_snapshot_record(
            record,
            previous if isinstance(previous, dict) else None,
            int(state.get('deltaDepth') or 0),
        )
        line = compact_json_bytes(stored)
        if len(line) > MAX_SNAPSHOT_RECORD_BYTES:
            raise ValueError(
                f'FileHistory snapshot record exceeds {MAX_SNAPSHOT_RECORD_BYTES} bytes'
            )
        needs_separator = False
        try:
            if os.path.getsize(path) > 0:
                with open(path, 'rb') as reader:
                    reader.seek(-1, os.SEEK_END)
                    needs_separator = reader.read(1) != b'\n'
        except OSError:
            pass
        with open(path, 'ab') as handle:
            if needs_separator:
                handle.write(b'\n')
            handle.write(line)
            handle.write(b'\n')
            handle.flush()
            os.fsync(handle.fileno())

        snapshot_count = int(state.get('snapshotCount') or 0) + 1
        new_fingerprint = _snapshot_log_fingerprint(base_path)
        _write_snapshot_tail_index(
            base_path,
            snapshot_count=snapshot_count,
            latest=record,
            previous=previous if isinstance(previous, dict) else None,
            delta_depth=delta_depth,
        )
        files = record.get('files') if isinstance(record, dict) else None
        if isinstance(files, dict):
            _reference_cache_advance(
                base_path, old_fingerprint, new_fingerprint, files,
            )
        return snapshot_count


def latest_snapshot_id(base_path: str) -> str | None:
    """Return the latest materializable id without replaying a legacy log."""
    tail = _load_snapshot_tail_index(base_path)
    if tail is not None:
        return tail['latest'].get('id')
    stored = _read_last_stored_snapshot(base_path)
    if isinstance(stored, dict) and isinstance(stored.get('files'), dict):
        snapshot_id = stored.get('id')
        if isinstance(snapshot_id, str) and snapshot_id:
            return snapshot_id
    summary = _scan_snapshot_log_summary(base_path)
    latest = summary.get('latest')
    return latest.get('id') if isinstance(latest, dict) else None


def find_latest_snapshot_id_by_metadata(
    base_path: str,
    *,
    task_id: str | None = None,
    conv_id: str | None = None,
) -> str | None:
    """Find the newest metadata match without retaining snapshot file maps."""
    if not task_id and not conv_id:
        return None

    def matches(snapshot: dict) -> bool:
        if task_id:
            return snapshot.get('taskId') == task_id
        return snapshot.get('convId') == conv_id

    tail = _load_snapshot_tail_index(base_path)
    if tail is not None and matches(tail['latest']):
        return tail['latest'].get('id')
    found = None
    for snapshot in iter_snapshots(base_path):
        if matches(snapshot):
            found = snapshot.get('id') or found
    return found


def _tail_previous_files(tail: dict) -> dict | None:
    latest = tail.get('latest')
    if not isinstance(latest, dict) or not isinstance(latest.get('files'), dict):
        return None
    return apply_files_delta(
        latest['files'], tail.get('previousFilesDelta'),
    )


def get_snapshot_file_maps(
    base_path: str,
    from_id: str | None,
    to_id: str,
) -> tuple[dict, dict] | None:
    """Resolve two file maps in one pass, with an O(1) adjacent-tail path."""
    if not to_id:
        return None
    tail = _load_snapshot_tail_index(base_path)
    if tail is not None:
        latest = tail['latest']
        latest_id = latest.get('id')
        latest_files = latest.get('files')
        if latest_id == to_id and isinstance(latest_files, dict):
            if not from_id:
                return {}, dict(latest_files)
            if from_id == latest_id:
                copied = dict(latest_files)
                return copied, dict(copied)
            if tail.get('previousId') == from_id:
                previous_files = _tail_previous_files(tail)
                if previous_files is not None:
                    return previous_files, dict(latest_files)

    found_from: dict | None = {} if not from_id else None
    found_to: dict | None = None
    for snapshot in iter_snapshots(base_path):
        snapshot_id = snapshot.get('id')
        if from_id and snapshot_id == from_id:
            found_from = dict(snapshot.get('files') or {})
        if snapshot_id == to_id:
            found_to = dict(snapshot.get('files') or {})
        if found_from is not None and found_to is not None:
            break
    if found_to is None:
        return None
    return found_from or {}, found_to


def find_snapshot(base_path: str, snapshot_id: str) -> dict | None:
    if not snapshot_id:
        return None
    tail = _load_snapshot_tail_index(base_path)
    if tail is not None and tail['latest'].get('id') == snapshot_id:
        return dict(tail['latest'])
    for snapshot in iter_snapshots(base_path):
        if snapshot.get('id') == snapshot_id:
            return snapshot
    return None


def find_snapshot_with_previous(
    base_path: str,
    snapshot_id: str,
) -> tuple[dict | None, dict | None]:
    """Find a snapshot and its predecessor with one replay at most."""
    if not snapshot_id:
        return None, None
    tail = _load_snapshot_tail_index(base_path)
    if tail is not None and tail['latest'].get('id') == snapshot_id:
        previous_files = _tail_previous_files(tail)
        previous = None
        if previous_files is not None and tail.get('previousId'):
            previous = {'id': tail['previousId'], 'files': previous_files}
        return dict(tail['latest']), previous
    previous = None
    for snapshot in iter_snapshots(base_path):
        if snapshot.get('id') == snapshot_id:
            return snapshot, previous
        previous = snapshot
    return None, None


def read_blob(base_path: str, rel_path: str, version: int) -> bytes | None:
    p = backup_blob_path(base_path, rel_path, version)
    try:
        with open(p, 'rb') as f:
            return f.read()
    except FileNotFoundError as _e_audit:
        logger.debug('[store] read_blob caught %s: %s', type(_e_audit).__name__, _e_audit)
        return None
    except OSError as e:
        logger.warning('[FileHistory] read blob v%d for %s failed: %s',
                       version, rel_path, e)
        return None



# ═══════════════════════════════════════════════════════════════════
#  Store compaction (snapshots.jsonl rotation + orphan-blob GC)
# ═══════════════════════════════════════════════════════════════════

def _all_referenced_versions(snaps: Iterable[dict]) -> dict[str, set[int]]:
    """Map ``rel_path -> {versions pinned by any of ``snaps``}``."""
    refs: dict[str, set[int]] = {}
    for snap in snaps:
        for rel, v in (snap.get('files') or {}).items():
            if isinstance(v, int) and v > 0:
                refs.setdefault(rel, set()).add(v)
    return refs


def _gc_orphan_blobs(
    base_path: str,
    survivors: Iterable[dict] | None = None,
    *,
    referenced_versions: dict[str, set[int]] | None = None,
) -> int:
    """Delete backup blobs not pinned by any surviving snapshot.

    Preserves, for every path: version 1 (so "rewind to start" stays
    possible) and the ``latest_version`` recorded in ``tracked.json``
    (the current on-disk state).  Returns the number of blobs removed.
    Best-effort — never raises.
    """
    bdir = backups_dir(base_path)
    if not os.path.isdir(bdir):
        return 0
    refs = (
        referenced_versions
        if referenced_versions is not None
        else _all_referenced_versions(survivors or ())
    )
    tracked = load_tracked(base_path)
    # Map sha-prefix back to ref sets keyed by the hashed rel_path.
    keep_by_hash: dict[str, set[int]] = {}
    for rel, versions in refs.items():
        keep_by_hash.setdefault(_hash_rel_path(rel), set()).update(versions)
    for rel, info in tracked.items():
        lv = int(info.get('latest_version') or 0)
        if lv > 0:
            keep_by_hash.setdefault(_hash_rel_path(rel), set()).add(lv)
    removed = 0
    for sub in os.listdir(bdir):
        bucket = os.path.join(bdir, sub)
        if not os.path.isdir(bucket):
            continue
        for name in os.listdir(bucket):
            if '@v' not in name:
                continue
            h, _, vstr = name.rpartition('@v')
            try:
                v = int(vstr)
            except ValueError as e:
                logger.debug('[FileHistory] skipping non-versioned blob %r: %s', name, e)
                continue
            if v == 1:
                continue  # always keep the earliest backup
            if v in keep_by_hash.get(h, ()):  # pinned by a survivor / current
                continue
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(bucket, name))
                removed += 1
    return removed


@with_project_lock
def compact_store(base_path: str) -> dict:
    """Trim ``snapshots.jsonl`` to the newest ``MAX_SNAPSHOTS`` records and
    GC any backup blob no surviving snapshot pins.

    Returns ``{snapshots_before, snapshots_after, blobs_removed}``.
    Caller must hold the project lock.  Best-effort: on any error the
    store is left untouched and the error is logged.
    """
    result = {
        'snapshots_before': 0,
        'snapshots_after': 0,
        'blobs_removed': 0,
        'bytes_before': 0,
        'bytes_after': 0,
    }
    ensure_store(base_path)
    path = snapshots_path(base_path)
    from lib.json_store import locked_path

    with locked_path(path):
        try:
            result['bytes_before'] = os.path.getsize(path)
        except OSError:
            return result
        summary = _scan_snapshot_log_summary(base_path)
        snapshot_count = int(summary.get('snapshotCount') or 0)
        raw_record_count = _count_nonempty_snapshot_lines(base_path)
        result['snapshots_before'] = snapshot_count
        result['snapshots_after'] = snapshot_count
        needs_rewrite = (
            snapshot_count > MAX_SNAPSHOTS
            or result['bytes_before'] > SNAPSHOT_LOG_REWRITE_BYTES
        )
        if not needs_rewrite:
            _write_snapshot_tail_index(
                base_path,
                snapshot_count=snapshot_count,
                latest=summary.get('latest'),
                previous=summary.get('previous'),
                delta_depth=int(summary.get('deltaDepth') or 0),
            )
            result['bytes_after'] = result['bytes_before']
            return result
        if raw_record_count != snapshot_count:
            logger.warning(
                '[FileHistory] compact_store: refusing rewrite of %d raw row(s) '
                'with only %d materializable snapshot(s)',
                raw_record_count, snapshot_count,
            )
            result['bytes_after'] = result['bytes_before']
            return result

        keep_count = min(snapshot_count, MAX_SNAPSHOTS)
        skip_count = snapshot_count - keep_count
        directory = os.path.dirname(path)
        references: dict[str, set[int]] = {}
        previous: dict | None = None
        penultimate: dict | None = None
        latest: dict | None = None
        delta_depth = 0
        written = 0
        try:
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=directory, prefix='.fh-snap-', suffix='.tmp',
            )
            with os.fdopen(fd, 'wb') as handle:
                for index, (snapshot, _old_depth) in enumerate(
                    _iter_materialized_snapshot_entries(base_path)
                ):
                    if index < skip_count:
                        continue
                    stored, delta_depth = encode_snapshot_record(
                        snapshot, previous, delta_depth,
                    )
                    line = compact_json_bytes(stored)
                    if len(line) > MAX_SNAPSHOT_RECORD_BYTES:
                        raise ValueError(
                            'materialized snapshot exceeds JSONL record cap'
                        )
                    handle.write(line)
                    handle.write(b'\n')
                    for rel_path, version in (snapshot.get('files') or {}).items():
                        if isinstance(version, int) and version > 0:
                            references.setdefault(rel_path, set()).add(version)
                    penultimate = latest
                    latest = snapshot
                    previous = snapshot
                    written += 1
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception as error:
            logger.warning('[FileHistory] compact_store: rewrite failed: %s', error)
            with contextlib.suppress(OSError, NameError):
                os.unlink(temporary)  # type: ignore[name-defined]
            return result

        result['snapshots_after'] = written
        try:
            result['bytes_after'] = os.path.getsize(path)
        except OSError:
            result['bytes_after'] = 0
        _write_snapshot_tail_index(
            base_path,
            snapshot_count=written,
            latest=latest,
            previous=penultimate,
            delta_depth=delta_depth,
        )
        fingerprint = _snapshot_log_fingerprint(base_path)
        _reference_cache_put(base_path, fingerprint, references)
        result['blobs_removed'] = _gc_orphan_blobs(
            base_path, referenced_versions=references,
        )
        logger.info(
            '[FileHistory] compacted store: %d → %d snapshots, '
            '%d → %d bytes, %d orphan blob(s) removed',
            result['snapshots_before'], result['snapshots_after'],
            result['bytes_before'], result['bytes_after'],
            result['blobs_removed'],
        )
        return result


def maybe_compact_store(base_path: str, snapshot_count: int) -> None:
    """Cheap gate: run :func:`compact_store` only every ``COMPACT_CHECK_EVERY``
    snapshots.  ``snapshot_count`` is the freshly-appended record's index
    in the log (1-based).  Caller must hold the project lock.  Never raises.
    """
    if snapshot_count <= 0:
        return
    try:
        oversized_log = (
            os.path.getsize(snapshots_path(base_path))
            > SNAPSHOT_LOG_REWRITE_BYTES
        )
    except OSError:
        oversized_log = False
    if not oversized_log and snapshot_count % COMPACT_CHECK_EVERY != 0:
        return
    try:
        compact_store(base_path)
    except Exception as e:
        logger.debug('[FileHistory] maybe_compact_store skipped: %s', e)
