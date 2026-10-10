"""Task startup: canonical phase events, project setup and external-edit probe.

Entry points: emit_startup_phase, setup_project_context. The root invocation
owns ordering and cancellation; this module uses canonical event and project
services and never creates a second task lifecycle.
"""

from __future__ import annotations

import threading
from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, Phase, build_event, emit_phase
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


_STARTUP_PHASES = {
    'config': (
        'Resolving model and workspace settings…',
        'stream.phase.startupConfig',
    ),
    'tools': (
        'Preparing tools and workspace…',
        'stream.phase.startupTools',
    ),
    'context': (
        'Loading project context and relevant memory…',
        'stream.phase.startupContext',
    ),
}


def emit_startup_phase(task: dict[str, Any], stage: str) -> None:
    detail, detail_key = _STARTUP_PHASES[stage]
    try:
        emit_phase(
            task,
            Phase.WORKING,
            detail=detail,
            detailKey=detail_key,
        )
    except Exception as exc:
        logger.debug('[Task %s] startup phase emit failed stage=%s: %s',
                     (task.get('id') or '')[:8], stage, exc)


def _probe_external_edits(task: dict[str, Any], project_path: str) -> None:
    """Daemon-thread target that runs the FUSE external-edit probe.

    Runs SILENTLY: no phase event, no UI status — the LLM response starts
    streaming immediately. Cost is bounded by the size of the tracked-
    files set (files the assistant has touched this session), not the
    worktree, so this is cheap even on slow filesystems.

    Correctness guard: if the round has already started mutating files by
    the time the probe finishes, we skip the synthetic external-edit
    snapshot to avoid misattribution. The next round's probe catches the
    drift cleanly on top of a stable timeline.

    Args:
        task: The live task dict — read for ``modifiedFileList`` /
            ``modifiedFiles`` / ``id``, and mutated via ``append_event``
            on detection.
        project_path: The active project root the probe should scan.
    """
    try:
        if task.get('aborted') or task.get('status') in {
                'done', 'error', 'aborted', 'interrupted'}:
            logger.debug('[Task:%s] skipping external-edit probe — task is '
                         'cancelled or terminal', task['id'][:8])
            return
        if task.get('modifiedFileList') or task.get('modifiedFiles'):
            logger.debug('[Task:%s] skipping external-edit probe '
                         '— round already mutated files',
                         task['id'][:8])
            return
        # Pass the set of known Tofu task ids so the probe can tell a
        # CONCURRENT conversation's write on the shared project root
        # (last_writer_task_id ∈ known) from a genuine out-of-band IDE
        # edit — the former must NOT surface as an "edited outside
        # Tofu" toast.
        try:
            from lib.tasks_pkg.manager.runtime import chat_task_runtime
            _known_task_ids = set(chat_task_runtime.task_ids())
        except Exception as _kte:
            logger.debug('[Task:%s] known-task-id snapshot failed: %s',
                         task['id'][:8], _kte)
            _known_task_ids = None
        from lib import file_history as fh
        _ext = fh.detect_external_edits(
            project_path, known_task_ids=_known_task_ids)
        if task.get('aborted') or task.get('status') in {
                'done', 'error', 'aborted', 'interrupted'}:
            logger.debug('[Task:%s] external-edit probe completed after task '
                         'cancel/settlement — suppressing stale event',
                         task['id'][:8])
            return
        if _ext.get('siblingFiles'):
            logger.info('[Task:%s] external-edit probe attributed %d '
                        'drifted file(s) to concurrent Tofu task(s) — '
                        'suppressed IDE toast', task['id'][:8],
                        len(_ext.get('siblingFiles', [])))
        if (task.get('modifiedFileList') or task.get('modifiedFiles')):
            logger.debug('[Task:%s] external-edit probe completed after '
                         'round started mutating files — not emitting '
                         'SSE event (attribution ambiguous)',
                         task['id'][:8])
            return
        if _ext.get('committed'):
            append_event(task, build_event(
                EventType.PROJECT_EXTERNAL_EDIT,
                files=_ext.get('files', []),
                sha=_ext.get('snapshotId'),
            ))
            logger.info('[Task:%s] captured %d external edit(s) snap=%s',
                        task['id'][:8], len(_ext.get('files', [])),
                        (_ext.get('snapshotId') or '')[:8])
    except Exception as e:
        logger.warning('[Task:%s] external-edit detection failed: %s',
                       task['id'][:8], e)


def start_external_edit_probe(task: dict[str, Any], project_path: str) -> None:
    """Spawn ``_probe_external_edits`` in a daemon thread.

    Sole responsibility: wrap the ``threading.Thread`` boilerplate so the
    call site in ``_run.py`` stays a single line. The probe itself is
    the pure-ish target function above — spawning it separately keeps it
    directly testable without stubbing out threading.
    """
    threading.Thread(
        target=_probe_external_edits,
        args=(task, project_path),
        name=f'ext-edit-probe-{task["id"][:8]}',
        daemon=True,
    ).start()


