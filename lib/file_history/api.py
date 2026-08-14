"""Public API for the file-history package.

See ``lib/file_history/__init__.py`` for the design rationale.  This
module is the only one external code should import from.

Threading: all mutation entry points are wrapped in a per-project
``RLock`` (see :mod:`lib.file_history.store`).  Reads (``list_history``,
``find_snapshot``) are unlocked.
"""
from __future__ import annotations

import difflib
import os
import re
import time
import uuid

from lib.json_store import write_bytes_atomic
from lib.log import get_logger

from lib.file_history.store import (
    COMPACT_CHECK_EVERY,
    append_snapshot_record,
    backup_blob_path,
    ensure_store,
    find_snapshot,
    iter_snapshots,
    load_tracked,
    maybe_compact_store,
    read_blob,
    save_tracked,
    stage_backup,
    with_project_lock,
)

logger = get_logger(__name__)

_DISABLED_WARNED = False

# Per-project count of snapshots appended this process, used as a cheap
# modulo gate for ``maybe_compact_store`` (avoids scanning the whole log
# every round just to decide whether compaction is due).
_SNAP_COUNTERS: dict[str, int] = {}


# ═══════════════════════════════════════════════════════════════════
#  Feature flags
# ═══════════════════════════════════════════════════════════════════

def is_enabled() -> bool:
    """Honour ``TOFU_FILE_HISTORY``.  Default ``1`` (enabled)."""
    flag = (os.environ.get('TOFU_FILE_HISTORY')
            or '1').strip().lower()
    if flag in ('0', 'false', 'no', 'off'):
        global _DISABLED_WARNED
        if not _DISABLED_WARNED:
            logger.info('[FileHistory] disabled via TOFU_FILE_HISTORY=%s', flag)
            _DISABLED_WARNED = True
        return False
    return True


def probe_enabled() -> bool:
    """External-edit probe gate. ``TOFU_FILE_HISTORY_PROBE`` (default 1)."""
    flag = (os.environ.get('TOFU_FILE_HISTORY_PROBE')
            or '1').strip().lower()
    return flag not in ('0', 'false', 'no', 'off')


# ═══════════════════════════════════════════════════════════════════
#  track_edit — pre-write hook
# ═══════════════════════════════════════════════════════════════════

@with_project_lock
def track_edit(base_path: str, rel_path: str, *,
               message_id: str | None = None,
               pre_content: bytes | str | None = None,
               task_id: str | None = None) -> int | None:
    """Record a backup version of ``rel_path``'s contents.

    By default reads the current on-disk contents (true pre-write hook
    when called BEFORE the write).  ``pre_content`` lets callers pass
    the pre-write content explicitly when the file has already been
    overwritten by the time we're invoked — this is the path used by
    ``_record_modification`` in ``lib/project_mod/modifications.py``.

    Returns the version number recorded, or ``None`` if no new backup
    was needed (file unchanged since last seen) or if the store is
    disabled.

    ``message_id`` is recorded for diagnostics only.  ``task_id`` is
    stored as the file's ``last_writer_task_id`` so the orchestrator
    can attribute fh side-channel mutations correctly when multiple
    tasks edit the same project root concurrently.  ``rel_path``
    should be project-relative with forward slashes; absolute paths are
    converted with ``os.path.relpath`` against ``base_path``.
    """
    if not is_enabled():
        return None
    if not rel_path:
        return None
    rel = _normalize_rel(base_path, rel_path)
    if rel is None:
        return None
    try:
        ensure_store(base_path)
        v = stage_backup(base_path, rel, explicit_content=pre_content,
                         task_id=task_id or message_id)
        if v is not None:
            logger.debug('[FileHistory] track_edit %s → v%d (msg=%s task=%s explicit=%s)',
                         rel, v, (message_id or '-')[:8],
                         (task_id or '-')[:8],
                         pre_content is not None)
        return v
    except Exception as e:
        logger.warning('[FileHistory] track_edit failed for %s: %s', rel, e)
        return None


