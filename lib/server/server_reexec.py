"""Own restart preparation, Supervisor handoff and in-place process replacement.

Request threads may ask for a restart, but only ``server.py`` may replace the
process image.  The serving loop first stops accepting connections and runs
Quart's bounded shutdown stack; the main thread then executes the fresh image.
This keeps child authorities, background producers, and transport sockets
inside their declared lifecycle instead of orphaning them across ``execv``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import os
import sys
import threading
import time

from lib.log import audit_log, get_logger
from lib.runtime.runtime_paths import data_root

logger = get_logger(__name__)


ShutdownRequester = Callable[[str], None]
ExecFunction = Callable[[str, list[str]], object]


@dataclass(frozen=True)
class _PendingServerReexec:
    reason: str
    preparation_finished: threading.Event
    storage_boundary_released: threading.Event


_STATE_LOCK = threading.RLock()
_shutdown_requester: ShutdownRequester | None = None
_pending_request: _PendingServerReexec | None = None


def prepare_server_reexec_frontend() -> str:
    """Prepare current disk code in an isolated control interpreter."""
    from serverctl_pkg.application_restart import run_application_restart_control

    try:
        run_application_restart_control(_restart_project(), stage='prepare')
    except (OSError, RuntimeError) as exc:
        logger.warning('[Update] application restart preparation failed: %s', exc)
        return str(exc)
    return ''


def _restart_project() -> str:
    return os.path.realpath(
        os.environ.get('TOFU_PROJECT_PATH')
        or os.path.join(os.path.dirname(__file__), '..', '..'))


def perform_server_reexec(reason: str, *, frontend_prevalidated: bool = False,
                           requested_at: float | None = None) -> bool:
    """Request a graceful process replacement through its lifecycle owner.

    The caller must have verified there is no in-flight work — see
    update_restart's list_running_tasks guard and lib/auto_restart.py's
    precondition bundle. The host-local Supervisor is offered the replacement
    for EVERY launch mode (not only ``TOFU_MANAGED_BY=supervisor``): the
    manager kills and respawns the worker from outside, so a restart never
    depends on the sick process replacing its own image — the failure mode
    behind the stuck-retry incident. A freshly reloaded manager also cannot
    keep launch-time resource policy pinned in a stale parent or inherited
    environment. Only when the manager cannot be reached or refuses does the
    worker fall back to the bounded in-place ``execv`` path. Returns ``False``
    only when every lifecycle bridge refuses the request.

    ``frontend_prevalidated`` marks that the synchronous request path already
    ran (and passed) the frontend artifact preflight this restart; re-running
    it here would rebuild/validate the same artifact twice, adding several
    seconds of dead time to every restart on a slow filesystem.
    """
    if frontend_prevalidated:
        logger.info('[Update] Frontend preflight already passed in-request; '
                    'skipping the duplicate build')
    else:
        preflight_error = prepare_server_reexec_frontend()
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

    project = _restart_project()
    try:
        from serverctl_pkg.application_restart import run_application_restart_control
        supervisor_started = time.monotonic()
        restart_kwargs = {
            'stage': 'delegate',
            'reason': reason,
        }
        if requested_at is not None:
            restart_kwargs['requested_at'] = requested_at
        result = run_application_restart_control(project, **restart_kwargs)
        logger.info(
            '[Update] Supervisor accepted deferred worker restart in '
            '%.1fs', time.monotonic() - supervisor_started)
    except Exception as exc:
        logger.warning(
            '[Update] Manager delegation unavailable (%s); falling back to '
            'in-place re-exec', exc)
        audit_log(
            'self_update_manager_delegation_unavailable',
            pid=os.getpid(),
            reason=reason,
            detail=str(exc)[:500],
        )
    else:
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

    if not begin_server_reexec(reason):
        # Every bridge refused BEFORE the shutdown point of no return, so this
        # process is guaranteed to keep serving. Roll back the acceptance
        # stamp consume_restart committed in-request: leaving it would make
        # the next human-approved retry hit cooldown/restartPending against a
        # process that will never replace itself (the stuck-retry incident).
        try:
            from lib.lifecycle_approval import rollback_restart_stamp
            if rollback_restart_stamp(pid=os.getpid()):
                logger.warning(
                    '[Update] Restart refused by every bridge; acceptance '
                    'stamp rolled back so a retry can proceed')
        except Exception as rb_exc:
            logger.warning('[Update] Restart stamp rollback failed: %s', rb_exc)
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


def install_server_reexec_shutdown_requester(
    requester: ShutdownRequester,
) -> None:
    """Install the serving-loop shutdown bridge before requests are accepted."""
    if not callable(requester):
        raise TypeError('server re-exec shutdown requester must be callable')
    global _shutdown_requester
    with _STATE_LOCK:
        _shutdown_requester = requester


def begin_server_reexec(reason: str) -> bool:
    """Fence the serving loop and arm its hard shutdown deadline.

    Repeated callers share one pending request.  Returning ``False`` means no
    shutdown bridge was available or the bridge rejected the request, so the
    caller must leave the current process image in place.
    """
    normalized_reason = str(reason or 'restart').strip()[:80] or 'restart'
    global _pending_request
    with _STATE_LOCK:
        if _pending_request is not None:
            return True
        requester = _shutdown_requester
        if requester is None:
            logging.getLogger(__name__).critical(
                'Server re-exec refused: graceful shutdown bridge is absent')
            return False
        request = _PendingServerReexec(
            reason=normalized_reason,
            preparation_finished=threading.Event(),
            storage_boundary_released=threading.Event(),
        )
        _pending_request = request
    try:
        # The production callback sets the shutdown event and arms the hard
        # deadline before any best-effort FUSE-backed preparation runs.
        requester(normalized_reason)
    except Exception:
        with _STATE_LOCK:
            if _pending_request is request:
                _pending_request = None
        logging.getLogger(__name__).exception(
            'Server re-exec shutdown bridge failed')
        return False
    return True


def finish_server_reexec_preparation() -> bool:
    """Publish that best-effort restart metadata is safe to leave behind."""
    with _STATE_LOCK:
        request = _pending_request
    if request is None:
        return False
    request.preparation_finished.set()
    return True


def confirm_server_reexec_storage_boundary_released() -> bool:
    """Certify that no application-owned Storage Sidecar crosses ``execv``.

    The production shutdown owner calls this only after ``stop_storage()``
    returns.  Personal mode therefore proved that its child process exited;
    external-sidecar mode proved that the application detached from the
    independently managed authority.  The final exec gate consumes this
    certificate instead of inferring release from a completed ASGI lifespan.
    """
    with _STATE_LOCK:
        request = _pending_request
    if request is None:
        return False
    request.storage_boundary_released.set()
    return True


def pending_server_reexec_reason() -> str:
    """Return the bounded pending reason without changing coordinator state."""
    with _STATE_LOCK:
        request = _pending_request
    return request.reason if request is not None else ''


def _clear_inheritable_file_descriptors(logger: logging.Logger) -> int:
    """Make every non-stdio descriptor close on the upcoming exec."""
    try:
        descriptors = [
            int(name) for name in os.listdir('/proc/self/fd')
            if name.isdigit()
        ]
    except OSError as exc:
        logger.debug(
            '[Restart] /proc/self/fd unavailable (%s); scanning bounded FDs',
            exc,
        )
        maximum = os.sysconf('SC_OPEN_MAX') if hasattr(os, 'sysconf') else 4096
        descriptors = range(3, min(maximum, 65536))
    changed = 0
    for descriptor in descriptors:
        if descriptor < 3:
            continue
        try:
            if os.get_inheritable(descriptor):
                os.set_inheritable(descriptor, False)
                changed += 1
        except OSError:
            # The /proc enumeration races ordinary descriptor closure.
            continue
    return changed


def execute_pending_server_reexec(
    *,
    lifecycle_stopped: bool,
    preparation_timeout: float = 2.0,
    exec_function: ExecFunction | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Replace the process image only after the production lifespan stopped.

    Returns ``False`` when no restart is pending.  A real ``os.execv`` never
    returns on success; an injected test function may return, in which case the
    consumed request is cleared and this function returns ``True``.
    """
    global _pending_request
    with _STATE_LOCK:
        request = _pending_request
    if request is None:
        return False
    if not lifecycle_stopped:
        raise RuntimeError(
            'server re-exec requires the completed production shutdown stack')
    if not request.preparation_finished.wait(
        timeout=max(0.0, float(preparation_timeout))
    ):
        raise RuntimeError(
            'server re-exec preparation did not finish before shutdown')
    if not request.storage_boundary_released.is_set():
        raise RuntimeError(
            'server re-exec requires the storage boundary to be released')

    log = logger or logging.getLogger(__name__)
    os.environ.pop('_TOFU_ENV_REEXEC', None)
    runtime_port = (os.environ.get('_TOFU_RUNTIME_PORT', '') or '').strip()
    if runtime_port:
        os.environ['_TOFU_REEXEC_PORT'] = runtime_port
    changed = _clear_inheritable_file_descriptors(log)
    if changed:
        log.info(
            '[Restart] Marked %d inherited descriptor(s) close-on-exec',
            changed,
        )
    # A local runtime replica may be stale after the restart's own package
    # installs; the source interpreter is always current, and the next
    # managed spawn re-selects a fresh replica.
    from serverctl_pkg.runtime_replica import authoritative_interpreter
    executable = authoritative_interpreter()
    arguments = [executable, *sys.argv]
    log.info(
        '[Restart] Production shutdown complete; re-execing (%s): %s %s',
        request.reason,
        executable,
        ' '.join(sys.argv),
    )
    execute = os.execv if exec_function is None else exec_function
    try:
        execute(executable, arguments)
    except OSError as exc:
        log.critical(
            '[Restart] execv failed after graceful shutdown: %s',
            exc,
            exc_info=True,
        )
        raise RuntimeError('server re-exec failed after shutdown') from exc

    # Only a test double can reach this point.  Clear the request so repeated
    # unit calls do not impersonate a second restart.
    with _STATE_LOCK:
        if _pending_request is request:
            _pending_request = None
    return True


def _reset_server_reexec_state_for_tests() -> None:
    """Reset process globals for isolated unit tests only."""
    global _pending_request, _shutdown_requester
    with _STATE_LOCK:
        _pending_request = None
        _shutdown_requester = None


__all__ = [
    'perform_server_reexec',
    'prepare_server_reexec_frontend',
    'begin_server_reexec',
    'confirm_server_reexec_storage_boundary_released',
    'execute_pending_server_reexec',
    'finish_server_reexec_preparation',
    'install_server_reexec_shutdown_requester',
    'pending_server_reexec_reason',
]
