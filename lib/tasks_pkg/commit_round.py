"""Per-round file-history commit + modified-file derivation.

Extracted from ``lib/tasks_pkg/orchestrator.py`` (2026-06-24) — a
self-contained, daemon-thread-decoupled file-history concern with no
coupling to the orchestration loop beyond the task dict + event helpers.

  - ``derive_round_modified_files`` — build a round's authoritative file-change
    list from the per-root modifications journal (conversation-isolated via
    taskId stamping). Called by ``_finalize_and_emit_done``.
  - ``_spawn_async_commit_round`` / ``_run_commit_round_async`` — run
    ``file_history.make_snapshot`` in a daemon thread so the snapshot persist
    can't block ``persist_task_result`` → ``_dispatch_queued_message``; emit a
    ``round_committed`` SSE event + enrich ``modifiedFileList`` with
    opaque-writer (code_exec / MCP) side-effects the journal misses.
  - ``_patch_assistant_message_with_git`` — persist the snapshotId onto the
    conversation's assistant message after the SSE reader may have closed.

``orchestrator`` imports all four back, so its internal call sites are
unchanged. Dependency is one-directional: this module imports from
``lib.agent_core.events`` + ``lib.tasks_pkg.manager`` (append_event), never the
reverse.
"""

from __future__ import annotations

import os
import threading
import time

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def derive_round_modified_files(task: dict, project_path: str | None,
                                project_paths: list[str] | None) -> tuple[list[dict], int, bool]:
    """Build this round's authoritative file-change list from the journal.

    The modifications journal is keyed per-root (``session_dir =
    md5(base_path)``), so a write to an EXTRA workspace root lands in THAT
    root's journal — not the primary's.  Scanning only ``project_path``
    (the primary) makes extra-root edits invisible, which in turn lets the
    project-global file-history side-channel seed ``modifiedFileList`` with
    a CONCURRENT conversation's edit instead of this round's real edits.

    This helper scans the primary root PLUS every extra root in
    ``project_paths[1:]``, keeps only modifications stamped with THIS
    task's id (falling back to a start-timestamp filter for legacy mods),
    and returns ``(file_list, count, used_ts_fallback)``.  Because each mod
    is taskId-stamped at write time, the result is conversation-isolated
    and cannot leak across conversations.

    Args:
        task: The task dict (needs ``id``, ``convId``, ``created_at``).
        project_path: Primary workspace root abs path.
        project_paths: Full ``cfg['projectPaths']`` list (index 0 == primary);
            indices 1.. are extra roots.

    Returns:
        ``(file_list, count, used_ts_fallback)`` where ``file_list`` is a
        list of ``{path, action, root?}`` dicts keyed uniquely by
        ``(root, path)``.
    """
    from lib.project_mod import get_modifications

    conv_id = task.get('convId')
    scan_roots: list[str] = []
    seen_roots: set[str] = set()
    for p in ([project_path] + list((project_paths or [])[1:])):
        if p and p not in seen_roots:
            seen_roots.add(p)
            scan_roots.append(p)

    turn_mods: list[dict] = []
    used_ts_fallback = False
    for root in scan_roots:
        root_mods = get_modifications(root, conv_id=conv_id) or []
        if not root_mods:
            continue
        own = [m for m in root_mods if m.get('taskId') == task.get('id')]
        if not own:
            task_start = task.get('created_at', 0)
            own = [m for m in root_mods if m.get('timestamp', 0) >= task_start]
            if own:
                used_ts_fallback = True
        turn_mods.extend(own)

    if not turn_mods:
        return [], 0, used_ts_fallback

    seen: dict[tuple[str, str], dict] = {}
    for m in turn_mods:
        p = m.get('path', '?')
        t = m.get('type', '')
        root_name = m.get('root', '') or ''
        if t == 'write_file':
            action = 'created' if not m.get('existed', True) else 'written'
        elif t in ('apply_diff', 'apply_diffs'):
            action = 'patched'
        elif t in ('insert_content', 'insert_contents'):
            action = 'inserted'
        elif t == 'run_command':
            # Resolve the exists-check against the mod's OWN root
            # (basePath), not the primary, so extra-root deletes classify
            # correctly.
            base = m.get('basePath') or project_path or ''
            abs_p = p if os.path.isabs(p) else os.path.join(base, p)
            if not m.get('existed', True):
                action = 'created'
            elif 'originalContent' in m and not os.path.exists(abs_p):
                action = 'deleted'
            else:
                action = 'modified'
        else:
            action = t
        seen[(root_name, p)] = {'action': action, 'root': root_name}

    file_list = [
        {'path': p, 'action': info['action'],
         **({'root': info['root']} if info['root'] else {})}
        for (root_name, p), info in seen.items()
    ]
    return file_list, len(turn_mods), used_ts_fallback


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