def _normalize_rel(base_path: str, rel_path: str) -> str | None:
    """Coerce ``rel_path`` to a project-relative posix path.

    Returns ``None`` (and logs at debug) if the path escapes the project
    root — we never back up files outside the workspace.
    """
    abs_base = os.path.abspath(base_path)
    if os.path.isabs(rel_path):
        try:
            rp = os.path.relpath(rel_path, abs_base)
        except ValueError:
            logger.debug('[FileHistory] cannot relativise %s vs %s',
                         rel_path, abs_base)
            return None
    else:
        rp = rel_path
    # Strip only a literal leading './' prefix — NOT lstrip('./'), which is a
    # CHARACTER SET and mangles every dotfile ('.gitignore' → 'gitignore',
    # '.tofu/x' → 'tofu/x'). That silently corrupted the stored snapshot key,
    # so undo/redo of a dotfile wrote to the wrong on-disk path, and any
    # consumer cross-checking the recorded key against the real path (e.g.
    # project_commit's byte-identity attribution) could never match.
    rp = re.sub(r'^(?:\./)+', '', rp.replace('\\', '/'))
    if rp.startswith('../') or rp == '..':
        logger.debug('[FileHistory] refusing path outside project root: %s', rel_path)
        return None
    return rp


# ═══════════════════════════════════════════════════════════════════
#  make_snapshot — round end
# ═══════════════════════════════════════════════════════════════════

@with_project_lock
def make_snapshot(base_path: str, *,
                  task_id: str | None,
                  conv_id: str | None = None,
                  message_id: str | None = None,
                  tool_names: list[str] | None = None,
                  summary: str | None = None,
                  rel_paths: list[str] | None = None,
                  external: bool = False,
                  redo_of: str | None = None) -> str | None:
    """Pin a snapshot at the end of one round.

    Walks the tracked-files index and records, for each currently-tracked
    path, the version that represents its post-round contents.  If
    ``rel_paths`` is given, also re-stages those paths first (so a tool
    that writes through a non-write_file path can still get its
    post-image into a new backup version before we pin the snapshot).

    Returns the snapshot id (a UUID4) or ``None`` if disabled / nothing
    to record.
    """
    if not is_enabled():
        return None
    try:
        ensure_store(base_path)
        # Capture post-images for explicitly-declared paths.  This is the
        # equivalent of ``git add -A`` over the round's known file list,
        # but bounded to a list of paths instead of the whole worktree.
        if rel_paths:
            for rp in rel_paths:
                norm = _normalize_rel(base_path, rp)
                if norm:
                    stage_backup(base_path, norm, task_id=task_id)

        tracked = load_tracked(base_path)
        # Build the snapshot file map: {rel_path: latest_version}.
        # ``deleted`` files are recorded with version 0 to signal "remove
        # on rewind".
        files: dict[str, int] = {}
        for rel, info in tracked.items():
            v = int(info.get('latest_version') or 0)
            if v > 0 or info.get('deleted'):
                files[rel] = 0 if info.get('deleted') else v
        if not files and not external:
            logger.debug('[FileHistory] make_snapshot: no tracked files (task=%s)',
                         task_id)
            return None

        snap_id = uuid.uuid4().hex
        record = {
            'id': snap_id,
            'taskId': task_id,
            'convId': conv_id,
            'messageId': message_id,
            'tools': list(tool_names or []),
            'summary': (summary or '').strip() or None,
            'when': time.time(),
            'files': files,
            'external': bool(external),
            'redoOf': redo_of,
        }
        append_snapshot_record(base_path, record)
        logger.info('[FileHistory] snapshot %s task=%s conv=%s files=%d%s',
                    snap_id[:8], (task_id or '-')[:12],
                    (conv_id or '-')[:8], len(files),
                    ' external' if external else '')
        # Cheap modulo gate: only every COMPACT_CHECK_EVERY appends do we
        # pay for the full-log scan inside compact_store. The counter is
        # seeded from the on-disk log length the first time we see a project
        # so a long-lived store still compacts promptly after a restart.
        key = os.path.abspath(base_path)
        n = _SNAP_COUNTERS.get(key)
        if n is None:
            n = sum(1 for _ in iter_snapshots(base_path))
        else:
            n += 1
        _SNAP_COUNTERS[key] = n
        if n % COMPACT_CHECK_EVERY == 0:
            maybe_compact_store(base_path, n)
        return snap_id
    except Exception as e:
        logger.warning('[FileHistory] make_snapshot failed task=%s: %s',
                       task_id, e, exc_info=True)
        return None


