"""orchestrator/_vu_startup.py — VU startup + external-edit probe (run_task slice).

**Extraction context** (board epic ```, slice 2, FIRST source
extraction from the 1813-line ``run_task``):

Two self-contained helpers that ``run_task`` previously carried as nested
closures inside its main ``try:`` block. Both were pure-ish (no dependency
on any local variable OTHER than what they explicitly closed over) so they
translate 1:1 to module-level functions taking their captures as arguments.

  * :func:`_vu_phase` — emit a PHASE event during the VU sub-task's
    startup window (silent no-op on ordinary non-VU turns).
  * :func:`_probe_external_edits` — the daemon-thread target that runs
    the FUSE external-edit probe after ``ensure_project_state``; on
    detection of committed off-Tofu edits, snapshots them into
    file-history and appends a ``PROJECT_EXTERNAL_EDIT`` event. The
    event is a stream audit record ONLY — the frontend deliberately
    ignores it (the drift toast advertised an undo the UI had no path
    for; the snapshot's real consumer is the file-history timeline).

Kept OUT of ``_finalize`` (post-loop helpers) and ``_turn`` (per-round
primitives) because they run ONCE at task startup and are neither
finalization nor per-turn.

**Strangler-fig discipline** (matches routes/chat_helpers.py's pattern):
these functions are DEFINED here; ``_run.py`` imports them and calls them
at the same source sites where the closures previously lived. There is now
exactly ONE definition of each. Wire-parity guarded by
``tests/test_lib_orchestrator_wire_parity.py``.
"""

from __future__ import annotations

import threading
from typing import Any

from lib.log import get_logger
from lib.agent_core.events import EventType, Phase, build_event, build_phase
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)


def _vu_phase(
    task: dict[str, Any],
    detail: str,
    *,
    vu_startup: bool,
    detail_key: str | None = None,
    detail_args: dict | None = None,
) -> None:
    """Emit a PHASE event during the VU sub-task's startup window.

    The VU sub-task carries ``_vu_event_transform`` (the event-transform
    seam), so any PHASE emitted here is wrapped as
    ``autopilot_vu_event`` and lands in the synthetic-user bubble on BOTH
    the carrier's own stream and the parent's. The
    pre-stream prep window (tool assembly → tool-history rebuild → system-
    context injection → FUSE memory/project prefetch) is otherwise SILENT
    for up to tens of seconds on a large conversation (measured 2.9–4.7s
    typical, ~26s on a 3000-event conv), leaving the bubble on a vague
    placeholder. Naming each real sub-step keeps the display honest.

    Gated on ``vu_startup`` so the ordinary non-VU startup path
    stays byte-identical (no new events).

    Args:
        task: The live task dict — passed to ``append_event`` as-is.
        detail: Short user-visible description of the current sub-step.
        vu_startup: True IFF this is a VU sub-task's startup window
            (``bool(task.get('_vu_subtask'))`` at the call site).
    """
    if not vu_startup:
        return
    try:
        fields: dict[str, Any] = {'detail': detail}
        if detail_key:
            fields['detailKey'] = detail_key
        if detail_args:
            fields['detailArgs'] = detail_args
        append_event(task, build_phase(Phase.WORKING, **fields))
    except Exception as _e:
        _tid = (task.get('id') or '')[:8]
        logger.debug('[Task %s] vu startup phase emit failed: %s', _tid, _e)


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
    _conv_id_for_roots = task.get('convId') or task.get('id') or ''
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


def make_vu_phase(task: dict[str, Any]):
    """Bind the VU-startup flag + task into the closure-style phase emitter.

    Extracted 2026-08-01 ( slice 37) from ``run_task``'s inline
    attribution + closure block. The captured ``task`` + ``_vu_startup``
    are stable across the whole invocation (no rebind), so binding them
    once at factory time is semantically identical to the inline
    closure — and the loop's closure-style call sites (``_vu_phase(x)``)
    are preserved unchanged. The returned closure is internally named
    ``_bound`` so the module-level ``_vu_phase`` stays resolvable through
    the module namespace at call time (an inner ``def _vu_phase`` would
    shadow it across the whole factory scope — def is an assignment).
    """
    _vu_startup = bool(task.get('_vu_subtask'))

    def _bound(detail, *, detail_key=None, detail_args=None):
        _vu_phase(
            task,
            detail,
            vu_startup=_vu_startup,
            detail_key=detail_key,
            detail_args=detail_args,
        )

    return _bound


__all__ = [
    '_vu_phase', '_probe_external_edits',
    'start_external_edit_probe', 'setup_project_context', 'make_vu_phase',
]
