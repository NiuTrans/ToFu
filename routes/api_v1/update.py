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

import json
import os
import sys
import threading
import time
import uuid

from flask import Blueprint, request

from lib import lifecycle_approval as _lca
from lib.runtime_paths import data_root
from lib.api_response import (
    api_conflict, api_error, api_forbidden, api_internal_error, api_not_found,
    api_ok,
)
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

# ── Apply-state persistence (survives page reloads AND process restarts) ──
# A download can take 5-15 minutes; the user will close or reload the page.
# Push frames are transient and in-memory frontend state dies with the page,
# so the terminal result is ALSO persisted here: /update/check projects
# ``pending_restart`` (code landed, process still runs the old version) and
# ``apply_in_progress`` (a live download, re-attachable via its task_id).
_APPLY_STATE_NAME = 'update_apply_state.json'
_ACTIVE_APPLIES: dict = {}  # task_id → Thread; in-process liveness truth


def _apply_state_path() -> str:
    return os.path.join(data_root(), _APPLY_STATE_NAME)


def _write_apply_state(state: dict) -> None:
    try:
        from lib.json_store import write_json_atomic
        write_json_atomic(_apply_state_path(), state)
    except Exception as e:
        logger.warning('[Update] apply-state write failed: %s', e)


def _read_apply_state():
    try:
        from lib.json_store import read_json
        st = read_json(_apply_state_path(), default=None)
        return st if isinstance(st, dict) else None
    except Exception as e:
        logger.debug('[Update] apply-state read failed: %s', e)
        return None


def _enrich_with_apply_state(payload):
    """Project the persisted apply state onto the /update/check payload.

    * ``pending_restart`` — a finished apply landed code for
      ``new_version`` while the running process still serves an older one
      (clears itself the moment the restarted process reports the new
      version, so no explicit ack endpoint is needed).
    * ``apply_in_progress`` — a download whose worker thread is verifiably
      alive in THIS process; the frontend can re-attach its push
      subscription after a page reload. A 'running' marker whose thread is
      gone (the owning process died mid-apply) is rewritten to
      ``interrupted`` once so it stops resurfacing.
    """
    if not isinstance(payload, dict):
        return payload
    st = _read_apply_state()
    if not st:
        return payload
    status = st.get('status')
    if status == 'running':
        tid = st.get('task_id') or ''
        th = _ACTIVE_APPLIES.get(tid)
        if th is not None and th.is_alive():
            payload['apply_in_progress'] = {
                'task_id': tid,
                'started_at': st.get('started_at'),
                'old_version': st.get('old_version'),
            }
        else:
            _write_apply_state({**st, 'status': 'interrupted',
                                'finished_at': time.time()})
        return payload
    if status == 'done' and st.get('needs_restart'):
        from lib.self_update._version import current_version
        new_ver = st.get('new_version') or ''
        if new_ver and new_ver != current_version():
            payload['pending_restart'] = {
                'new_version': new_ver,
                'old_version': st.get('old_version'),
                'method': st.get('method'),
                'finished_at': st.get('finished_at'),
                'changed': True,
                'deps_changed': bool(st.get('deps_changed')),
                'deps_installed': bool(st.get('deps_installed')),
                'error': st.get('error') or '',
                'detail': st.get('detail') or '',
            }
    return payload