# ═══════════════════════════════════════════════════════════════════
#  Read API: history / diff_name_status
# ═══════════════════════════════════════════════════════════════════

def list_history(base_path: str, *,
                 path: str | None = None,
                 limit: int = 20) -> list[dict]:
    """Return recent snapshot summaries (newest first).

    Filtered by ``path`` if given (only snapshots that touch that
    path).  ``limit`` defaults to 20.
    """
    if not is_enabled():
        return []
    try:
        snaps = list(iter_snapshots(base_path))
    except Exception as e:
        logger.warning('[FileHistory] list_history failed: %s', e)
        return []
    snaps.reverse()  # newest first
    if path:
        norm = _normalize_rel(base_path, path)
        snaps = [s for s in snaps
                 if norm is not None and norm in (s.get('files') or {})]
    out = []
    for s in snaps[:max(1, int(limit))]:
        files = list((s.get('files') or {}).keys())
        out.append({
            'id': s.get('id'),
            'shortId': (s.get('id') or '')[:8],
            'taskId': s.get('taskId'),
            'convId': s.get('convId'),
            'tools': s.get('tools') or [],
            'summary': s.get('summary'),
            'when': s.get('when'),
            'filesChanged': files,
            'external': bool(s.get('external')),
            'redoOf': s.get('redoOf'),
        })
    return out


def diff_name_status(base_path: str, from_id: str | None,
                     to_id: str | None) -> list[dict]:
    """Return ``[{path, action}]`` between two snapshots.

    Compares the ``files`` maps of ``from_id`` vs ``to_id``.  Paths that
    appear only in ``to_id`` → ``created``.  Paths that appear in both
    with different versions → ``modified``.  Paths that appear in
    ``from_id`` but in ``to_id`` are version 0 (tombstone) → ``deleted``.

    Returns ``[]`` on any failure — never raises.
    """
    if not is_enabled() or not to_id:
        return []
    try:
        to_snap = find_snapshot(base_path, to_id)
        if not to_snap:
            return []
        to_files = to_snap.get('files') or {}
        from_files: dict[str, int] = {}
        if from_id:
            from_snap = find_snapshot(base_path, from_id)
            if from_snap:
                from_files = from_snap.get('files') or {}
        out: list[dict] = []
        for rel, v in to_files.items():
            prev = from_files.get(rel)
            if v == 0:
                if prev not in (None, 0):
                    out.append({'path': rel, 'action': 'deleted'})
                continue
            if prev is None or prev == 0:
                out.append({'path': rel, 'action': 'created'})
            elif prev != v:
                out.append({'path': rel, 'action': 'modified'})
        # Files present in from but absent in to (rare — only if a later
        # snapshot truly forgot them, which we don't currently produce —
        # but defend anyway).
        for rel in from_files:
            if rel not in to_files:
                v_prev = from_files.get(rel)
                if v_prev not in (None, 0):
                    out.append({'path': rel, 'action': 'deleted'})
        return out
    except Exception as e:
        logger.debug('[FileHistory] diff_name_status %s..%s failed: %s',
                     from_id, to_id, e)
        return []


# ═══════════════════════════════════════════════════════════════════
#  Rewind / restore
# ═══════════════════════════════════════════════════════════════════

