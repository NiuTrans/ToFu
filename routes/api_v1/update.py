"""routes/api_v1/update.py — Self-update surface for the topbar button.

Routes (mounted under ``/api/v1``):

  GET  /api/v1/update/check    — compare installed VERSION vs. the newest
                                 GitHub release tag; report git availability
                                 and whether the working tree is safe to pull.
  POST /api/v1/update/apply    — admin: apply the update. A git checkout
                                 uses ``git pull --ff-only`` (refuses on a
                                 dirty tree); a non-git deployment (exported
                                 copy / zip) downloads the release tarball
                                 and overlays tracked source instead.
  POST /api/v1/update/restart  — admin: re-exec the server process so pulled
                                 ``.py`` changes take effect. Explicit only —
                                 ``apply`` never auto-restarts.

The heavy lifting lives in :mod:`lib.self_update`; this layer is a thin,
fully-logged HTTP wrapper.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

from flask import Blueprint

from lib.api_response import api_conflict, api_internal_error, api_ok
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.push import push_event
from lib.request_parser import parse_body

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_update_bp = Blueprint('api_v1_update', __name__)

# Push channel for live self-update progress (mirrors the 'translate' /
# 'paper' pattern). Frontend subscribes via pushSubscribe('update', taskId).
UPDATE_CHANNEL = 'update'


@api_v1_update_bp.route('/api/v1/update/check', methods=['GET'])
@require_auth
@api_meta(
    summary='Check for an available update',
    description=(
        'Compares the installed version against the newest release tag on '
        'the official GitHub repository. Also reports whether this is a git '
        'checkout and whether the working tree is safe to fast-forward '
        '(runtime-state churn under .tofu/ is tolerated; tracked-source '
        'edits block the update). Read-only.'
    ),
    tags=['system'],
)
def update_check():
    from lib.self_update import check_for_update
    try:
        payload = check_for_update()
    except Exception as e:
        logger.error('[Update] check failed: %s', e, exc_info=True)
        return api_internal_error(e, context='update_check',
                                  source='api_v1.update.check')
    return api_ok(payload)


@api_v1_update_bp.route('/api/v1/update/apply', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Apply the available update',
    description=(
        'Applies the update, choosing the strategy automatically. A git '
        'checkout runs git fetch + git pull --ff-only (refuses, without '
        'mutating anything, on a dirty tracked-source tree; never '
        'auto-stashes or force-resets). A non-git deployment downloads the '
        'official release tarball and overlays tracked source onto the '
        'project root, backing up replaced files to .update_backup/. Either '
        'way user settings/data/memories live outside tracked code and are '
        'never touched. If requirements.txt changed, runs pip install '
        'against the running interpreter so the update is self-contained. '
        'Returns needs_restart=true when files changed; the caller must '
        'POST /api/v1/update/restart.'
    ),
    tags=['system'],
)
def update_apply():
    """Launch the update in a background thread; stream progress via push.

    The pull + ``pip install`` can take minutes — far longer than a sane
    HTTP timeout. Rather than block the request (which makes the modal look
    frozen and risks a client-side abort killing a legitimate install), we
    spawn a daemon worker that emits per-stage events on the ``update`` push
    channel and a terminal ``done`` frame carrying the full result dict.
    The route returns a ``taskId`` immediately; the frontend subscribes to
    ``pushSubscribe('update', taskId)`` and renders a live stepper.
    """
    task_id = uuid.uuid4().hex

    def _progress(stage: str, status: str, detail: str = '', meta=None):
        frame = {
            'type': 'stage', 'stage': stage, 'status': status,
            'detail': (detail or '')[:300],
        }
        # Structured download / transfer telemetry (percent, bytes, speed)
        # so the frontend can render a determinate bar + speed readout
        # instead of an opaque spinner. Only present on the fetch/deps
        # stages that report it; the schema tolerates it being absent.
        if isinstance(meta, dict):
            for k in ('pct', 'loaded', 'total', 'speed', 'phase'):
                if meta.get(k) is not None:
                    frame[k] = meta[k]
        push_event(UPDATE_CHANNEL, task_id, frame)

    def _worker():
        from lib.self_update import apply_update
        try:
            result = apply_update(progress=_progress)
        except Exception as e:
            logger.error('[Update] apply failed: %s', e, exc_info=True)
            push_event(UPDATE_CHANNEL, task_id, {
                'type': 'done', 'ok': False,
                'error': 'Update failed unexpectedly. Check the server log.',
                'detail': str(e)[:300],
            })
            return
        push_event(UPDATE_CHANNEL, task_id, {'type': 'done', **result})

    threading.Thread(target=_worker, name=f'tofu-update-{task_id[:8]}',
                     daemon=True).start()
    logger.info('[Update] apply started in background (task=%s)', task_id[:8])
    return api_ok({'taskId': task_id, 'started': True})


def _close_inheritable_listen_sockets():
    """Mark inherited-across-exec FDs (Hypercorn's listen socket) close-on-exec.

    Hypercorn's ``Config._create_sockets`` calls ``sock.set_inheritable(True)``
    on its listening socket (hypercorn/config.py). Since PEP 446 (Python 3.4)
    every other FD is created non-inheritable by default, so the ONLY
    inheritable FDs above the std streams are exactly Hypercorn's bound
    listeners. If we don't clear that flag, ``os.execv`` leaks the still-bound
    listen socket into the fresh server image: the old port stays occupied by
    our own inherited FD, ``_wait_port_free`` in server.py times out, and the
    restart silently shifts to port+1 (15002 → 15003) on every restart.

    Resetting the inheritable flag makes execv close these FDs, freeing the
    port so the new image reclaims it.
    """
    # Only inspect actually-open FDs. /proc/self/fd (Linux) avoids scanning a
    # potentially huge SC_OPEN_MAX range; fall back to a bounded range elsewhere.
    try:
        fds = [int(name) for name in os.listdir('/proc/self/fd') if name.isdigit()]
    except OSError as e:
        logger.debug('[Update] /proc/self/fd unavailable (%s) — scanning bounded FD range', e)
        _max = os.sysconf('SC_OPEN_MAX') if hasattr(os, 'sysconf') else 4096
        fds = range(3, min(_max, 65536))
    closed = 0
    for fd in fds:
        if fd < 3:
            continue
        try:
            if os.get_inheritable(fd):
                os.set_inheritable(fd, False)
                closed += 1
        except OSError as e:
            logger.debug('[Update] could not clear inheritable flag on fd %d: %s', fd, e)
            continue
    if closed:
        logger.info('[Update] Cleared inheritable flag on %d FD(s) before re-exec '
                    '(prevents leaked listen socket holding the port)', closed)


def _deferred_reexec(delay: float = 0.6):
    """Re-exec the current process after a short delay.

    Runs in a daemon thread so the HTTP response flushes first. Mirrors
    server.py's launch contract: same interpreter, same argv. The instance
    lock fd is close-on-exec, so it releases before the new image re-acquires.
    """
    time.sleep(delay)
    logger.info('[Update] Re-execing server: %s %s',
                sys.executable, ' '.join(sys.argv))
    # Flip the clean-shutdown dirty-bit: an in-place re-exec is a controlled
    # exit, so the fresh image must NOT flag the previous PID as an OS kill.
    try:
        from lib.shutdown_marker import mark_clean
        mark_clean('restart')
    except Exception as _sm_e:
        logger.warning('[Update] mark_clean(restart) failed: %s', _sm_e)
    try:
        # Let the env-reexec guard run again from a clean slate.
        os.environ.pop('_TOFU_ENV_REEXEC', None)
        # Drop Hypercorn's inheritable listen socket so execv frees the port;
        # otherwise the new image inherits our bound socket and shifts to
        # port+1. MUST run before execv. See _close_inheritable_listen_sockets.
        _close_inheritable_listen_sockets()
        # Tell the fresh image which port we were serving so it reclaims it
        # (waits for our lingering listener to drain) instead of letting the
        # connect-probe shift the port (15000 → 15001). See server.py.
        _runtime_port = (os.environ.get('_TOFU_RUNTIME_PORT', '') or '').strip()
        if _runtime_port:
            os.environ['_TOFU_REEXEC_PORT'] = _runtime_port
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except OSError as e:
        # execv only returns on failure — log loudly; the process keeps running.
        logger.critical('[Update] Re-exec failed, server NOT restarted: %s',
                        e, exc_info=True)


@api_v1_update_bp.route('/api/v1/update/restart', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Restart the server',
    description=(
        'Re-execs the server process so freshly-pulled code takes effect. '
        'Explicit and admin-only — there is no silent auto-restart. The '
        'response is sent before the process restarts; clients should wait '
        'a few seconds and reconnect. Refuses with 409 when OTHER '
        'conversations have in-flight tasks (a re-exec kills every running '
        'task); pass {"force": true} to override.'
    ),
    tags=['system'],
)
def update_restart():
    # A restart is an unconditional os.execv of the whole server, so EVERY
    # in-flight task dies with it. Refuse by default when sibling conversations
    # are mid-run — otherwise an agent's own run_command probing this endpoint
    # silently interrupts all its long-running siblings. The caller's own
    # conversation (if any) is excluded so it can restart itself.
    body = parse_body()
    force = bool(body.get('force'))
    own_conv = (body.get('convId') or body.get('conv_id') or '').strip() or None

    running = []
    try:
        from lib.tasks_pkg.manager import list_running_tasks
        running = list_running_tasks(exclude_conv_id=own_conv)
    except Exception as e:
        logger.warning('[Update] Could not check running tasks before restart: %s', e)

    if running and not force:
        logger.warning(
            '[Update] Restart REFUSED — %d running task(s) would be killed: %s '
            '(pass force=true to override)',
            len(running), [r['taskId'][:8] for r in running])
        audit_log('self_update_restart_refused', pid=os.getpid(),
                  running_tasks=len(running))
        return api_conflict(
            'Restart refused: %d other conversation(s) have running tasks that '
            'a restart would interrupt. Retry when idle, or pass force=true.'
            % len(running),
            runningTasks=running, needsForce=True)

    audit_log('self_update_restart', pid=os.getpid(),
              forced=force, running_tasks=len(running))
    logger.warning('[Update] Restart requested — re-exec scheduled (pid=%d, '
                   'force=%s, running_tasks=%d)',
                   os.getpid(), force, len(running))
    threading.Thread(target=_deferred_reexec, name='tofu-restart',
                     daemon=True).start()
    return api_ok({'restarting': True, 'forced': force,
                   'interruptedTasks': len(running)})


def _deferred_shutdown(delay: float = 0.6):
    """Gracefully stop the server after a short delay (response flushes first).

    Marks the clean-shutdown dirty-bit ``manual`` FIRST, then raises SIGTERM on
    ourselves so the existing handler (server.py) drains in-flight requests and
    exits cleanly. Because the marker is already ``clean``, the NEXT boot
    classifies this exit as a deliberate manual stop — NOT an OS kill — so
    recovery leaves those turns tagged ``manual`` and does not auto-recover
    them. Runs in a daemon thread.
    """
    import signal as _signal
    time.sleep(delay)
    try:
        from lib.shutdown_marker import mark_clean
        mark_clean('manual')
    except Exception as e:
        logger.warning('[Shutdown] mark_clean(manual) failed: %s', e)
    logger.warning('[Shutdown] Manual shutdown requested — raising SIGTERM (pid=%d)',
                   os.getpid())
    try:
        os.kill(os.getpid(), _signal.SIGTERM)
    except OSError as e:
        logger.critical('[Shutdown] SIGTERM to self failed: %s', e, exc_info=True)


@api_v1_update_bp.route('/api/v1/update/shutdown', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Shut the server down (manual, graceful)',
    description=(
        'Marks the clean-shutdown dirty-bit as a MANUAL stop, then gracefully '
        'stops the server (drains in-flight requests via SIGTERM). This is the '
        'operator marker for a deliberate shutdown: the next boot classifies '
        'the exit as intentional rather than an OS SIGKILL/OOM, so '
        'crash-recovery does NOT auto-recover the interrupted turns. Unlike '
        'restart there is no re-exec — the process exits and does not come '
        'back on its own.'
    ),
    tags=['system'],
)
def update_shutdown():
    audit_log('manual_shutdown', pid=os.getpid())
    logger.warning('[Shutdown] Manual shutdown requested (pid=%d)', os.getpid())
    threading.Thread(target=_deferred_shutdown, name='tofu-shutdown',
                     daemon=True).start()
    return api_ok({'shuttingDown': True})


__all__ = ['api_v1_update_bp']
