"""Per-round file-history SNAPSHOT daemon.

  - ``_spawn_async_commit_round`` / ``_run_commit_round_async`` — run
    ``file_history.make_snapshot`` in a daemon thread so the snapshot persist
    can't block ``persist_task_result`` → ``_dispatch_queued_message``; emit a
    ``round_committed`` SSE event + enrich ``modifiedFileList`` with
    opaque-writer (code_exec / MCP) side-effects the journal misses.
  - ``_patch_assistant_message_with_git`` — persist the snapshotId onto the
    conversation's assistant message after the SSE reader may have closed.

Dependency is one-directional: imports from ``lib.agent_core.events`` +
``lib.tasks_pkg.manager`` (append_event), never the reverse.  The actual
``make_snapshot`` lives in ``lib.file_history`` (imported lazily inside the
daemon body) — this module never redefines it.
"""

from __future__ import annotations

import os
import threading
import time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _spawn_async_commit_round(task: dict, project_enabled: bool, project_path: str | None) -> None:
    """Run ``file_history.make_snapshot`` in a daemon thread.

    Decoupled from ``_finalize_and_emit_done`` so the snapshot persist
    cannot block ``persist_task_result`` → ``_dispatch_queued_message``.
    On success, emits a ``round_committed`` SSE event carrying
    ``snapshotId`` (and ``gitSha`` for backward-compat) plus any
    file-history-derived ``modifiedFileList`` additions.
    """
    if not (project_enabled and project_path and task.get('id')):
        return
    try:
        threading.Thread(
            target=_run_commit_round_async,
            args=(task, project_path),
            name=f'commit-round-{task["id"][:8]}',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning('[Task:%s] failed to spawn async commit thread: %s',
                       task['id'][:8], e, exc_info=True)


def _run_commit_round_async(task: dict, project_path: str) -> None:
    """Daemon-thread body for the deferred ``make_snapshot`` call.

    Uses the file-history store (lib.file_history) — the previous
    shadow-git shim was retired in the Tier-3 redesign.  See
    ``lib/file_history/__init__.py`` for the rationale.
    """
    tid = task['id'][:8]
    try:
        from lib import file_history as fh
        from lib.file_history.store import _project_lock as _fh_project_lock
        from lib.file_history.store import load_tracked as _fh_load_tracked
        from lib.project_mod import get_modifications

        if not fh.is_enabled():
            return

        # Pull actual tool names (mod['type']) from this task's modifications.
        _tool_names: list[str] = []
        _rel_paths: list[str] | None = None
        try:
            _turn_mods = [
                m for m in (get_modifications(project_path, conv_id=task.get('convId')) or [])
                if m.get('taskId') == task['id']
            ]
            _tool_names = [m.get('type') or '' for m in _turn_mods]
            _tool_names = [t for t in _tool_names if t]
            _rel_paths = [m.get('path') for m in _turn_mods if m.get('path')]
        except Exception as _e:
            logger.debug('[Task:%s] async tool_names/rel_paths extraction failed: %s',
                         tid, _e)

        # ── Atomic commit region (Fix 3) ───────────────────────────
        # The sequence
        #   prev_snap  = get_last_snapshot_id(...)
        #   _snap_id   = make_snapshot(...)
        #   fh_changes = diff_name_status(prev_snap, _snap_id)
        #   tracked    = load_tracked(...)              # for Fix 2
        # MUST run atomically against the per-project file-history
        # store.  Each individual call already takes the
        # ``_project_lock`` via ``@with_project_lock``, but releasing
        # it between calls lets a concurrent commit thread (from
        # another conversation pointing at the same project root)
        # advance the snapshot log and ``tracked.json`` between our
        # ``prev_snap`` capture and our ``make_snapshot``.  When that
        # happens, our snapshot's file map ends up containing the
        # OTHER task's edits too, and ``diff_name_status`` then
        # attributes those edits to OUR round.  Holding the
        # re-entrant lock across the whole sequence closes the window.
        # The store's per-call ``with_project_lock`` re-acquires the
        # same RLock, which is a no-op while we're holding it.
        fh_changes: list[dict] = []
        tracked_index: dict = {}
        with _fh_project_lock(project_path):
            # Find the snapshot that was active before this round
            # started, so diff_name_status can isolate just the round's
            # changes.
            prev_snap = fh.get_last_snapshot_id(project_path)

            _t0 = time.time()
            _snap_id = fh.make_snapshot(
                project_path,
                task_id=task['id'],
                conv_id=task.get('convId'),
                tool_names=_tool_names or None,
                summary=task.get('toolSummary'),
                rel_paths=_rel_paths or None,
            )
            _elapsed = time.time() - _t0
            if not _snap_id:
                logger.debug('[Task:%s] async make_snapshot returned no id (no-op or disabled) elapsed=%.2fs',
                             tid, _elapsed)
                return

            # Diff + tracked-index snapshot still inside the lock so
            # last_writer_task_id reflects the writers as of this
            # snapshot's instant.
            try:
                fh_changes = fh.diff_name_status(project_path, prev_snap, _snap_id) or []
            except Exception as _e:
                logger.debug('[Task:%s] async diff_name_status fallback: %s',
                             tid, _e)
                fh_changes = []
            try:
                tracked_index = _fh_load_tracked(project_path) or {}
            except Exception as _e:
                logger.debug('[Task:%s] async load_tracked fallback: %s', tid, _e)
                tracked_index = {}

        # Keep ``gitSha`` field for backward-compat with the frontend (which
        # captures it onto _gitSha for prospective undo UI but doesn't
        # currently consume it).  ``snapshotId`` is the new canonical name.
        task['snapshotId'] = _snap_id
        task['gitSha'] = _snap_id
        if _elapsed > 1.0:
            logger.info('[Task:%s] async make_snapshot completed in %.2fs id=%s',
                        tid, _elapsed, _snap_id[:8])

        amend_evt = build_event(EventType.ROUND_COMMITTED,
                                snapshotId=_snap_id,
                                gitSha=_snap_id,
                                taskId=task['id'])

        # File-history-derived additions (run_command / code_exec / MCP side
        # effects that modifications.py doesn't track) come from
        # diff_name_status against the prior snapshot.
        #
        # Fix 2 — per-task attribution: filter the diff to keep ONLY
        # paths whose latest tracked-index entry was last written by
        # THIS task.  Any path whose ``last_writer_task_id`` is some
        # other task belongs to a concurrent conversation operating
        # on the same project root and must not be reported here.
        # ── The fh diff is ENRICHMENT ONLY, never a source of truth. ──
        # The authoritative ``modifiedFileList`` was already built in
        # ``_finalize_and_emit_done`` from this round's OWN writes
        # (modifications journal, aggregated across all roots) — a
        # conversation-isolated signal.  The fh diff is computed against
        # the PRIMARY root's project-global snapshot index, so it
        # legitimately catches only one thing the journal can't: file
        # edits made by OPAQUE writers that don't stamp attribution —
        # ``code_exec`` and arbitrary MCP tools.  (``run_command`` IS
        # journalled by modifications.py, and the file-edit tools
        # write_file / apply_diff(s) / insert_content(s) journal AND
        # stamp ``last_writer_task_id`` on their own tracked entries.)
        #
        # So an fh diff path is only legitimately OURS when:
        #   • its tracked entry's ``last_writer_task_id`` == this task, OR
        #   • the entry is UNATTRIBUTED (empty writer) AND this round ran
        #     an OPAQUE writer that could have produced an unstamped edit.
        # Any other empty-writer path is concurrent-conversation drift on
        # the shared primary root (e.g. another session journalling) and
        # MUST be dropped — that was the cross-conversation leak that let
        # a foreign file appear while this round's real (extra-root) edits
        # were missing.
        #
        # ``_TRACKED_EDIT_TOOLS`` and the read-only set both stamp/leave
        # NO unattributed edits, so a round running only those cannot own
        # an empty-writer path.  Probe by ACTUAL tool name; unknown names
        # (custom MCP tools) count as opaque writers — fail open so a
        # genuine side-channel edit is never suppressed.
        _READ_ONLY_TOOLS = frozenset({
            'list_dir', 'read_files', 'grep_search', 'find_files',
            'web_search', 'fetch_url', 'inspect_image',
        })
        _TRACKED_EDIT_TOOLS = frozenset({
            'write_file', 'apply_diff', 'apply_diffs',
            'insert_content', 'insert_contents', 'run_command',
        })
        _round_has_opaque_writer = False
        try:
            for _r in (task.get('toolRounds') or []):
                if not isinstance(_r, dict):
                    continue
                _tn = _r.get('toolName') or _r.get('tool_name') or ''
                if not _tn:
                    continue
                if _tn in _READ_ONLY_TOOLS or _tn in _TRACKED_EDIT_TOOLS:
                    continue
                # Anything else (code_exec / MCP / unknown) may write
                # without stamping attribution.
                _round_has_opaque_writer = True
                break
        except Exception as _e:
            logger.debug('[Task:%s] fh opaque-writer probe failed: %s', tid, _e)
            _round_has_opaque_writer = True  # fail open — never over-suppress

        try:
            if fh_changes:
                _own_task_id = task.get('id') or ''
                _filtered: list[dict] = []
                _dropped = 0
                _dropped_drift = 0
                for entry in fh_changes:
                    _writer = (tracked_index.get(entry.get('path'), {})
                               .get('last_writer_task_id') or '')
                    if _writer and _writer != _own_task_id:
                        # Attributed to another concurrent task — always drop.
                        _dropped += 1
                    elif not _writer and not _round_has_opaque_writer:
                        # Unattributed path on a round that ran no opaque
                        # writer — it cannot be ours.  Drop (closes the
                        # concurrent-conversation leak).
                        _dropped_drift += 1
                    else:
                        _filtered.append(entry)
                if _dropped:
                    logger.info('[Task:%s] fh side-channel dropped %d path(s) '
                                'attributable to other concurrent task(s)',
                                tid, _dropped)
                if _dropped_drift:
                    logger.info('[Task:%s] fh side-channel dropped %d unattributed '
                                'path(s) on a round with no opaque writer', tid, _dropped_drift)
                fh_changes = _filtered
        except Exception as _e:
            logger.debug('[Task:%s] fh attribution filter failed: %s', tid, _e)

        # Dedup must use the same root-tagging convention that
        # ``modifications.py`` uses when it records a write.  That code
        # reverse-looks-up ``base_path`` in the global ``_roots`` registry
        # and stores the matching root NAME on each mod.  When the merger
        # in ``_emit_done_event`` later builds ``modifiedFileList`` it
        # carries that ``root`` field through.  If we naively dedup the
        # fh side-channel by ``('', path)`` here, every file that
        # modifications.py already recorded with a non-empty ``root``
        # would be re-added by us — producing duplicate rows in the
        # frontend's "files changed" bar (one entry with the root prefix,
        # one without).  Resolve the project root's NAME first and use
        # it as the dedup key so we collapse against the existing entry.
        try:
            if fh_changes:
                fh_root = ''
                try:
                    from lib.project_mod.config import _lock as _proj_lock
                    from lib.project_mod.config import _roots as _proj_roots
                    _abs_proj = os.path.abspath(project_path)
                    with _proj_lock:
                        for _rn, _rs in _proj_roots.items():
                            if os.path.abspath(_rs.get('path') or '') == _abs_proj:
                                fh_root = _rn
                                break
                except Exception as _re:
                    logger.debug('[Task:%s] fh_root lookup failed for %s: %s',
                                 tid, project_path, _re)

                existing = list(task.get('modifiedFileList') or [])
                seen_paths: set[tuple[str, str]] = set()
                for f in existing:
                    if not isinstance(f, dict):
                        continue
                    p = f.get('path', '')
                    r = f.get('root', '') or ''
                    seen_paths.add((r, p))
                    # Also record an unrooted alias so a fh entry that
                    # does not (yet) know the root name still dedups
                    # against an existing rooted entry for the same file.
                    seen_paths.add(('', p))
                added: list[dict] = []
                for entry in fh_changes:
                    p = entry['path']
                    if (fh_root, p) in seen_paths or ('', p) in seen_paths:
                        continue
                    item = {'path': p, 'action': entry['action']}
                    if fh_root:
                        item['root'] = fh_root
                    existing.append(item)
                    added.append(item)
                    seen_paths.add((fh_root, p))
                    seen_paths.add(('', p))
                if added:
                    task['modifiedFileList'] = existing
                    task['modifiedFiles'] = len(existing)
                    amend_evt['modifiedFileList'] = existing
                    amend_evt['modifiedFiles'] = len(existing)
                    amend_evt['addedByGit'] = added
                    logger.info('[Task:%s] async file-history modifiedFileList '
                                'added %d file(s) missed by modifications.py '
                                '(root=%s)', tid, len(added), fh_root or '-')
        except Exception as _e:
            logger.debug('[Task:%s] async diff_name_status fallback: %s',
                         tid, _e)

        # Emit the amend event so any still-connected SSE reader can wire
        # snapshotId onto the assistant message.
        try:
            append_event(task, amend_evt)
        except Exception as _e:
            logger.debug('[Task:%s] append_event for round_committed failed: %s',
                         tid, _e)

        # ── Persist snapshotId to the conversation DB so reloads after
        #    the SSE reader has closed still see it for the redo UI. ──
        try:
            _patch_assistant_message_with_git(task, amend_evt)
        except Exception as _e:
            from lib.database import log_db_finalize_error
            log_db_finalize_error(logger, 'warning', _e,
                                  f'[Task:{tid}] failed to patch assistant message with snapshotId')
    except Exception as e:
        logger.warning('[Task:%s] async make_snapshot failed: %s',
                       tid, e, exc_info=True)


def _patch_assistant_message_with_git(task: dict, amend_evt: dict) -> None:
    """Stamp gitSha + git-derived files onto THIS task's assistant message.

    Called from the async commit thread after ``persist_task_result`` has
    already run.  Mirrors the subset of ``_sync_result_to_conversation``
    that depends on git output.

    Goes through the store's field-level patch rather than rewriting the whole
    transcript: this daemon races the autopilot's VU append (both fire within
    the same second at turn settle), and a read-modify-write of the entire blob
    erased the appended row — measured 13 appends vs 8 survivors on conv
    ms3sfyrmn31omb.  The patch re-reads under a rev-CAS so a concurrent append
    survives.
    """
    conv_id = task.get('convId') or ''
    task_id = task.get('id') or ''
    git_sha = amend_evt.get('gitSha')
    if not (conv_id and task_id and git_sha):
        return
    fields = {
        '_gitSha': git_sha,
        '_snapshotId': amend_evt.get('snapshotId') or git_sha,
    }
    if amend_evt.get('modifiedFileList'):
        fields['modifiedFileList'] = amend_evt['modifiedFileList']
    if amend_evt.get('modifiedFiles'):
        fields['modifiedFiles'] = amend_evt['modifiedFiles']

    from lib.agent_core.store import get_conversation_store
    store = get_conversation_store()
    try:
        if store.patch_message_fields_by_task(conv_id, task_id, fields):
            logger.info('[Task:%s] persisted gitSha=%s to conv=%s',
                        task_id[:8], git_sha[:12], conv_id[:8])
    except Exception as _e:
        logger.warning('[Task:%s] gitSha DB write failed: %s',
                       task_id[:8], _e, exc_info=True)