@with_project_lock
def rewind_to(base_path: str, snapshot_id: str) -> dict:
    """Restore the working tree to the state recorded at ``snapshot_id``'s
    PARENT — i.e. undo the round that produced ``snapshot_id``.

    For each path the snapshot lists, we look up its version IN THE
    PRECEDING SNAPSHOT (or "absent" if no preceding snapshot recorded
    it).  Then we copy the backup blob (or delete the file) to restore
    the pre-round state.

    Returns ``{ok, snapshotId, files: [paths], failed: [{path, reason}]}``.
    """
    result: dict = {'ok': False, 'snapshotId': snapshot_id,
                    'files': [], 'failed': []}
    if not is_enabled() or not snapshot_id:
        result['error'] = 'disabled or empty snapshot_id'
        return result
    try:
        target = find_snapshot(base_path, snapshot_id)
        if not target:
            result['error'] = 'snapshot not found'
            return result
        # Pre-state = the most recent snapshot strictly before this one.
        prior_files: dict[str, int] = {}
        prev_seen = False
        for s in iter_snapshots(base_path):
            if s.get('id') == snapshot_id:
                break
            prior_files = s.get('files') or {}
            prev_seen = True
        # Apply rewind: for every path in target.files, restore to
        # prior_files[path] (default: absent → delete).
        target_files = target.get('files') or {}
        applied: list[dict] = []
        failed: list[dict] = []
        for rel in target_files:
            prev_v = int(prior_files.get(rel) or 0)
            try:
                ok, action = _restore_one(base_path, rel, prev_v)
                if ok:
                    applied.append({'path': rel, 'action': action})
                else:
                    failed.append({'path': rel, 'reason': action})
            except Exception as e:
                failed.append({'path': rel, 'reason': str(e)})
                logger.warning('[FileHistory] rewind %s failed: %s', rel, e)
        result['ok'] = True
        result['files'] = applied
        result['failed'] = failed
        result['hadPriorSnapshot'] = prev_seen
        logger.info('[FileHistory] rewind to=%s applied=%d failed=%d',
                    snapshot_id[:8], len(applied), len(failed))
        return result
    except Exception as e:
        logger.warning('[FileHistory] rewind_to %s failed: %s',
                       snapshot_id[:8] if snapshot_id else '-', e, exc_info=True)
        result['error'] = str(e)
        return result


@with_project_lock
def restore_from(base_path: str, snapshot_id: str) -> dict:
    """Re-apply ``snapshot_id`` (REDO an earlier round).

    For each path in the snapshot's file map, copies the corresponding
    backup blob (or deletes the file if version 0).  After restore, a
    new ``redoOf`` snapshot is appended to the log so the timeline
    stays linear.
    """
    result: dict = {'ok': False, 'snapshotId': snapshot_id,
                    'files': [], 'failed': []}
    if not is_enabled() or not snapshot_id:
        result['error'] = 'disabled or empty snapshot_id'
        return result
    try:
        target = find_snapshot(base_path, snapshot_id)
        if not target:
            result['error'] = 'snapshot not found'
            return result
        target_files = target.get('files') or {}
        applied: list[dict] = []
        failed: list[dict] = []
        for rel, v in target_files.items():
            try:
                ok, action = _restore_one(base_path, rel, int(v or 0))
                if ok:
                    applied.append({'path': rel, 'action': action})
                else:
                    failed.append({'path': rel, 'reason': action})
            except Exception as e:
                failed.append({'path': rel, 'reason': str(e)})
                logger.warning('[FileHistory] restore_from %s failed: %s', rel, e)

        # Refresh tracked.json so the new disk state is the latest.
        tracked = load_tracked(base_path)
        for rel, v in target_files.items():
            v_int = int(v or 0)
            entry = tracked.get(rel) or {}
            if v_int == 0:
                tracked[rel] = {**entry, 'latest_version': v_int,
                                'deleted': True, 'mtime': 0, 'size': 0}
            else:
                abs_p = os.path.join(os.path.abspath(base_path), rel)
                try:
                    st = os.stat(abs_p)
                    tracked[rel] = {**entry, 'latest_version': v_int,
                                    'deleted': False,
                                    'mtime': st.st_mtime, 'size': st.st_size}
                except OSError as _e_audit:
                    logger.debug('[api] restore_from caught %s: %s', type(_e_audit).__name__, _e_audit)
                    tracked[rel] = {**entry, 'latest_version': v_int,
                                    'deleted': False}
        save_tracked(base_path, tracked)

        new_id = make_snapshot(
            base_path,
            task_id=target.get('taskId'),
            conv_id=target.get('convId'),
            tool_names=target.get('tools') or [],
            summary=f'redo-of {snapshot_id[:8]}',
            redo_of=snapshot_id,
        )
        result['ok'] = True
        result['files'] = applied
        result['failed'] = failed
        result['newSnapshotId'] = new_id
        logger.info('[FileHistory] restore_from %s applied=%d failed=%d new=%s',
                    snapshot_id[:8], len(applied), len(failed),
                    (new_id or '-')[:8])
        return result
    except Exception as e:
        logger.warning('[FileHistory] restore_from %s failed: %s',
                       snapshot_id[:8] if snapshot_id else '-', e, exc_info=True)
        result['error'] = str(e)
        return result


