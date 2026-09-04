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
  POST /api/v1/update/restart  — admin: replace the managed worker (or re-exec
                                 an unmanaged process) so pulled ``.py``
                                 changes take effect. Explicit only — ``apply``
                                 never auto-restarts.

The heavy lifting lives in :mod:`lib.self_update`; this layer is a thin,
fully-logged HTTP wrapper.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid

from quart import Blueprint, request

from lib import lifecycle_approval as _lca
from lib.runtime_paths import data_root
from lib.api_response import (
    api_conflict, api_error, api_forbidden, api_internal_error, api_not_found,
    api_ok,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.agent_core.push import push_event
from lib.request_parser import parse_body

from .auth import request_user_id, require_auth, require_scope

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


def _prepare_server_reexec_frontend() -> str:
    """Repair and validate the frontend graph while this worker is still live.

    The lifecycle CLI already owns the cross-process build lock and the
    source-checkout/release distinction.  Reuse that exact preparation path,
    then require validation to pass *before* the serving loop is fenced.  A
    release without Node can still serve its published graph, but it may not
    stop a healthy old worker in order to discover that the new graph is bad.
    """
    from lib.process_roles import CAPABILITY_FRONTEND, process_role_has

    role = (os.environ.get('TOFU_PROCESS_ROLE') or 'all').strip().lower()
    try:
        owns_frontend = process_role_has(role, CAPABILITY_FRONTEND)
    except ValueError as exc:
        return f'invalid process role for restart preflight: {exc}'
    if not owns_frontend:
        return ''

    try:
        from serverctl import prepare_source_frontend_artifact
        repair_error = prepare_source_frontend_artifact('in-app restart')
    except Exception as exc:
        logger.warning(
            '[Update] frontend artifact preparation failed: %s',
            type(exc).__name__,
        )
        return f'frontend artifact preparation failed: {exc}'
    if repair_error:
        return repair_error
    try:
        from lib.vite_assets import validate_vite_artifact
        validate_vite_artifact()
    except Exception as exc:
        logger.warning(
            '[Update] frontend artifact validation failed: %s',
            type(exc).__name__,
        )
        return (
            'frontend artifact is not restart-safe: '
            f'{exc}; run `npm run build:frontend` while the current server '
            'remains online')
    return ''


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
    owner_user_id = request_user_id()

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
        push_event(
            UPDATE_CHANNEL, task_id, frame, user_id=owner_user_id)

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
            }, user_id=owner_user_id)
            _ACTIVE_APPLIES.pop(task_id, None)
            return
        # Terminal state is written BEFORE the registry pop: a concurrent
        # /update/check between the two must never see a 'running' marker
        # with no live thread and rewrite it to 'interrupted'.
        _write_apply_state({'status': 'done', 'task_id': task_id,
                            'finished_at': time.time(), **result})
        push_event(
            UPDATE_CHANNEL,
            task_id,
            {'type': 'done', **result},
            user_id=owner_user_id,
        )
        _ACTIVE_APPLIES.pop(task_id, None)

    t = threading.Thread(target=_worker, name=f'tofu-update-{task_id[:8]}',
                         daemon=True)
    _ACTIVE_APPLIES[task_id] = t
    t.start()
    logger.info('[Update] apply started in background (task=%s)', task_id[:8])
    return api_ok({'taskId': task_id, 'started': True})


