"""Disk-layer primitives for the file-history store.

Serialises to ``<base_path>/.tofu/file-history/`` with three things on
disk:

* ``snapshots.jsonl`` — append-only log of :class:`FileHistorySnapshot`.
* ``tracked.json`` — single-shot persisted set of currently-tracked
  ``rel_path``s plus their latest version number (so we don't have to
  re-scan ``backups/`` on every call).
* ``backups/<sha256(rel)[:2]>/<sha256(rel)>@v<n>`` — copy backup blobs.

All public helpers in this module are guarded by a per-project ``RLock``
so concurrent task threads on the same project don't tear the
``snapshots.jsonl`` or the ``tracked.json`` index.  Different projects
do not contend.
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
from typing import Iterable

from lib.log import get_logger

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

#: When the directory containing snapshots/backups grows above this many
#: bytes, ``compact_store`` may be called to trim oldest snapshots.
SOFT_DISK_BUDGET_BYTES = 256 * 1024 * 1024

#: Hard cap on the number of snapshot records kept in ``snapshots.jsonl``.
#: Append-only logs never shrink on their own; once we exceed this many
#: rows ``compact_store`` rewrites the log keeping only the newest
#: ``MAX_SNAPSHOTS`` and GCs any backup blob no surviving snapshot pins.
MAX_SNAPSHOTS = 2000

#: ``make_snapshot`` calls ``maybe_compact_store`` every this-many
#: snapshots (cheap modulo gate; the full size/row scan only runs then).
COMPACT_CHECK_EVERY = 200


# ═══════════════════════════════════════════════════════════════════
#  Per-project lock (mirrors the per-repo RLock pattern)
# ═══════════════════════════════════════════════════════════════════

_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_MUTEX = threading.Lock()


def _project_lock(base_path: str) -> threading.RLock:
    key = os.path.abspath(base_path)
    with _PROJECT_LOCKS_MUTEX:
        lk = _PROJECT_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _PROJECT_LOCKS[key] = lk
    return lk


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
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_json(path: str, payload) -> None:
    """Atomically write JSON. Delegates to lib.json_store."""
    from lib.json_store import write_json_atomic
    write_json_atomic(path, payload, fsync=True, indent=2)


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


def _versions_referenced_by_snapshots(base_path: str, rel_path: str) -> set[int]:
    """Set of ``rel_path``'s versions still pinned by some snapshot.

    Best-effort — on read errors returns an empty set (which is the safe
    side: GC will preserve fewer files, never delete still-pinned ones,
    because the keep-above threshold also applies).
    """
    refs: set[int] = set()
    try:
        for snap in iter_snapshots(base_path):
            files = snap.get('files') or {}
            v = files.get(rel_path)
            if isinstance(v, int):
                refs.add(v)
    except Exception as e:
        logger.debug('[FileHistory] snapshot scan for refs failed: %s', e)
    return refs


# ═══════════════════════════════════════════════════════════════════
#  Snapshots log (append-only JSONL)
# ═══════════════════════════════════════════════════════════════════

def append_snapshot_record(base_path: str, record: dict) -> None:
    p = snapshots_path(base_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')
    # Append atomically: open in append mode + fsync.
    with open(p, 'ab') as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def iter_snapshots(base_path: str) -> Iterable[dict]:
    """Yield every snapshot record in chronological order (oldest first).

    Skips malformed lines with a debug log.  Never raises.
    """
    p = snapshots_path(base_path)
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding='utf-8') as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.debug('[FileHistory] malformed snapshot line skipped: %s', e)
    except OSError as e:
        logger.warning('[FileHistory] could not read %s: %s', p, e)


def find_snapshot(base_path: str, snapshot_id: str) -> dict | None:
    if not snapshot_id:
        return None
    for s in iter_snapshots(base_path):
        if s.get('id') == snapshot_id:
            return s
    return None


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


def _gc_orphan_blobs(base_path: str, survivors: list[dict]) -> int:
    """Delete backup blobs not pinned by any surviving snapshot.

    Preserves, for every path: version 1 (so "rewind to start" stays
    possible) and the ``latest_version`` recorded in ``tracked.json``
    (the current on-disk state).  Returns the number of blobs removed.
    Best-effort — never raises.
    """
    bdir = backups_dir(base_path)
    if not os.path.isdir(bdir):
        return 0
    refs = _all_referenced_versions(survivors)
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


def compact_store(base_path: str) -> dict:
    """Trim ``snapshots.jsonl`` to the newest ``MAX_SNAPSHOTS`` records and
    GC any backup blob no surviving snapshot pins.

    Returns ``{snapshots_before, snapshots_after, blobs_removed}``.
    Caller must hold the project lock.  Best-effort: on any error the
    store is left untouched and the error is logged.
    """
    result = {'snapshots_before': 0, 'snapshots_after': 0, 'blobs_removed': 0}
    try:
        snaps = list(iter_snapshots(base_path))
    except Exception as e:
        logger.warning('[FileHistory] compact_store: read failed: %s', e)
        return result
    result['snapshots_before'] = len(snaps)
    if len(snaps) <= MAX_SNAPSHOTS:
        result['snapshots_after'] = len(snaps)
        return result

    survivors = snaps[-MAX_SNAPSHOTS:]
    p = snapshots_path(base_path)
    dn = os.path.dirname(p)
    try:
        os.makedirs(dn, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dn, prefix='.fh-snap-', suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            for snap in survivors:
                f.write(json.dumps(snap, ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except OSError as e:
        logger.warning('[FileHistory] compact_store: rewrite failed: %s', e)
        with contextlib.suppress(OSError, NameError):
            os.unlink(tmp)  # type: ignore[name-defined]
        return result
    result['snapshots_after'] = len(survivors)
    result['blobs_removed'] = _gc_orphan_blobs(base_path, survivors)
    logger.info('[FileHistory] compacted store: %d → %d snapshots, %d orphan blob(s) removed',
                result['snapshots_before'], result['snapshots_after'],
                result['blobs_removed'])
    return result


def maybe_compact_store(base_path: str, snapshot_count: int) -> None:
    """Cheap gate: run :func:`compact_store` only every ``COMPACT_CHECK_EVERY``
    snapshots.  ``snapshot_count`` is the freshly-appended record's index
    in the log (1-based).  Caller must hold the project lock.  Never raises.
    """
    if snapshot_count <= 0 or snapshot_count % COMPACT_CHECK_EVERY != 0:
        return
    try:
        compact_store(base_path)
    except Exception as e:
        logger.debug('[FileHistory] maybe_compact_store skipped: %s', e)