def _restore_one(base_path: str, rel: str, version: int) -> tuple[bool, str]:
    """Restore ``rel`` to ``version``'s contents (or delete if v=0).

    Returns (ok, action_label_or_reason).
    """
    abs_p = os.path.join(os.path.abspath(base_path), rel)
    if version == 0:
        try:
            if os.path.exists(abs_p):
                os.remove(abs_p)
                _nudge_vscode(abs_p)
                return True, 'deleted'
            return True, 'already_absent'
        except OSError as e:
            logger.debug('[api] _restore_one caught %s: %s', type(e).__name__, e)
            return False, f'unlink failed: {e}'
    blob = read_blob(base_path, rel, version)
    if blob is None:
        # The blob was GC'd or never written (file too large at backup time).
        return False, f'no backup for v{version}'
    try:
        write_bytes_atomic(abs_p, blob)
        _nudge_vscode(abs_p)
        return True, 'restored'
    except OSError as e:
        logger.debug('[api] _restore_one caught %s: %s', type(e).__name__, e)
        return False, f'write failed: {e}'


def _nudge_vscode(abs_path: str) -> None:
    """Bump mtime so VS Code's file watcher picks up the change."""
    try:
        st = os.stat(abs_path)
        os.utime(abs_path, (st.st_atime, st.st_mtime + 1e-6))
    except OSError as e:
        logger.debug('[FileHistory] vscode mtime bump failed for %s: %s', abs_path, e)


# ═══════════════════════════════════════════════════════════════════
#  External-edit detection (mtime-based, bounded by tracked set)
# ═══════════════════════════════════════════════════════════════════

def _disk_matches_last_backup(base_path: str, rel: str, info: dict,
                              abs_p: str) -> bool:
    """True iff ``abs_p``'s current bytes equal the last backed-up version.

    This is the CONTENT guard against phantom external-edit reports.  An
    mtime/size difference is only a *hint* that a file may have changed:
    a byte-identical rewrite still bumps mtime, and — critically — the
    ``mtime: 0`` sentinel that :func:`~lib.file_history.store._stage_explicit`
    records after a Tofu write tool overwrites a file makes EVERY
    subsequent probe see ``real_mtime != 0`` even though the on-disk
    bytes never changed out-of-band.  Comparing actual content against
    the pinned backup blob is what tells a real edit from that noise.

    Returns ``False`` (i.e. "can't confirm identical → treat as possible
    drift") when there is no comparable blob: the file was never backed
    up (``latest_version == 0``), was too large to back up
    (``too_large``), is a tombstone (``deleted``), the blob is missing/
    GC'd, or the file can't be read.  Callers then fall through to the
    normal drift path, so this guard can only SUPPRESS a false positive,
    never manufacture one or hide a genuine edit.
    """
    v = int(info.get('latest_version') or 0)
    if v <= 0 or info.get('too_large') or info.get('deleted'):
        return False
    blob = read_blob(base_path, rel, v)
    if blob is None:
        return False
    try:
        with open(abs_p, 'rb') as f:
            disk = f.read()
    except OSError as e:
        logger.debug('[FileHistory] content-guard read failed for %s: %s', rel, e)
        return False
    return disk == blob