def _perform_server_reexec(reason: str) -> bool:
    """Request a graceful process replacement through its lifecycle owner.

    The caller must have verified there is no in-flight work — see
    update_restart's list_running_tasks guard and lib/auto_restart.py's
    precondition bundle. A Supervisor-managed worker is replaced by a freshly
    reloaded manager so launch-time resource policy cannot remain pinned in a
    stale parent or inherited environment. An unmanaged worker retains the
    bounded in-place ``execv`` path. Returns ``False`` only when the relevant
    lifecycle bridge refuses the request.
    """
    preflight_error = _prepare_server_reexec_frontend()
    if preflight_error:
        logger.error(
            '[Update] Restart preflight refused before shutdown: %s',
            preflight_error,
        )
        audit_log(
            'self_update_restart_preflight_failed',
            pid=os.getpid(),
            reason=reason,
            detail=preflight_error[:500],
        )
        return False

    if os.environ.get('TOFU_MANAGED_BY') == 'supervisor':
        project = os.path.realpath(
            os.environ.get('TOFU_PROJECT_PATH')
            or os.path.join(os.path.dirname(__file__), '..', '..'))
        try:
            from supervisor_protocol import request_deferred_worker_restart
            result = request_deferred_worker_restart(
                project,
                source=f'application-{reason}',
                environment=os.environ,
            )
        except Exception as exc:
            logger.error(
                '[Update] Managed worker replacement refused: %s',
                exc,
                exc_info=True,
            )
            audit_log(
                'self_update_managed_restart_failed',
                pid=os.getpid(),
                reason=reason,
                detail=str(exc)[:500],
            )
            return False
        audit_log(
            'self_update_managed_restart_requested',
            pid=os.getpid(),
            reason=reason,
            manager_pid=(result.get('supervisorRefresh') or {}).get(
                'managerPid'),
        )
        logger.warning(
            '[Update] Current Supervisor accepted a fresh worker generation '
            '(%s)', reason)
        return True

    from lib.server_reexec import (
        begin_server_reexec,
        finish_server_reexec_preparation,
    )

    if not begin_server_reexec(reason):
        return False
    logger.info(
        '[Update] Graceful server re-exec requested (%s); draining lifecycle',
        reason,
    )
    # The shutdown event and its hard deadline are armed above before these
    # best-effort writes touch the data volume.  ``execv`` skips atexit, so the
    # freshness snapshot still has to land before the main thread replaces the
    # process image.
    try:
        from lib import write_freshness as _wf
        _wf.save_snapshot()
    except Exception as _wf_e:
        logger.warning('[Update] write-freshness snapshot save failed: %s', _wf_e)
    # tofu_guard must not relaunch into the drain / fresh-boot window.
    # execv KEEPS the pid, so the guard's process-age check can never see a
    # re-exec — this marker is the only truthful signal. The fresh image
    # clears it at boot-ready (server.py); the guard ignores markers older
    # than 300s. Best-effort: the live instance lock remains the second fence.
    try:
        with open(os.path.join(data_root(), '.reexec_in_progress'), 'w') as _fh:
            json.dump({'pid': os.getpid(), 'ts': time.time()}, _fh)
    except Exception as _mk_e:
        logger.warning('[Update] re-exec marker write failed (guard may race): %s',
                       _mk_e)
    finish_server_reexec_preparation()
    return True


def _deferred_reexec(delay: float = 0.6):
    """Request lifecycle-owned replacement after the HTTP response can flush.

    Managed workers hand the replacement to the independent Supervisor;
    unmanaged workers let the main serving thread own the actual exec after
    the production shutdown stack releases child authorities and sockets.
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
        'Replaces the managed worker (or re-execs an unmanaged process) so '
        'freshly-pulled code takes effect. '
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
    # HUMAN-APPROVAL GATE (, 2026-07-28 incident: an
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

    # Preparation is intentionally before one-time approval consumption and
    # before the cooldown is stamped.  A stale bundle is recoverable while the
    # old worker is still serving; it must not burn the operator's approval or
    # turn a repairable artifact error into an outage.
    preflight_error = _prepare_server_reexec_frontend()
    if preflight_error:
        logger.error(
            '[Update] Restart REFUSED — frontend preflight failed: %s',
            preflight_error,
        )
        audit_log(
            'self_update_restart_preflight_failed',
            pid=os.getpid(),
            approval_id=approval_id,
            detail=preflight_error[:500],
        )
        return api_conflict(
            'Restart refused before shutdown because the frontend artifact '
            'could not be prepared. The current server is still running.',
            restartPreflightFailed=True,
            detail=preflight_error,
        )

    # Acceptance: atomically consume the one-time token AND start the global
    # cooldown. A refusal above deliberately left the token usable for the
    # force retry; once here, two concurrently approved requests must not both
    # pass a stale cooldown read and schedule two re-execs.
    c_ok, c_why, c_remaining = _lca.consume_restart(approval_id)
    if not c_ok:
        if c_why == 'cooldown':
            logger.warning('[Update] Restart approval %s lost acceptance race '
                           '— cooldown now active (%ds remaining)',
                           approval_id[:8], c_remaining)
            return api_error(
                'Restart refused: another restart was accepted concurrently. '
                'Retry later.', status=429, retryAfterSec=c_remaining)
        logger.warning('[Update] Restart approval %s vanished at acceptance: %s',
                       approval_id[:8], c_why)
        return api_forbidden('Restart approval no longer valid (%s).' % c_why)
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
    # Same human-approval gate as restart () — a shutdown
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
