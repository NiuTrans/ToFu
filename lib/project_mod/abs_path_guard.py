"""lib/project_mod/abs_path_guard.py — Absolute-path sandbox policy.

``read_files`` and ``write_file`` accept absolute (``/etc/passwd``) and
``~``-relative paths and deliberately step OUTSIDE any registered
workspace root — this is essential for the local desktop / CLI use case
(reading a PDF from ``~/Downloads``, writing to another repo by absolute
path, etc.).

That same capability is a critical vulnerability when the tool call
originates from a **remote, partially-trusted principal** — e.g. a
third-party holder of an ``agents:run`` / ``chat`` API key hitting
``POST /api/v1/agent/run``. For such a caller, an unrestricted absolute
read is arbitrary server-file disclosure (``/etc/passwd``, ``~/.ssh``,
``/proc/self/environ``) and an unrestricted absolute write is
persistence / RCE (``~/.bashrc``).

Policy
------
A :class:`contextvars.ContextVar` records whether the current task is
"restricted". It defaults to **False** (permissive) so local/desktop
behaviour is unchanged. The chat task handler flips it **True** for any
task that carries a remote-API marker (``_api_key_id`` from a non-cookie
key, ``_via_agent_run``, ``_compat_openai`` / ``_compat_anthropic``).

When restricted, absolute / ``~`` paths are allowed ONLY if they resolve
(via ``realpath`` — symlinks followed) to a location inside a registered
workspace root. Anything else is refused with a clear error. This is
enforced at the lowest choke points — :func:`enforce_abs_read` (called
from ``_read_absolute_file``) and the absolute branch of
``_resolve_write_path`` — so every code path that reaches the filesystem
is covered, regardless of which tool was used.
"""

from __future__ import annotations

import contextvars
import os

from lib.log import get_logger

logger = get_logger(__name__)

# Default False = permissive (local desktop/CLI). Set True per-task for
# remote API callers. A ContextVar (not thread-local) so it rides along
# correctly when the orchestrator copies context into worker execution.
_restrict_abs_paths: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'tofu_restrict_abs_paths', default=False)


class AbsPathDenied(ValueError):
    """Raised when a restricted principal touches an out-of-root abs path."""


def set_restricted(value: bool):
    """Set the per-context restriction flag; returns the reset token."""
    return _restrict_abs_paths.set(bool(value))


def reset_restricted(token) -> None:
    """Restore a previous restriction state from :func:`set_restricted`."""
    try:
        _restrict_abs_paths.reset(token)
    except (ValueError, LookupError) as e:
        # Token from a different context (e.g. handler ran in a pool
        # thread). Fall back to clearing — the next task sets it afresh.
        logger.debug('[AbsGuard] reset token mismatch (%s); clearing flag', e)
        _restrict_abs_paths.set(False)


def is_restricted() -> bool:
    return _restrict_abs_paths.get()


def task_is_remote(task: dict) -> bool:
    """True when ``task`` was created by a remote API principal.

    Cookie-authenticated UI calls and the local CLI are NOT remote: they
    carry no ``_api_key_id`` (cookie auth resolves to the local-admin
    context whose ``key_id`` is empty) and no compat / agent-run marker.
    """
    if not isinstance(task, dict):
        return False
    if task.get('_via_agent_run') or task.get('_compat_openai') \
            or task.get('_compat_anthropic'):
        return True
    # A real Bearer key (non-empty key_id) on the headless surface.
    return bool(task.get('_api_key_id'))


def _within_registered_root(abs_path: str) -> bool:
    """True iff ``abs_path`` (realpath'd) sits inside a registered root."""
    from lib.project_mod.config import _lock, _roots
    real = os.path.realpath(abs_path)
    with _lock:
        roots = [rs['path'] for rs in _roots.values()]
    for root_path in roots:
        real_root = os.path.realpath(root_path)
        if real == real_root or real.startswith(real_root + os.sep):
            return True
    return False


def enforce_abs_read(path: str) -> None:
    """Raise :class:`AbsPathDenied` if a restricted caller reads outside roots.

    No-op for the default (unrestricted) local context. For restricted
    callers, the absolute/``~`` target must realpath into a registered
    workspace root.
    """
    if not is_restricted():
        return
    expanded = os.path.abspath(os.path.expanduser(
        path[7:] if path.startswith('file://') else path))
    if '\x00' in path:
        raise AbsPathDenied('Null byte in path is not allowed.')
    if not _within_registered_root(expanded):
        logger.warning('[AbsGuard] denied out-of-root read by remote caller: %s',
                       expanded)
        raise AbsPathDenied(
            'Access denied: this API key may only read files inside a '
            'registered workspace/project root. Absolute paths outside '
            f'the project sandbox ({path!r}) are not permitted for '
            'headless API callers.')


def enforce_abs_write(abs_path: str) -> None:
    """Raise :class:`AbsPathDenied` if a restricted caller writes outside roots.

    Called from the absolute branch of ``_resolve_write_path`` BEFORE any
    auto-registration of a new root, so a restricted caller can neither
    write outside existing roots nor expand the root set.
    """
    if not is_restricted():
        return
    if '\x00' in (abs_path or ''):
        raise AbsPathDenied('Null byte in path is not allowed.')
    if not _within_registered_root(abs_path):
        logger.warning('[AbsGuard] denied out-of-root write by remote caller: %s',
                       abs_path)
        raise AbsPathDenied(
            'Access denied: this API key may only write files inside a '
            'registered workspace/project root. Writing to absolute paths '
            f'outside the project sandbox ({abs_path!r}) is not permitted '
            'for headless API callers.')


__all__ = [
    'AbsPathDenied', 'set_restricted', 'reset_restricted', 'is_restricted',
    'task_is_remote', 'enforce_abs_read', 'enforce_abs_write',
]