def setup_project_context(
    task: dict[str, Any],
    cfg: dict[str, Any],
    project_path: str,
    project_enabled: bool,
) -> None:
    """One-shot project-scope startup: server root, presence, edit-probe.

    Runs once per task, at the start of ``run_task``, when the task
    targets a project. Aggregates three side effects that all share the
    same ``project_enabled AND project_path`` gate:

      1. ``ensure_project_state(project_path, ...)`` — reconciles the
         server's global project state with THIS task's roots (primary
         + extras from ``cfg['projectPaths'][1:]``) + read-only paths
         from ``cfg['readOnlyPaths']``. Prevents concurrent tasks from
         clobbering each other's workspace-root namespace.
      2. ``presence.announce(...)`` — best-effort registration of this
         conv as a live peer of the project root (feeds the
         "who is working here now" panel). A presence failure MUST NOT
         affect the task.
      3. ``start_external_edit_probe(task, project_path)`` — kicks the
         daemon-thread FUSE probe that captures any IDE-side edits made
         between rounds. Guarded by ``file_history.is_enabled() and
         file_history.probe_enabled()``.

    The gate lives INSIDE this function so callers can invoke it
    unconditionally — an ordinary chat task with no project attached
    simply returns immediately.

    Args:
        task: the live task dict — read for ``id`` / ``convId``, and
            passed to presence + probe.
        cfg: the resolved task config — read for ``projectPaths`` /
            ``readOnlyPaths`` / ``autopilotRunId`` / ``convTitle`` /
            ``autopilotObjective``.
        project_path: primary project root (may be empty).
        project_enabled: the outer feature gate.
    """
    if not (project_enabled and project_path):
        return

    # Extract extra root paths from projectPaths (frontend sends all roots).
    #   projectPaths[0] = primary (same as projectPath), rest are extras.
    _all_paths = cfg.get('projectPaths') or []
    _extra_paths = (
        [p for p in _all_paths[1:] if p and p != project_path]
        if len(_all_paths) > 1 else [])
    # Read-only roots: a subset of the configured paths the user
    #   attached for reference only. Writes/edits and destructive
    #   run_command targeting these are refused; reads are
    #   always allowed. Empty list = today's all-writable behaviour.
    _readonly_paths = [p for p in (cfg.get('readOnlyPaths') or []) if p]
    logger.info('[Task:%s] project_path=%s extra_roots=%d readonly=%d',
                task['id'], project_path, len(_extra_paths),
                len(_readonly_paths))
    # Ensure the server's global project state matches this task's
    #   project path + extras. Another conversation may have switched
    #   the server to a different project, causing
    #   get_context_for_prompt to miss the file tree (path mismatch →
    #   no tree in system prompt → LLM doesn't know the project
    #   structure → "backend cannot use tools").
    from lib.project_mod import ensure_project_state
    # Pass conv_id for per-conversation root isolation (2026-05-05).
    #   Prevents concurrent tasks from clobbering each other's
    #   workspace-root namespace when they call set_project with
    #   different primary paths. See lib/project_mod/config.py
    #   ::set_conv_roots docstring for background.
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    _conv_id_for_roots = execution_root_key(task)
    ensure_project_state(project_path, extra_paths=_extra_paths,
                         conv_id=_conv_id_for_roots,
                         readonly_paths=_readonly_paths)

    # ── Presence: announce this conversation as a live peer of the
    #    project root. Best-effort; a presence failure must NEVER
    #    affect the task.
    if task.get('convId'):
        try:
            from lib.presence import announce as _presence_announce
            from lib.tasks_pkg.manager import task_user_id
            _presence_announce(
                project_path, task['convId'],
                user_id=int(task_user_id(task)),
                task_id=task['id'],
                run_id=cfg.get('autopilotRunId') or '',
                title=cfg.get('convTitle') or '',
                objective=cfg.get('autopilotObjective') or '',
                phase='working',
            )
        except Exception as _pe:
            logger.debug('[Task:%s] presence announce failed: %s',
                         task['id'][:8], _pe)

    # Presence keepalive: a live task must read ACTIVE for its whole
    #   lifetime, not only while the model streams text (a >25s tool call
    #   otherwise drops the conversation out of every collaboration view).
    #   One bounded daemon refreshes liveness from the task registry; a
    #   keepalive failure must NEVER affect the task (same as announce).
    if task.get('convId'):
        try:
            from lib.tasks_pkg.manager._presence_keepalive import (
                ensure_started as _presence_keepalive_start,
            )
            _presence_keepalive_start()
        except Exception as _pk:
            logger.debug('[Task:%s] presence keepalive start failed: %s',
                         task['id'][:8], _pk)

    # ── File-history: capture any external (IDE) edits made between
    #    rounds. Runs SILENTLY in a background thread — no phase event,
    #    no UI status; the LLM response starts streaming immediately.
    try:
        from lib import file_history as fh
        if fh.is_enabled() and fh.probe_enabled():
            start_external_edit_probe(task, project_path)
    except Exception as e:
        logger.warning('[Task:%s] could not start external-edit probe: %s',
                       task['id'][:8], e)


__all__ = [
    '_probe_external_edits',
    'setup_project_context',
    'start_external_edit_probe',
]