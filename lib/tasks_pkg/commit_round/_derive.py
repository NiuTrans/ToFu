"""Per-round file facts from local journals and validated device/Overleaf commits.

``derive_round_modified_files`` builds a round's authoritative file-change
list from the per-root modifications journal (conversation-isolated via
taskId stamping).  Called by ``_finalize_and_emit_done``.

``note_live_file_change`` is the mid-run complement: the journal writer
(``lib.project_mod.modifications``) notifies it on every task-stamped record
so the live task's ``modifiedFileList`` grows DURING the turn and the
``fileChanges`` projection block renders before settlement.  Settlement
rebuilds local entries from the journal and preserves remote commit facts.
Both sources use the same bounded turn presentation, separate from durable
file history. Device envelopes are validated by the project tool handler;
``note_remote_file_changes`` binds their owner, task and addressed root.

Dependency is one-directional: this module reads ``lib.project_mod`` and
(lazily, inside the live stamp) the task registry — never the orchestration
loop.
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.project_mod.receipts import file_change_identity

logger = get_logger(__name__)


def derive_round_modified_files(task: dict, project_path: str | None,
                                project_paths: list[str] | None, *,
                                recover_conversation_journal: bool = False) -> tuple[list[dict], int, bool]:
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
    # Remote facts came from the addressed device's validated write result.
    # They are already in this task's canonical list; no host journal exists
    # for a remote pseudo-path and settlement must not erase these commits.
    external = [dict(row) for row in (task.get('modifiedFileList') or [])
                if isinstance(row, dict) and row.get('changeId')
                and (str(row.get('rootPath') or '').startswith('remote:') or row.get('source') == 'overleaf')]
    scan_roots: list[str] = []
    seen_roots: set[str] = set()
    for p in ([project_path] + list((project_paths or [])[1:])):
        if p and p not in seen_roots and not str(p).startswith('remote:'):
            seen_roots.add(p)
            scan_roots.append(p)

    # Roots admitted during this turn (for example linked worktrees) need
    # not be in the executor's frozen projectPaths. Committed live facts
    # retain the journal address even after Git removes the checkout. They
    # are discovery hints only: re-read the conversation/task journal rather
    # than promoting presentation rows into durable change evidence.
    configured_roots = set(scan_roots)
    if conv_id and task.get('id'):
        from lib.project_mod.config import get_conv_roots
        # Production roots are keyed by the physical executor. Legacy direct
        # file-tool callers still use the conversation key.
        root_policy = get_conv_roots(str(task['id'])) or get_conv_roots(conv_id)
        runtime_roots = [row['path'] for row in root_policy.values()]
        live_roots = [row.get('rootPath') for row in (task.get('modifiedFileList') or [])
                      if isinstance(row, dict) and row.get('changeId')
                      and row.get('source') == 'local']
        for root in runtime_roots + live_roots:
            if (isinstance(root, str) and os.path.isabs(root)
                    and root not in seen_roots):
                seen_roots.add(root)
                scan_roots.append(root)

    turn_mods: list[dict] = []
    used_ts_fallback = False
    for root in scan_roots:
        root_mods = get_modifications(root, conv_id=conv_id, include_content=False) or []
        if not root_mods:
            continue
        own = [m for m in root_mods if m.get('taskId') == task.get('id')]
        if not own and root in configured_roots:
            task_start = task.get('created_at', 0)
            # Orphan recovery explicitly covers prior physical attempts in
            # this conversation. Normal settlement never borrows their IDs.
            recover_prior_attempts = (recover_conversation_journal
                                      and not task.get('id') and bool(conv_id))
            own = [m for m in root_mods if (not m.get('taskId') or recover_prior_attempts)
                   and isinstance(task_start, (int, float)) and task_start > 0
                   and m.get('timestamp', 0) >= task_start]
            if own:
                used_ts_fallback = True
        turn_mods.extend(own)

    if not turn_mods:
        return external, len(external), used_ts_fallback

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
        elif t == 'run_command' and m.get('action') in ('created', 'deleted', 'modified'):
            # This is a committed command observation. Worktree removal after
            # integration cannot rewrite a modification as a command deletion.
            action = m['action']
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
        seen[(m.get('basePath') or root_name, p)] = {'action': action, 'root': root_name,
                                **({'changeId': m['changeId'], 'source': 'local'} if m.get('changeId') else {}),
                                **({'rootPath': m['basePath']} if m.get('changeId') and m.get('basePath') else {}),
                                **({'undoAvailable': False} if m.get('undoAvailable') is False else {})}
    file_list = [
        {'path': p, 'action': info['action'],
         **({'changeId': info['changeId'], 'source': info['source']} if info.get('changeId') else {}),
         **({'rootPath': info['rootPath']} if info.get('rootPath') else {}),

         **({'undoAvailable': False} if info.get('undoAvailable') is False else {}),
         **({'root': info['root']} if info['root'] else {})}
        for (root_name, p), info in seen.items()
    ]
    # The count is the UNIQUE-FILE count, never the raw journal-event count:
    # the "files changed" card headlines this number above the deduped list,
    # and a file edited five times must read as one changed file, not five.
    for entry in external:
        file_list = merge_live_file_entry(file_list, entry)
    return file_list, len(file_list), used_ts_fallback


def merge_live_file_entry(files: list[dict], entry: dict) -> list[dict]:
    """Fold ``entry`` into ``files`` by its explicit source/project/path identity.

    Copy-on-write: returns a fresh list so a projection fold iterating the
    task's previous list mid-stamp never sees a torn sequence.  An
    already-listed file keeps its first-seen position (the settlement
    ordering) with the newest action; a new file appends.
    """
    key = file_change_identity(entry)
    merged: list[dict] = []
    replaced = False
    for item in files:
        if not isinstance(item, dict):
            continue
        if file_change_identity(item) == key:
            if not replaced:
                merged.append(dict(entry))
                replaced = True
            continue
        merged.append(item)
    if not replaced:
        merged.append(dict(entry))
    return merged


def note_live_file_change(task_id: str | None, entry: dict) -> bool:
    """Fold one journaled file change into the LIVE task's file list.

    Best-effort presentation: never raises, never creates tasks, and refuses
    terminal/aborted tasks so a late tool callback cannot rewrite a settled
    list.  Seeds from ``_checkpointModifiedFileList`` so a continued turn
    shows its full-round list live, not only the current executor's edits.
    """
    if not task_id or not isinstance(entry, dict) or not entry.get('path'):
        return False


    def _apply(task: dict) -> None:
        from lib.project_mod.receipts import bound_file_changes
        existing = task.get('modifiedFileList')
        if not isinstance(existing, list):
            existing = task.get('_checkpointModifiedFileList') or []
        merged, truncated = bound_file_changes(merge_live_file_entry(existing, entry))
        task['modifiedFileList'] = merged
        task['modifiedFiles'] = len(merged)
        if truncated:
            task['modifiedFilesTruncated'] = True

    try:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        stamped = chat_task_runtime.update_matching(
            predicate=lambda task: (
                task.get('id') == task_id
                and task.get('status') in ('pending', 'running')
                and not task.get('aborted')
            ),
            updater=_apply,
        )
        if not stamped:
            # Probe only after a miss, outside update_matching's registry lock.
            # This is a later observation, not proof of state at stamp time.
            try:
                probe = chat_task_runtime.get(task_id)
            except Exception as diagnostic_error:
                logger.debug('note_live_file_change failure handled: %s', type(diagnostic_error).__name__)
                probe = None
            status = (probe or {}).get('status')
            aborted = bool((probe or {}).get('aborted'))
            from lib.task_replay.task_replay import TASK_REPLAY_TERMINAL_STATUSES
            expected_late = aborted or status in TASK_REPLAY_TERMINAL_STATUSES
            # Status is diagnostic metadata, never arbitrary task content.
            if status not in ('pending', 'running') + TASK_REPLAY_TERMINAL_STATUSES:
                status = 'unknown'
            log = logger.debug if expected_late else logger.warning
            log('[Derive] live file-change stamp matched no running task '
                '(task=%s registered=%s status=%s aborted=%s)',
                str(task_id)[:8], probe is not None, status, aborted)
        return bool(stamped)
    except Exception as e:  # presentation only — the journal stays authoritative
        logger.warning('[Derive] live file-change stamp skipped '
                       '(task=%s error_type=%s)',
                       str(task_id)[:8], type(e).__name__[:64])
        return False


def note_remote_file_changes(task_id, owner_user_id, remote, entries):
    """Project validated device commits into the addressed running task only."""
    return _note_external_file_changes(task_id, owner_user_id, entries, {
        'source': 'local', 'root': remote['root'],
        'rootPath': f'remote:{remote["agent_id"]}:{remote["root"]}'})


def note_overleaf_file_changes(task_id, owner_user_id, server, project_id, entries):
    """Project request-bound Overleaf commits without borrowing host paths."""
    return _note_external_file_changes(task_id, owner_user_id, entries, {
        'source': 'overleaf', 'mcpServer': server, 'projectId': project_id})


def _note_external_file_changes(task_id, owner_user_id, entries, binding):
    if not task_id or not owner_user_id or not entries:
        return False
    from lib.project_mod.receipts import bound_file_changes
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    def apply(task):
        existing = task.get('modifiedFileList') or task.get('_checkpointModifiedFileList') or []
        merged, truncated = bound_file_changes(existing)
        for entry in entries:
            merged = merge_live_file_entry(merged, {**entry, **binding})
            merged, pressure = bound_file_changes(merged)
            truncated = truncated or pressure
        task['modifiedFileList'], task['modifiedFiles'] = merged, len(merged)
        if truncated:
            task['modifiedFilesTruncated'] = True
    try:
        return bool(chat_task_runtime.update_matching(predicate=lambda task: (
            task.get('id') == task_id and str(task.get('_userId') or '') == str(owner_user_id)
            and task.get('status') in ('pending', 'running') and not task.get('aborted')
            and not (task.get('abort_event') and task['abort_event'].is_set())), updater=apply))
    except Exception:
        logger.warning('[Derive] remote file-change projection failed for task %s', str(task_id)[:8])
        return False
