"""Bounded child-process execution for scheduled and maintenance jobs.

This module owns timeout enforcement, process-tree termination, output caps,
and audit state for every scheduler task that crosses a process boundary.
Callers retain domain-specific result parsing; they do not call ``Popen`` or
invent a second cancellation policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import signal
import subprocess
import threading
import time
from collections.abc import Sequence

from lib.identity import PrincipalContext
from lib.log import audit_log, get_logger


logger = get_logger(__name__)
_TERMINATE_GRACE_SECONDS = 2.0
_OUTPUT_LIMIT = 50_000
_ERROR_OUTPUT_LIMIT = 10_000


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    error: str = ''

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.error


class _ProcessCancelled(RuntimeError):
    """The scheduler lifecycle requested child-process cancellation."""


def _validated_principal(principal: PrincipalContext) -> PrincipalContext:
    if not isinstance(principal, PrincipalContext):
        raise TypeError('scheduled process requires PrincipalContext')
    if not principal.scopes:
        raise PermissionError('scheduled process principal requires a scope')
    return principal


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, OSError):
        pass
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == 'posix':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, OSError):
        pass
    process.wait()


def run_bounded_process(
    arguments: Sequence[str],
    *,
    max_runtime: int,
    job_id: str,
    job_type: str,
    principal: PrincipalContext,
    cancel_event: threading.Event | None = None,
) -> BoundedProcessResult:
    """Run one child in its own process group and enforce one deadline."""
    principal = _validated_principal(principal)
    timeout_seconds = max(1, int(max_runtime))
    started = time.monotonic()
    audit_log(
        'scheduled_process_started',
        task_id=str(job_id),
        task_type=str(job_type),
        principal_kind=principal.kind,
        principal_subject_id=principal.subject_id,
        owner_user_id=principal.owner_user_id,
        tenant_id=principal.tenant_id,
        max_runtime=timeout_seconds,
    )
    process = None
    try:
        process = subprocess.Popen(
            list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=(os.name == 'posix'),
        )
        deadline = started + timeout_seconds
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise _ProcessCancelled('scheduler shutdown requested')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout_seconds)
            try:
                stdout, stderr = process.communicate(timeout=min(0.25, remaining))
                break
            except subprocess.TimeoutExpired:
                continue
        result = BoundedProcessResult(
            returncode=process.returncode,
            stdout=(stdout or '')[:_OUTPUT_LIMIT],
            stderr=(stderr or '')[:_ERROR_OUTPUT_LIMIT],
        )
        audit_log(
            'scheduled_process_finished',
            task_id=str(job_id),
            task_type=str(job_type),
            principal_kind=principal.kind,
            principal_subject_id=principal.subject_id,
            owner_user_id=principal.owner_user_id,
            returncode=process.returncode,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return result
    except subprocess.TimeoutExpired:
        if process is not None:
            _terminate_process_tree(process)
        audit_log(
            'scheduled_process_finished',
            task_id=str(job_id),
            task_type=str(job_type),
            principal_kind=principal.kind,
            principal_subject_id=principal.subject_id,
            owner_user_id=principal.owner_user_id,
            outcome='timeout',
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return BoundedProcessResult(
            returncode=None,
            stdout='',
            stderr='',
            timed_out=True,
            error=f'Timed out after {timeout_seconds}s',
        )
    except _ProcessCancelled:
        if process is not None:
            _terminate_process_tree(process)
        audit_log(
            'scheduled_process_finished',
            task_id=str(job_id),
            task_type=str(job_type),
            principal_kind=principal.kind,
            principal_subject_id=principal.subject_id,
            owner_user_id=principal.owner_user_id,
            outcome='cancelled',
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return BoundedProcessResult(
            returncode=None,
            stdout='',
            stderr='',
            cancelled=True,
            error='Cancelled during scheduler shutdown',
        )
    except Exception as exc:
        if process is not None:
            _terminate_process_tree(process)
        logger.error(
            '[Scheduler] Could not execute %s process for task %s: %s',
            job_type, job_id, exc, exc_info=True)
        audit_log(
            'scheduled_process_finished',
            task_id=str(job_id),
            task_type=str(job_type),
            principal_kind=principal.kind,
            principal_subject_id=principal.subject_id,
            owner_user_id=principal.owner_user_id,
            outcome='spawn_error',
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return BoundedProcessResult(
            returncode=None,
            stdout='',
            stderr='',
            error='Process execution error (see logs)',
        )


__all__ = ['BoundedProcessResult', 'run_bounded_process']