@with_project_lock
def detect_external_edits(base_path: str, *,
                          message_id: str | None = None,
                          known_task_ids: set[str] | None = None) -> dict:
    """Detect genuine out-of-band (IDE) edits to already-tracked files.

    Walks ONLY the tracked-files index — never the whole worktree.  For
    each tracked file, compares current ``stat`` against the recorded
    ``mtime``/``size``.  A stat difference is treated as a *hint* only:
    two guards then decide whether it is a real, external edit before it
    stages a backup / fires the drift notice.

    Guard 1 — CONTENT (kills phantom fires).  A stat hint is confirmed
    against actual bytes via :func:`_disk_matches_last_backup`.  A
    byte-identical rewrite (including the ``mtime: 0`` sentinel
    ``_stage_explicit`` leaves after a Tofu write) is NOT drift: the
    stale ``mtime``/``size`` in the index is repaired so future probes
    short-circuit, and NO redundant backup or snapshot is produced.

    Guard 2 — ATTRIBUTION (stops misattributing sibling conversations).
    The tracked entry's ``last_writer_task_id`` is stamped by Tofu's own
    write path (an IDE never touches it).  A genuine content change whose
    last writer is a KNOWN/recent Tofu task (``known_task_ids``) is a
    concurrent conversation editing the shared project root — NOT an
    external/IDE edit — so it is excluded from the external-edit path and
    returned separately under ``siblingFiles`` instead of firing the
    "you edited this in an IDE" toast.

    ``known_task_ids`` is the set of in-process/recent Tofu task ids
    (the orchestrator passes ``manager.tasks``); when ``None`` no
    attribution filtering is applied (every genuine change counts as
    external — the conservative legacy behaviour).

    Returns ``{committed, snapshotId, files, siblingFiles}`` where
    ``files`` are genuinely-external paths (drive the drift toast) and
    ``siblingFiles`` are ``[{path, taskId}]`` attributed to a concurrent
    Tofu task.
    """
    out: dict = {'committed': False, 'snapshotId': None,
                 'files': [], 'siblingFiles': []}
    if not is_enabled() or not probe_enabled():
        return out
    known = set(known_task_ids or ())
    try:
        tracked = load_tracked(base_path)
        if not tracked:
            return out
        drifted: list[str] = []
        sibling: list[dict] = []
        # Paths whose stat drifted but whose CONTENT is byte-identical —
        # their stale index stat is reconciled AFTER the loop so we never
        # hold a stale ``tracked`` dict across a ``stage_backup`` call
        # (which does its own load-modify-save of tracked.json).
        reconcile: dict[str, tuple[int, float]] = {}

        def _attributed(writer: str) -> bool:
            return bool(writer) and writer in known

        for rel, info in tracked.items():
            if info.get('deleted'):
                continue
            abs_p = os.path.join(os.path.abspath(base_path), rel)
            try:
                st = os.stat(abs_p)
            except FileNotFoundError as _e_audit:
                # File deleted out-of-band → real drift, but a delete by a
                # concurrent Tofu task is a sibling write, not an IDE edit.
                logger.debug('[api] detect_external_edits caught %s: %s',
                             type(_e_audit).__name__, _e_audit)
                writer = info.get('last_writer_task_id') or ''
                if _attributed(writer):
                    sibling.append({'path': rel, 'taskId': writer})
                    continue
                if stage_backup(base_path, rel) is not None:
                    drifted.append(rel)
                continue
            except OSError as e:
                logger.debug('[FileHistory] probe stat failed for %s: %s', rel, e)
                continue
            stat_hint = (st.st_size != info.get('size')
                         or abs(st.st_mtime - float(info.get('mtime') or 0)) > 1e-3)
            if not stat_hint:
                continue
            # Guard 1 — CONTENT: a byte-identical rewrite is not an edit.
            if _disk_matches_last_backup(base_path, rel, info, abs_p):
                reconcile[rel] = (st.st_size, st.st_mtime)
                continue
            # Genuine content change.  Guard 2 — ATTRIBUTION.
            writer = info.get('last_writer_task_id') or ''
            if _attributed(writer):
                sibling.append({'path': rel, 'taskId': writer})
                continue
            if stage_backup(base_path, rel) is not None:
                drifted.append(rel)

        # Reconcile stale stat for byte-identical files against the
        # FRESHEST index (post any stage_backup version bumps), touching
        # only mtime/size and leaving version + last_writer_task_id intact.
        if reconcile:
            try:
                fresh = load_tracked(base_path)
                changed = False
                for rel, (size, mtime) in reconcile.items():
                    e = fresh.get(rel)
                    if not e or e.get('deleted'):
                        continue
                    if (e.get('size') != size
                            or abs(float(e.get('mtime') or 0) - mtime) > 1e-3):
                        e['size'] = size
                        e['mtime'] = mtime
                        changed = True
                if changed:
                    save_tracked(base_path, fresh)
                    logger.debug('[FileHistory] reconciled stat for %d '
                                 'byte-identical file(s) (no drift)', len(reconcile))
            except Exception as _e:
                logger.debug('[FileHistory] stat reconcile failed: %s', _e)

        if sibling:
            out['siblingFiles'] = sibling
            logger.info('[FileHistory] %d drifted file(s) attributed to '
                        'concurrent Tofu task(s) — not external', len(sibling))
        if not drifted:
            return out
        snap_id = make_snapshot(
            base_path,
            task_id=None,
            conv_id=None,
            message_id=message_id,
            tool_names=['external_edit'],
            summary=f'External drift to {len(drifted)} file(s)',
            external=True,
        )
        out['committed'] = bool(snap_id)
        out['snapshotId'] = snap_id
        out['files'] = drifted
        if snap_id:
            logger.info('[FileHistory] external-edit captured: %d file(s) snap=%s',
                        len(drifted), snap_id[:8])
        return out
    except Exception as e:
        logger.warning('[FileHistory] detect_external_edits failed: %s', e)
        return out


