"""Auto-restart watcher: re-exec the server when HEAD moves while idle.

The "effective" contract on a shared checkout: an agent's work only COUNTS
once the running process serves it — i.e. after the change lands on the
checked-out branch AND the server restarts. The restart half used to be a
human chore, and a forgotten one (worktrees "done" but never live — the
original worktree post-mortem). This daemon automates it under a strict
precondition bundle:

  * env-gated: ``TOFU_AUTO_RESTART=1`` — default OFF, a self-driving
    restart is a deliberate deployment choice;
  * git checkout only (``lib.self_update._git._is_git_repo``);
  * HEAD sha differs from the boot-time baseline — any commit that lands
    while the server runs (ours or a sibling's);
  * shutdown not already requested;
  * no in-flight tasks (``list_running_tasks``) — a re-exec kills them
    all. This is the SAME guard the manual ``POST /api/v1/update/restart``
    endpoint enforces; active chat streams belong to running tasks, and a
    replaying client warm-reconnects (Last-Event-ID), so a tasks-empty
    server is safe to bounce.

A HEAD move that arrives while busy is NOT lost: the next poll re-checks,
so the restart fires as soon as the server drains. After triggering, the
thread exits — the re-exec replaces the process and the fresh image starts
its own watcher against the new baseline.

Deliberate non-goals (v1): dirty-tree detection (restarting on uncommitted
edits would fight the agent mid-flight; committed work is the unit), and
any force path (this watcher never kills in-flight work).

Env: ``TOFU_AUTO_RESTART=1`` master switch;
``TOFU_AUTO_RESTART_INTERVAL_SEC`` poll interval (default 60, floor 10).
"""

from __future__ import annotations

import os
import threading
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_DEFAULT_INTERVAL_SEC = 60.0
_MIN_INTERVAL_SEC = 10.0


def _auto_restart_enabled() -> bool:
    val = os.environ.get('TOFU_AUTO_RESTART', '').strip().lower()
    return val in ('1', 'true', 'yes', 'on')


def _interval_sec() -> float:
    raw = os.environ.get('TOFU_AUTO_RESTART_INTERVAL_SEC', '').strip()
    if not raw:
        return _DEFAULT_INTERVAL_SEC
    try:
        return max(_MIN_INTERVAL_SEC, float(raw))
    except ValueError:
        logger.warning('[AutoRestart] bad TOFU_AUTO_RESTART_INTERVAL_SEC=%r '
                       '— using %ss', raw, _DEFAULT_INTERVAL_SEC)
        return _DEFAULT_INTERVAL_SEC


def _restart_preconditions(shutdown_requested, running_tasks) -> tuple:
    """(ok, why_not) — every condition that must hold before the re-exec.

    Fail-closed on check ERRORS: a guard we cannot evaluate is a guard we
    treat as unmet (a spurious restart kills in-flight work; a skipped one
    just waits a poll).
    """
    try:
        if shutdown_requested is not None and shutdown_requested.is_set():
            return False, 'shutdown-requested'
        running = running_tasks()
        if running:
            return False, f'tasks-running:{len(running)}'
        return True, ''
    except Exception as e:
        logger.warning('[AutoRestart] precondition check failed (skipping): %s',
                       e, exc_info=True)
        return False, f'check-error:{type(e).__name__}'


def poll_once(state: dict, *, shutdown_requested=None, is_repo=None,
              head_sha=None, running_tasks=None, do_restart=None) -> str:
    """One watch tick against the mutable ``state`` dict.

    ``state`` keys: ``baseline_sha`` (None until first successful read),
    ``restarting`` (set once a re-exec has been triggered).

    Seam parameters default to the real implementations; tests inject
    fakes. Returns a verdict token:

      'restarting'        — a re-exec was already triggered (terminal)
      'no-repo'           — not a git checkout (feature inapplicable)
      'no-head'           — HEAD unreadable this tick (transient; retry)
      'baseline'          — captured the boot baseline this tick
      'unchanged'         — HEAD == baseline
      'not-ready:<why>'   — HEAD moved but a precondition is unmet
      'restart-triggered' — HEAD moved, all preconditions green, re-exec fired
      'error'             — poll itself failed (logged; retry next tick)
    """
    if state.get('restarting'):
        return 'restarting'
    if is_repo is None:
        from lib.self_update._git import _is_git_repo as is_repo
    if head_sha is None:
        from lib.self_update._git import _head_sha as head_sha
    if running_tasks is None:
        def running_tasks():
            from lib.tasks_pkg.manager import list_running_tasks
            return list_running_tasks()
    if do_restart is None:
        def do_restart(reason):
            from routes.api_v1.update import _perform_server_reexec
            return _perform_server_reexec(reason)
    try:
        if not is_repo():
            return 'no-repo'
        head = head_sha()
    except Exception as e:
        logger.warning('[AutoRestart] poll failed: %s', e, exc_info=True)
        return 'error'
    if not head:
        return 'no-head'
    baseline = state.get('baseline_sha')
    if not baseline:
        state['baseline_sha'] = head
        return 'baseline'
    if head == baseline:
        return 'unchanged'
    ok, why = _restart_preconditions(shutdown_requested, running_tasks)
    if not ok:
        return f'not-ready:{why}'
    logger.warning('[AutoRestart] HEAD moved %s → %s and server is idle — '
                   're-execing to make the change effective',
                   str(baseline)[:8], str(head)[:8])
    audit_log('auto_restart_trigger', old_head=str(baseline)[:12],
              new_head=str(head)[:12])
    state['restarting'] = True
    try:
        if do_restart('auto_restart_head_changed') is False:
            # execv failed (already logged CRITICAL) — keep watching.
            state['restarting'] = False
            return 'error'
    except Exception as e:
        logger.error('[AutoRestart] restart call failed: %s', e, exc_info=True)
        state['restarting'] = False
        return 'error'
    return 'restart-triggered'


def _watch_loop(shutdown_requested=None):
    state: dict = {'baseline_sha': None, 'restarting': False}
    interval = _interval_sec()
    logger.info('[AutoRestart] watcher started (interval=%ss)', interval)
    last_note = None
    while True:
        verdict = poll_once(state, shutdown_requested=shutdown_requested)
        if verdict == 'baseline':
            logger.info('[AutoRestart] baseline HEAD=%s',
                        (state.get('baseline_sha') or '')[:8])
        elif verdict == 'restart-triggered':
            return  # process is being replaced; the fresh image re-arms
        elif verdict.startswith('not-ready'):
            if verdict != last_note:
                logger.info('[AutoRestart] HEAD moved; restart deferred: %s',
                            verdict)
            last_note = verdict
        time.sleep(interval)


def maybe_start_auto_restart_watch(*, shutdown_requested=None) -> bool:
    """Start the daemon watcher when ``TOFU_AUTO_RESTART=1``.

    Returns True when the watcher was armed. Called once from server.py's
    serving-loop startup, alongside the loop-stall watchdog.
    """
    if not _auto_restart_enabled():
        logger.debug('[AutoRestart] disabled (set TOFU_AUTO_RESTART=1 to enable)')
        return False
    t = threading.Thread(target=_watch_loop,
                         kwargs={'shutdown_requested': shutdown_requested},
                         name='tofu-auto-restart', daemon=True)
    t.start()
    return True


__all__ = ['maybe_start_auto_restart_watch', 'poll_once']
