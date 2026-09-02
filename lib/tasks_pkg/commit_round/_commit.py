"""Per-round file-history SNAPSHOT daemon.

  - ``_spawn_async_commit_round`` / ``_run_commit_round_async`` — run
    ``file_history.make_snapshot`` in a daemon thread so the snapshot persist
    can't block ``persist_task_result`` → ``_dispatch_queued_message``; emit a
    ``round_committed`` SSE event + enrich ``modifiedFileList`` with
    opaque-writer (code_exec / MCP) side-effects the journal misses.
  - ``_patch_turn_projection_with_file_list`` — fold the derived file list
    into the settled turn-native projection (the turn authority refuses
    post-settlement event frames, so the card data needs this CAS seam).

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


def _note_write_set_advisories(
    task: dict,
    changed_files: list[dict],
    project_path: str,
) -> None:
    """Emit owner-scoped write-set drift notes for one committed file list."""
    try:
        from lib.tasks_pkg.manager import task_user_id
        from lib.write_set_advisory import note_project_write

        owner_user_id = int(task_user_id(task))
        for changed_file in changed_files:
            if not isinstance(changed_file, dict):
                continue
            changed_path = changed_file.get('path') or ''
            changed_root = changed_file.get('root') or project_path
            if changed_path:
                note_project_write(
                    task['convId'],
                    changed_root,
                    changed_path,
                    user_id=owner_user_id,
                )
    except Exception as error:
        logger.debug(
            '[Task:%s] write-set advisory failed: %s',
            str(task.get('id') or '')[:8],
            error,
        )


def _spawn_async_commit_round(task: dict, project_enabled: bool,
                              project_path: str | None,
                              project_paths: list[str] | None = None) -> None:
    """Run ``file_history.make_snapshot`` in a daemon thread.

    Decoupled from ``_finalize_and_emit_done`` so the snapshot persist
    cannot block ``persist_task_result`` → ``_dispatch_queued_message``.
    On success, emits a ``round_committed`` SSE event carrying
    ``snapshotId`` (and ``gitSha`` for backward-compat) plus the
    journal-derived ``modifiedFileList`` and any file-history-derived
    side-channel additions.
    """
    if not (project_enabled and project_path and task.get('id')):
        return
    try:
        threading.Thread(
            target=_run_commit_round_async,
            args=(task, project_path, project_paths),
            name=f'commit-round-{task["id"][:8]}',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning('[Task:%s] failed to spawn async commit thread: %s',
                       task['id'][:8], e, exc_info=True)


def _run_commit_round_async(task: dict, project_path: str,
                            project_paths: list[str] | None = None) -> None:
    """Daemon-thread body for the deferred snapshot + file-list work.

    Uses the file-history store (lib.file_history) — the previous
    shadow-git shim was retired in the Tier-3 redesign.  See
    ``lib/file_history/__init__.py`` for the rationale.

    Also runs ``derive_round_modified_files`` here (moved OFF the done hot
    path) so the authoritative per-round file list is built + persisted
    without ever blocking the terminal frame.
    """
    tid = task['id'][:8]
    linear_checkpoint: dict | None = None
    try:
        from lib.linear_git_checkpoint import settle_task_checkpoint
        from lib.tasks_pkg.manager import task_user_id
        linear_checkpoint = settle_task_checkpoint(
            task, user_id=task_user_id(task), project_path=project_path,
            project_paths=project_paths)
        if linear_checkpoint:
            logger.info(
                '[Task:%s] linear Git checkpoint settled status=%s repos=%d',
                tid, linear_checkpoint.get('status'),
                len(linear_checkpoint.get('repositories') or []),
            )
    except Exception as error:
        # Checkpointing is a best-effort post-task observer. It must never
        # rewrite the already-settled task result or block later project tools.
        logger.warning('[Task:%s] linear Git checkpoint settlement failed: %s',
                       tid, error, exc_info=True)

    linear_event_fields = (
        {'linearGitCheckpoint': linear_checkpoint}
        if linear_checkpoint else {})
    try:
        from lib import file_history as fh
        from lib.file_history.store import _project_lock as _fh_project_lock
        from lib.file_history.store import load_tracked as _fh_load_tracked
        from lib.project_mod import get_modifications

        # ── Authoritative modified-file list (moved OFF the done hot path) ──
        # The per-root modifications journal + per-mod filesystem probes used
        # to run inline in _finalize_and_emit_done between the last round_end
        # and the terminal done.  Do it here so the done frame is never
        # blocked by journal I/O; the result rides out on round_committed and
        # is folded into the settled turn projection below.
        _own_files: list[dict] = []
        _used_ts_fallback = False
        try:
            from lib.tasks_pkg.commit_round._derive import derive_round_modified_files
            _own_files, _n_mods, _used_ts_fallback = derive_round_modified_files(
                task, project_path, project_paths)
        except Exception as _e:
            logger.debug('[Task:%s] derive_round_modified_files failed: %s',
                         tid, _e)
        if _used_ts_fallback:
            logger.info('[Task:%s] modifiedFileList derived via timestamp fallback '
                        '(%d file(s))', tid, len(_own_files))
        # Merge any continue-flow checkpoint list (prior rounds) so the full
        # turn's list is what gets persisted, exactly as the old synchronous
        # finalize assembled it before emit.
        _cp_mod_list = task.get('_checkpointModifiedFileList')
        if _cp_mod_list:
            _merged_map: dict[tuple[str, str], dict] = {}
            def _mkey(f):
                if isinstance(f, dict):
                    return (f.get('root', '') or '', f.get('path', ''))
                return ('', str(f))
            for _f in _cp_mod_list:
                _merged_map[_mkey(_f)] = _f
            for _f in _own_files:
                _merged_map[_mkey(_f)] = _f
            _own_files = list(_merged_map.values())
        if _own_files:
            task['modifiedFileList'] = _own_files
            # Unique (root, path) count across checkpoint + this round —
            # adding the raw mod counts would double-count a file touched in
            # both and the card's headline would disagree with its own list.
            task['modifiedFiles'] = len(_own_files)

        # Presence: merge this turn's touched files into the peer registry.
        # Best-effort — moved here with the derivation it depends on.
        if _own_files and task.get('convId'):
            try:
                from lib.presence import record_files as _presence_record
                from lib.tasks_pkg.manager import task_user_id
                _presence_record(
                    project_path,
                    task['convId'],
                    _own_files,
                    user_id=int(task_user_id(task)),
                )
            except Exception as _pe:
                logger.debug('[Task:%s] presence record_files failed: %s',
                             tid, _pe)

            # Write-set drift is judged only after the round journal has
            # attributed files to this task. This seam owns all required
            # authority: authenticated owner, conversation, project roots and
            # the canonical file list. Low-level atomic file writers remain
            # storage-agnostic and cannot accidentally guess a tenant.
            _note_write_set_advisories(task, _own_files, project_path)

        if not fh.is_enabled():
            # No snapshot to anchor the round_committed event, but the journal
            # list above is still authoritative — persist/emit it independently.
            if _own_files:
                _files_evt = build_event(
                    EventType.ROUND_COMMITTED,
                    taskId=task['id'],
                    modifiedFileList=_own_files,
                    modifiedFiles=task.get('modifiedFiles'),
                    **linear_event_fields,
                )
                try:
                    append_event(task, _files_evt)
                except Exception as _e:
                    logger.debug('[Task:%s] append_event for file-list '
                                 'round_committed failed: %s', tid, _e)
                _patch_turn_projection_with_file_list(task, _files_evt)
            elif linear_event_fields:
                try:
                    append_event(task, build_event(
                        EventType.ROUND_COMMITTED,
                        taskId=task['id'],
                        **linear_event_fields,
                    ))
                except Exception as _e:
                    logger.debug('[Task:%s] append_event for linear Git '
                                 'checkpoint failed: %s', tid, _e)
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
                if _own_files or linear_event_fields:
                    _no_snapshot_evt = build_event(
                        EventType.ROUND_COMMITTED,
                        taskId=task['id'],
                        modifiedFileList=_own_files or None,
                        modifiedFiles=(task.get('modifiedFiles')
                                       if _own_files else None),
                        **linear_event_fields,
                    )
                    try:
                        append_event(task, _no_snapshot_evt)
                    except Exception as _e:
                        logger.debug('[Task:%s] append_event for no-snapshot '
                                     'round commit failed: %s', tid, _e)
                    _patch_turn_projection_with_file_list(
                        task, _no_snapshot_evt)
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
                                taskId=task['id'],
                                **linear_event_fields)
        # Authoritative journal-derived list (built above, off the hot path)
        # rides on the same round_committed frame as the snapshot id.
        if task.get('modifiedFileList'):
            amend_evt['modifiedFileList'] = task['modifiedFileList']
            amend_evt['modifiedFiles'] = task.get('modifiedFiles')

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
        # The authoritative ``modifiedFileList`` was already built ABOVE
        # (``derive_round_modified_files``, moved here off the done hot
        # path) from this round's OWN writes (modifications journal,
        # aggregated across all roots) — a conversation-isolated signal.
        # The fh diff is computed against the PRIMARY root's project-global
        # snapshot index, so it legitimately catches only one thing the
        # journal can't: file edits made by OPAQUE writers that don't stamp
        # attribution — ``code_exec`` and arbitrary MCP tools.
        # (``run_command`` IS journalled by modifications.py, and the
        # file-edit tools write_file / apply_diff(s) / insert_content(s)
        # journal AND stamp ``last_writer_task_id`` on their own tracked
        # entries.)
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
            'write_file', 'edit_file', 'apply_diff', 'apply_diffs',
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

        _patch_turn_projection_with_file_list(task, amend_evt)
    except Exception as e:
        logger.warning('[Task:%s] async make_snapshot failed: %s',
                       tid, e, exc_info=True)


def _patch_turn_projection_with_file_list(task: dict, amend_evt: dict) -> None:
    """Fold the derived file list into the settled turn-native projection.

    The turn-native UI reads ``storage_conversation_turns.projection_json``,
    and the post-settlement ``round_committed`` frame is refused by
    ``record_task_event`` (the attempt is no longer live).  Without this seam
    the files-changed card never renders on turn-native conversations
    (2026-08-26 regression from moving derivation off the done hot path).

    This CAS seam is the ONLY post-settlement persistence path: turn identity
    keys (``_taskId``) are deliberately stripped from projections by
    ``normalize_projection_document``, so the retired legacy bridge that
    looked a turn up by ``projection._taskId`` could never match — and its
    ``_gitSha``/``_snapshotId`` payload is outside
    ``PUBLIC_PROJECTION_FIELDS`` and would have been stripped on write
    anyway.  Undo/redo resolves through the ``fileChanges.taskId`` block and
    the project-mod journal, not through any snapshot id on the message.
    """
    file_list = amend_evt.get('modifiedFileList')
    conv_id = task.get('convId') or ''
    turn_id = task.get('_turnId') or ''
    if not (file_list and conv_id and turn_id):
        return
    try:
        from lib.turn_lifecycle import apply_commit_round_file_changes
        from lib.tasks_pkg.manager import task_user_id
        apply_commit_round_file_changes(
            conv_id,
            turn_id,
            files=file_list,
            modified_count=amend_evt.get('modifiedFiles'),
            task_id=str(task.get('id') or ''),
            user_id=task_user_id(task),
        )
    except Exception as error:
        logger.debug('[Task:%s] turn-projection file-changes fold failed: %s',
                     str(task.get('id') or '')[:8], error)