# ═══════════════════════════════════════════════════════════════════
#  Convenience helpers
# ═══════════════════════════════════════════════════════════════════

def get_last_snapshot_id(base_path: str) -> str | None:
    last = None
    try:
        for s in iter_snapshots(base_path):
            last = s.get('id') or last
    except Exception as e:
        logger.debug('[FileHistory] get_last_snapshot_id failed: %s', e)
    return last


def diff_text(base_path: str, from_id: str | None, to_id: str | None,
              *, path: str | None = None,
              max_chars: int = 100_000) -> str:
    """Render a unified diff between two snapshots (or one snapshot vs disk).

    Used by the LLM-facing ``project_diff`` tool replacement (we kept the
    function here in case we re-introduce a curated diff tool later, even
    though the LLM-facing tool itself was retired).  Output is capped at
    ``max_chars``.
    """
    if not is_enabled() or not to_id:
        return ''
    to_snap = find_snapshot(base_path, to_id) if to_id else None
    from_snap = find_snapshot(base_path, from_id) if from_id else None
    if not to_snap and not path:
        return ''
    pieces: list[str] = []
    rels = (to_snap or {}).get('files', {}).keys() if to_snap else []
    if path:
        rels = [_normalize_rel(base_path, path)] if path else []
        rels = [r for r in rels if r]
    for rel in rels:
        a = b''
        b = b''
        v_from = (from_snap or {}).get('files', {}).get(rel) if from_snap else None
        v_to = (to_snap or {}).get('files', {}).get(rel) if to_snap else None
        if v_from:
            a = read_blob(base_path, rel, int(v_from)) or b''
        if v_to:
            b = read_blob(base_path, rel, int(v_to)) or b''
        try:
            a_text = a.decode('utf-8', 'replace').splitlines(keepends=True)
            b_text = b.decode('utf-8', 'replace').splitlines(keepends=True)
        except (AttributeError, LookupError) as e:
            # `a`/`b` not bytes-like, or codec missing — skip this file.
            logger.debug('[FileHistory] diff_text decode failed for %s: %s', rel, e)
            continue
        diff = list(difflib.unified_diff(a_text, b_text,
                                         fromfile=f'a/{rel}',
                                         tofile=f'b/{rel}'))
        if diff:
            pieces.append(''.join(diff))
        if sum(len(p) for p in pieces) > max_chars:
            pieces.append(f'\n… [truncated at {max_chars} chars] …\n')
            break
    return ''.join(pieces)[:max_chars]


# Re-export blob path resolver so tests can poke at the disk layout
# without importing the store directly.
_BACKUP_BLOB_PATH = backup_blob_path
