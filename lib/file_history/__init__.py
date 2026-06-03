"""File-history: per-file copy-backup store for round-by-round undo.

This is the replacement for the legacy ``lib.project_mod.git_shim`` shadow-git
machinery.  Instead of synthesising a parallel git repository, we keep
**copy backups** of every file the assistant has touched in the current
session, plus an append-only ``snapshots.jsonl`` log of round boundaries.

Design summary
--------------
* Bounded work — every operation is O(files this session has touched).
  No worktree-wide ``git status`` / ``git add -A`` walks.
* Pure filesystem — no subprocesses, no ``index.lock``, no FUSE timeouts.
* Disk layout (per-project, alongside the project — survives moves):

      <base_path>/.tofu/file-history/
          snapshots.jsonl          — append-only round log
          backups/
              <sha256(rel)[:2]>/
                  <sha256(rel)>@v1 — first observed contents
                  <sha256(rel)>@v2 — next version
                  …

* Versions are integers, monotonically increasing per file.  Identical
  content is deduped (a fresh edit that produces unchanged bytes does
  NOT bump the version).  When the per-file version count exceeds
  ``MAX_VERSIONS_PER_FILE`` we drop the oldest backups except the
  earliest one (so "rewind to start of session" stays possible).
* A ``FileHistorySnapshot`` records the per-file version pinned at the
  end of one round.  ``rewind_to(snapshot_id)`` walks the snapshot's
  file list and restores each file to its pinned version (or deletes
  if the snapshot recorded the file as absent).

Modeled on Claude Code's ``src/utils/fileHistory.ts``.

Public API (mirrors the surface ``git_shim`` exposed):

    is_enabled()
    track_edit(base_path, rel_path, *, message_id=None)        — pre-write hook
    make_snapshot(base_path, *, task_id, conv_id, message_id,
                  tool_names=None) -> snapshot_id | None       — round end
    list_history(base_path, *, path=None, limit=20) -> list
    diff_name_status(base_path, from_id, to_id) -> list[dict]
    rewind_to(base_path, snapshot_id) -> dict                  — undo
    restore_from(base_path, snapshot_id) -> dict               — redo
    detect_external_edits(base_path, *, message_id=None) -> dict
    get_last_snapshot_id(base_path) -> str | None

Env knobs:

* ``TOFU_FILE_HISTORY``      — ``1`` (default) / ``0`` to disable.
* ``TOFU_FILE_HISTORY_PROBE`` — ``1`` (default) / ``0`` to disable the
  per-round external-edit probe (mtime-based — cheap, but skippable).
"""
from __future__ import annotations

from lib.file_history.api import (
    detect_external_edits,
    diff_name_status,
    get_last_snapshot_id,
    is_enabled,
    list_history,
    make_snapshot,
    probe_enabled,
    restore_from,
    rewind_to,
    track_edit,
)

__all__ = [
    'is_enabled',
    'probe_enabled',
    'track_edit',
    'make_snapshot',
    'list_history',
    'diff_name_status',
    'rewind_to',
    'restore_from',
    'detect_external_edits',
    'get_last_snapshot_id',
]
