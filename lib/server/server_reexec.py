"""Coordinate an in-place server re-exec through the production lifespan.

Request threads may ask for a restart, but only ``server.py`` may replace the
process image.  The serving loop first stops accepting connections and runs
Quart's bounded shutdown stack; the main thread then executes the fresh image.
This keeps child authorities, background producers, and transport sockets
inside their declared lifecycle instead of orphaning them across ``execv``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import os
import sys
import threading


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
    executable = sys.executable
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
    'begin_server_reexec',
    'confirm_server_reexec_storage_boundary_released',
    'execute_pending_server_reexec',
    'finish_server_reexec_preparation',
    'install_server_reexec_shutdown_requester',
    'pending_server_reexec_reason',
]