@api_v1_update_bp.route('/api/v1/update/check', methods=['GET'])
@require_auth
@api_meta(
    summary='Check for an available update',
    description=(
        'Compares the installed version against the newest release tag on '
        'the official GitHub repository. Also reports whether this is a git '
        'checkout and whether the working tree is safe to fast-forward '
        '(runtime-state churn under .tofu/ is tolerated; tracked-source '
        'edits block the update). Read-only. The payload also projects the '
        'persisted apply state: ``pending_restart`` when a finished apply '
        'landed a newer version than the running process serves, and '
        '``apply_in_progress`` (with the re-attachable task_id) while a '
        'download is verifiably alive in this process.'
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
    try:
        payload = _enrich_with_apply_state(payload)
    except Exception as e:
        logger.warning('[Update] apply-state enrichment failed: %s', e)
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
        from lib.self_update._version import current_version
        _write_apply_state({'status': 'running', 'task_id': task_id,
                            'started_at': time.time(),
                            'old_version': current_version()})
        try:
            result = apply_update(progress=_progress)
        except Exception as e:
            logger.error('[Update] apply failed: %s', e, exc_info=True)
            _write_apply_state({'status': 'failed', 'task_id': task_id,
                                'finished_at': time.time(), 'ok': False,
                                'error': 'Update failed unexpectedly.',
                                'detail': str(e)[:300]})
            push_event(UPDATE_CHANNEL, task_id, {
                'type': 'done', 'ok': False,
                'error': 'Update failed unexpectedly. Check the server log.',
                'detail': str(e)[:300],
            })
            _ACTIVE_APPLIES.pop(task_id, None)
            return
        # Terminal state is written BEFORE the registry pop: a concurrent
        # /update/check between the two must never see a 'running' marker
        # with no live thread and rewrite it to 'interrupted'.
        _write_apply_state({'status': 'done', 'task_id': task_id,
                            'finished_at': time.time(), **result})
        push_event(UPDATE_CHANNEL, task_id, {'type': 'done', **result})
        _ACTIVE_APPLIES.pop(task_id, None)

    t = threading.Thread(target=_worker, name=f'tofu-update-{task_id[:8]}',
                         daemon=True)
    _ACTIVE_APPLIES[task_id] = t
    t.start()
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


def _perform_server_reexec(reason: str) -> bool:
    """Re-exec the current process (unconditional).

    The caller must have verified there is no in-flight work — see
    update_restart's list_running_tasks guard and lib/auto_restart.py's
    precondition bundle. Extracted from _deferred_reexec so the HEAD-moved
    auto-restart watcher shares the exact same exec contract: clean
    dirty-bit, env-reexec guard reset, listen-socket close-on-exec, port
    reclaim hint, same interpreter + argv. Returns False when execv fails
    (the process keeps running); on success it never returns at all (the
    process image is replaced).
    """
    logger.info('[Update] Re-execing server (%s): %s %s',
                reason, sys.executable, ' '.join(sys.argv))
    # Carry write-freshness tokens across the restart — os.execv replaces
    # the process image WITHOUT running atexit handlers, so the snapshot
    # must be written explicitly here (the signal path is covered by the
    # atexit hook in server.py). Best-effort: never blocks a restart.
    try:
        from lib import write_freshness as _wf
        _wf.save_snapshot()
    except Exception as _wf_e:
        logger.warning('[Update] write-freshness snapshot save failed: %s', _wf_e)
    # Flip the clean-shutdown dirty-bit: an in-place re-exec is a controlled
    # exit, so the fresh image must NOT flag the previous PID as an OS kill.
    try:
        from lib.shutdown_marker import mark_clean
        mark_clean('restart')
    except Exception as _sm_e:
        logger.warning('[Update] mark_clean(restart) failed: %s', _sm_e)
    # re-exec marker (pt_aa3cd224b3b346e7): tofu_guard must not relaunch into
    # the re-exec window (old process dead → new one not yet exec'd/bound).
    # execv KEEPS the pid, so the guard's process-age check can never see a
    # re-exec — this marker is the only truthful signal. The fresh image
    # clears it at boot-ready (server.py); the guard ignores markers older
    # than 300s. Best-effort: a write failure degrades to the pre-marker
    # behavior (a duplicate relaunch that dies harmlessly on the instance
    # lock), never blocks the restart.
    try:
        with open(os.path.join(data_root(), '.reexec_in_progress'), 'w') as _fh:
            json.dump({'pid': os.getpid(), 'ts': time.time()}, _fh)
    except Exception as _mk_e:
        logger.warning('[Update] re-exec marker write failed (guard may race): %s',
                       _mk_e)
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
        return False
    return True


def _deferred_reexec(delay: float = 0.6):
    """Re-exec the current process after a short delay.

    Runs in a daemon thread so the HTTP response flushes first. Mirrors
    server.py's launch contract: same interpreter, same argv. The instance
    lock fd is close-on-exec, so it releases before the new image re-acquires.
    """
    time.sleep(delay)
    _perform_server_reexec('update')


def _lifecycle_origin(own_conv, force, running_count):
    """Attribution payload recorded on every pending approval request.

    This is what makes the NEXT restart attempt attributable in seconds:
    user-agent, socket peer, conversation, force flag, whether a real
    credential rode along, and how many tasks were in flight.
    """
    try:
        cred = bool(request.headers.get('Authorization') or request.cookies)
        ua = request.headers.get('User-Agent', '')
        peer = request.remote_addr or ''
    except Exception as e:
        logger.debug('[Update] origin capture degraded: %s', e)
        cred, ua, peer = False, '', ''
    return {'ua': ua, 'remote_addr': peer, 'conv_id': own_conv or '',
            'force': bool(force), 'running_tasks': running_count,
            'credential': cred}


def _approval_required(action, origin):
    """202 + pending-approval record — the gate's default answer.

    Nothing is executed; the human must approve in the UI and the caller
    retries with ``approvalId``. Loud by construction (create_request
    audits + logs)."""
    rec = _lca.create_request(action, origin=origin)
    resp, _ = api_ok({'needsApproval': True,
                      'pendingApproval': rec,
                      'message': (
                          'A live-server %s requires HUMAN approval. The '
                          'request was registered as pending; approve it in '
                          'the Tofu UI (Settings → 更新/Update), then retry '
                          'with {"approvalId": "%s"}.' % (action, rec['id']))})
    return resp, 202


def _consume_or_forbid(approval_id, action):
    """validate (early) → consume (at acceptance). Returns an error tuple or None."""
    ok, why = _lca.validate(approval_id, action)
    if not ok:
        logger.warning('[Update] %s approval %s rejected: %s',
                       action, approval_id[:8], why)
        audit_log('lifecycle_token_rejected', approval_id=approval_id,
                  action=action, reason=why)
        return api_forbidden(
            'Invalid %s approval (%s). Register a new request (POST without '
            'approvalId → 202) and have a human approve it in the UI.'
            % (action, why))
    return None


@api_v1_update_bp.route('/api/v1/update/restart', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Restart the server',
    description=(
        'Re-execs the server process so freshly-pulled code takes effect. '
        'HUMAN-APPROVAL GATED: without a valid approvalId this only '
        'registers a pending approval (202) and executes nothing — a human '
        'approves it in the UI, then the caller retries with '
        '{"approvalId": "<id>"}. The one-time token is consumed at '
        'acceptance. A second restart within the 15-minute cooldown is '
        'refused (429). Refuses with 409 when OTHER conversations have '
        'in-flight tasks (a re-exec kills every running task); pass '
        '{"force": true} to override (the token survives the 409).'
    ),
    tags=['system'],
)
def update_restart():
    # A restart is an unconditional os.execv of the whole server, so EVERY
    # in-flight task dies with it. Refuse by default when sibling conversations
    # are mid-run — otherwise an agent's own run_command probing this endpoint
    # silently interrupts all its long-running siblings. The caller's own
    # conversation (if any) is excluded so it can restart itself.
    #
    # HUMAN-APPROVAL GATE (pt_40d00fd526e5479a, 2026-07-28 incident: an
    # autopilot conv curl'ed this endpoint twice in 3 minutes, killing 23
    # in-flight tasks; the "approval" came from its own VU, not a human):
    # without a valid ``approvalId`` the request only REGISTERS a pending
    # approval (202) and executes nothing; the human approves in the UI; the
    # retried request with the id executes. The approval is consumed ONLY at
    # acceptance, so the running-tasks 409 / force retry keeps its token.
    body = parse_body()
    force = bool(body.get('force'))
    own_conv = (body.get('convId') or body.get('conv_id') or '').strip() or None
    approval_id = (body.get('approvalId') or body.get('approval_id')
                   or '').strip()

    # Idempotency net: a second restart within the cooldown is refused — this
    # is what stops a crash-resume / re-drive from double-firing a restart
    # that already succeeded (the state file survives the re-exec).
    remaining = _lca.restart_cooldown_remaining()
    if remaining > 0:
        logger.warning('[Update] Restart REFUSED — cooldown (%ds left of %ds)',
                       remaining, _lca.RESTART_COOLDOWN_SEC)
        audit_log('lifecycle_restart_rate_limited', remaining=remaining,
                  cooldown=_lca.RESTART_COOLDOWN_SEC, conv_id=own_conv or '')
        return api_error(
            'Restart refused: the server was already restarted %ds ago '
            '(cooldown %ds). Retry later.'
            % (_lca.RESTART_COOLDOWN_SEC - remaining,
               _lca.RESTART_COOLDOWN_SEC),
            status=429, retryAfterSec=remaining)

    running = []
    try:
        from lib.tasks_pkg.manager import list_running_tasks
        running = list_running_tasks(exclude_conv_id=own_conv)
    except Exception as e:
        logger.warning('[Update] Could not check running tasks before restart: %s', e)

    if not approval_id:
        return _approval_required(
            'restart', _lifecycle_origin(own_conv, force, len(running)))

    err = _consume_or_forbid(approval_id, 'restart')
    if err is not None:
        return err

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

    # Acceptance: consume the one-time token NOW (a refusal above — the
    # running-tasks 409 — deliberately left it usable for the force retry).
    c_ok, c_why = _lca.consume(approval_id, 'restart')
    if not c_ok:
        logger.warning('[Update] Restart approval %s vanished at acceptance: %s',
                       approval_id[:8], c_why)
        return api_forbidden('Restart approval no longer valid (%s).' % c_why)
    _lca.stamp_restart()
    audit_log('self_update_restart', pid=os.getpid(),
              forced=force, running_tasks=len(running),
              approval_id=approval_id)
    logger.warning('[Update] Restart requested — re-exec scheduled (pid=%d, '
                   'force=%s, running_tasks=%d, approval=%s)',
                   os.getpid(), force, len(running), approval_id[:8])
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
    # Same human-approval gate as restart (pt_40d00fd526e5479a) — a shutdown
    # strands every user and in-flight task, so a unilateral agent call must
    # not be able to trigger it. No cooldown: a shutdown is one-way.
    body = parse_body()
    own_conv = (body.get('convId') or body.get('conv_id') or '').strip() or None
    approval_id = (body.get('approvalId') or body.get('approval_id')
                   or '').strip()

    if not approval_id:
        return _approval_required(
            'shutdown', _lifecycle_origin(own_conv, False, None))

    err = _consume_or_forbid(approval_id, 'shutdown')
    if err is not None:
        return err

    c_ok, c_why = _lca.consume(approval_id, 'shutdown')
    if not c_ok:
        return api_forbidden('Shutdown approval no longer valid (%s).' % c_why)
    audit_log('manual_shutdown', pid=os.getpid(), approval_id=approval_id)
    logger.warning('[Shutdown] Manual shutdown requested (pid=%d, approval=%s)',
                   os.getpid(), approval_id[:8])
    threading.Thread(target=_deferred_shutdown, name='tofu-shutdown',
                     daemon=True).start()
    return api_ok({'shuttingDown': True})


# ── Lifecycle approval surface (the human side of the gate) ──────────


@api_v1_update_bp.route('/api/v1/update/lifecycle-approvals', methods=['GET'])
@require_scope('admin')
@api_meta(
    summary='List lifecycle approval requests',
    description=(
        'Lists restart/shutdown approval requests newest-first. '
        '``?status=pending|approved|denied|consumed|expired`` filters by '
        'status, ``?action=restart|shutdown`` by action. This is the queue '
        'the human reviews in the UI before approving.'
    ),
    tags=['system'],
)
def lifecycle_approvals_list():
    status = (request.args.get('status') or '').strip() or None
    action = (request.args.get('action') or '').strip() or None
    records = _lca.list_records(status=status, action=action)
    return api_ok({'records': records,
                   'cooldownRemainingSec': _lca.restart_cooldown_remaining()})


@api_v1_update_bp.route('/api/v1/update/lifecycle-approvals/<approval_id>',
                        methods=['GET'])
@require_scope('admin')
@api_meta(
    summary='Get one lifecycle approval request',
    description=(
        'Poll the status of one approval request — the 202-pended caller '
        '(human UI or an agent that was told to wait) uses this to learn '
        'the human\'s decision.'
    ),
    tags=['system'],
)
def lifecycle_approval_get(approval_id):
    rec = _lca.get(approval_id)
    if rec is None:
        return api_not_found('Unknown approval id')
    return api_ok({'record': rec})


@api_v1_update_bp.route('/api/v1/update/lifecycle-approvals/<approval_id>/decide',
                        methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Approve or deny a lifecycle request (human)',
    description=(
        'The HUMAN decision on a pending restart/shutdown request. Approving '
        'mints a one-time, short-TTL token: the caller retries the gated '
        'endpoint with {"approvalId": "<id>"} and the action executes. The '
        'token is consumed at acceptance, so exactly one action rides on one '
        'approval.'
    ),
    tags=['system'],
)
def lifecycle_approval_decide(approval_id):
    body = parse_body()
    approved = bool(body.get('approved'))
    rec = _lca.decide(approval_id, approved, decided_by='ui',
                      decide_ua=(request.headers.get('User-Agent', '') or ''))
    if rec is None:
        return api_not_found(
            'Unknown, expired or already-decided approval id')
    return api_ok({'record': rec})


__all__ = ['api_v1_update_bp']
