"""Per-round modified-file derivation from the modifications journal.

``derive_round_modified_files`` builds a round's authoritative file-change
list from the per-root modifications journal (conversation-isolated via
taskId stamping).  Called by ``_finalize_and_emit_done``.

``note_live_file_change`` is the mid-run complement: the journal writer
(``lib.project_mod.modifications``) notifies it on every task-stamped record
so the live task's ``modifiedFileList`` grows DURING the turn and the
``fileChanges`` projection block renders before settlement.  Settlement
still rebuilds the authoritative list from the journal and overwrites the
presentation value.

Dependency is one-directional: this module reads ``lib.project_mod`` and
(lazily, inside the live stamp) the task registry — never the orchestration
loop.
"""

from __future__ import annotations

import os

from lib.log import get_logger

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
    # The count is the UNIQUE-FILE count, never the raw journal-event count:
    # the "files changed" card headlines this number above the deduped list,
    # and a file edited five times must read as one changed file, not five.
    return file_list, len(file_list), used_ts_fallback


def merge_live_file_entry(files: list[dict], entry: dict) -> list[dict]:
    """Fold ``entry`` into ``files``, last-write-wins by ``(root, path)``.

    Copy-on-write: returns a fresh list so a projection fold iterating the
    task's previous list mid-stamp never sees a torn sequence.  An
    already-listed file keeps its first-seen position (the settlement
    ordering) with the newest action; a new file appends.
    """
    key = (entry.get('root', '') or '', entry.get('path', '') or '')
    merged: list[dict] = []
    replaced = False
    for item in files:
        if not isinstance(item, dict):
            continue
        if (item.get('root', '') or '', item.get('path', '') or '') == key:
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
        existing = task.get('modifiedFileList')
        if not isinstance(existing, list):
            existing = task.get('_checkpointModifiedFileList') or []
        merged = merge_live_file_entry(existing, entry)
        task['modifiedFileList'] = merged
        task['modifiedFiles'] = len(merged)

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
        return bool(stamped)
    except Exception as e:  # presentation only — the journal stays authoritative
        logger.debug('[Derive] live file-change stamp skipped for %s: %s', task_id, e)
        return False