def _spawn_async_profile_consolidation(task: dict, messages: list,
                                       cfg: dict | None = None) -> None:
    """Run the layer-3 preference consolidation in a daemon thread.

    Decoupled from ``_finalize_and_emit_done`` so the per-turn cheap-LLM
    consolidation round-trip can NEVER sit on the path between loop-exit and
    the ``done`` event — the user sees the turn finish immediately, and any
    "Noted: you prefer X" moment arrives a beat later as a post-done
    ``preference_learned`` event (best-effort live + persisted for reload).

    Gated on ``task['_profileConsolidateEligible']`` (set at the prefetch gate
    where ``memory_enabled``/``has_real_tools`` are in scope) and a clean
    finish (no error). ``messages`` is captured by reference — the consolidation
    pass only READS it (recent-surface extraction), so the post-done snapshot
    is fine.
    """
    if task.get('error') or not task.get('_profileConsolidateEligible'):
        return
    if not task.get('id'):
        return
    try:
        threading.Thread(
            target=_run_profile_consolidation_async,
            args=(task, messages),
            name=f'profile-consolidate-{task["id"][:8]}',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning('[Task:%s] failed to spawn consolidation thread: %s',
                       task['id'][:8], e, exc_info=True)


def _run_profile_consolidation_async(task: dict, messages: list) -> None:
    """Daemon-thread body: run consolidation, emit + persist learned prefs."""
    tid = task['id'][:8]
    try:
        from lib.memory.profile_consolidate import run_profile_consolidation
        learned = run_profile_consolidation(messages, task=task)
    except Exception as e:
        logger.warning('[Task:%s] profile consolidation failed: %s',
                       tid, e, exc_info=True)
        return
    if not learned:
        return

    task['_preferencesLearned'] = learned
    # Best-effort LIVE delivery: append_event fans out over SSE + push to any
    # still-connected client (and a disconnected client recovers it via the
    # DB patch below on reload).
    for pref in learned:
        try:
            append_event(task, build_event(
                EventType.PREFERENCE_LEARNED,
                kind=pref.get('kind', ''),
                summary=pref.get('summary', ''),
                pending=bool(pref.get('pending')),
                id=pref.get('id', ''),
            ))
        except Exception as e:
            logger.debug('[Task:%s] preference_learned emit failed: %s', tid, e)

    # Persist onto the conversation's assistant message so the chip survives a
    # reload even when the SSE reader already closed (mirrors
    # _patch_assistant_message_with_git).
    try:
        _patch_assistant_message_with_prefs(task, learned)
    except Exception as e:
        logger.warning('[Task:%s] persist preferences_learned failed: %s',
                       tid, e, exc_info=True)


def _patch_assistant_message_with_prefs(task: dict, learned: list) -> None:
    """Write ``_preferencesLearned`` onto the conversation's assistant message.

    Called from the consolidation daemon AFTER ``persist_task_result`` ran, so
    the chip is recoverable on reload. Mirrors
    :func:`_patch_assistant_message_with_git`'s message-locating logic.
    """
    conv_id = task.get('convId') or ''
    task_id = task.get('id') or ''
    if not (conv_id and task_id and learned):
        return
    from lib.agent_core.store import get_conversation_store
    store = get_conversation_store()
    loaded = store.load_conversation_messages(conv_id)
    if loaded is None:
        return
    messages, _updated_at = loaded
    if not isinstance(messages, list) or not messages:
        return
    target_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict) or m.get('role') != 'assistant':
            continue
        if m.get('_taskId') == task_id:
            target_idx = i
            break
        if target_idx == -1:
            target_idx = i
    if target_idx < 0:
        return
    messages[target_idx]['_preferencesLearned'] = learned
    try:
        store.save_conversation_messages(conv_id, messages)
        logger.info('[Task:%s] persisted %d preference_learned to conv=%s msg[%d]',
                    task_id[:8], len(learned), conv_id[:8], target_idx)
    except Exception as e:
        logger.warning('[Task:%s] preferences_learned DB write failed: %s',
                       task_id[:8], e, exc_info=True)


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
            logger.warning('[Task:%s] failed to patch assistant message with snapshotId: %s',
                           tid, _e, exc_info=True)
    except Exception as e:
        logger.warning('[Task:%s] async make_snapshot failed: %s',
                       tid, e, exc_info=True)


def _patch_assistant_message_with_git(task: dict, amend_evt: dict) -> None:
    """Update the conversation's last assistant message with gitSha + git-derived files.

    Called from the async commit thread after ``persist_task_result`` has
    already run.  Mirrors the subset of ``_sync_result_to_conversation``
    that depends on git output.
    """
    conv_id = task.get('convId') or ''
    task_id = task.get('id') or ''
    git_sha = amend_evt.get('gitSha')
    if not (conv_id and task_id and git_sha):
        return
    from lib.agent_core.store import get_conversation_store
    store = get_conversation_store()
    loaded = store.load_conversation_messages(conv_id)
    if loaded is None:
        return
    messages, _updated_at = loaded
    if not isinstance(messages, list) or not messages:
        return

    # Locate the assistant message for this task.  Prefer matching by
    # _taskId (set by _sync_result_to_conversation); fall back to the
    # last assistant message if not tagged.
    target_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if not isinstance(m, dict):
            continue
        if m.get('role') != 'assistant':
            continue
        if m.get('_taskId') == task_id:
            target_idx = i
            break
        if target_idx == -1:
            target_idx = i  # remember last assistant as fallback
            # Don't break — keep looking for an exact taskId match.
    if target_idx < 0:
        return
    msg = messages[target_idx]
    msg['_gitSha'] = git_sha
    msg['_snapshotId'] = amend_evt.get('snapshotId') or git_sha
    if amend_evt.get('modifiedFileList'):
        msg['modifiedFileList'] = amend_evt['modifiedFileList']
    if amend_evt.get('modifiedFiles'):
        msg['modifiedFiles'] = amend_evt['modifiedFiles']

    try:
        store.save_conversation_messages(conv_id, messages)
        logger.info('[Task:%s] persisted gitSha=%s to conv=%s msg[%d]',
                    task_id[:8], git_sha[:12], conv_id[:8], target_idx)
    except Exception as _e:
        logger.warning('[Task:%s] gitSha DB write failed: %s',
                       task_id[:8], _e, exc_info=True)

