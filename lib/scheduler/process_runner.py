"""Bounded child-process execution for scheduled and maintenance jobs.

This module owns timeout enforcement, process-tree termination, output caps,
and audit state for every scheduler task that crosses a process boundary.
Callers retain domain-specific result parsing; they do not call ``Popen`` or
invent a second cancellation policy.
"""

from __future__ import annotations

import codecs
import io
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
        return (self.returncode == 0 and not self.timed_out
                and not self.cancelled and not self.error)


class _ProcessCancelled(RuntimeError):
    """The scheduler lifecycle requested child-process cancellation."""


def _validated_principal(principal: PrincipalContext) -> PrincipalContext:
    if not isinstance(principal, PrincipalContext):
        raise TypeError('scheduled process requires PrincipalContext')
    if not principal.scopes:
        raise PermissionError('scheduled process principal requires a scope')
    return principal


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == 'posix':
        # The group outlives its leader when children inherit output pipes.
        # Reaping the direct child is not evidence that the whole job stopped.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
            return
        deadline = time.monotonic() + _TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            process.poll()  # reap the leader, avoiding a zombie-only group
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_TERMINATE_GRACE_SECONDS)


def _collect_bounded_output(process, *, deadline, cancel_event, arguments):
    """Drain nonblocking pipes without retaining bytes past the public caps.

    Python >=3.12 supports nonblocking anonymous pipes on Windows as well as
    POSIX. Polling owns no extra threads; every read is fixed-size, cancellation
    remains observable during continuous output, and inherited pipes cannot
    extend the command deadline after the direct child exits.
    """
    streams = []
    for pipe, limit in ((process.stdout, _OUTPUT_LIMIT),
                        (process.stderr, _ERROR_OUTPUT_LIMIT)):
        os.set_blocking(pipe.fileno(), False)
        decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder('utf-8')(errors='replace'), translate=True)
        streams.append({'pipe': pipe, 'decoder': decoder, 'limit': limit,
                        'chunks': [], 'size': 0, 'eof': False})
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise _ProcessCancelled('scheduler shutdown requested')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, 0)
        progressed = False
        for stream in streams:
            if stream['eof']:
                continue
            try:
                chunk = os.read(stream['pipe'].fileno(), 8192)
            except BlockingIOError:
                continue
            progressed = True
            stream['eof'] = not chunk
            # Continue draining capped streams so the child cannot block on
            # either pipe, but never decode or append discarded output.
            if stream['size'] < stream['limit']:
                text = stream['decoder'].decode(chunk, final=not chunk)
                kept = text[:stream['limit'] - stream['size']]
                stream['chunks'].append(kept)
                stream['size'] += len(kept)
        if all(stream['eof'] for stream in streams) and process.poll() is not None:
            return tuple(''.join(stream['chunks']) for stream in streams)
        if not progressed:
            if cancel_event is None:
                time.sleep(min(0.05, remaining))
            else:
                cancel_event.wait(min(0.05, remaining))


def run_bounded_process(
    arguments: Sequence[str],
    *,
    max_runtime: int,
    job_id: str,
    job_type: str,
    principal: PrincipalContext,
    cancel_event: threading.Event | None = None,
    cwd: str | None = None,
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
        if cancel_event is not None and cancel_event.is_set():
            raise _ProcessCancelled('scheduler shutdown requested')
        process = subprocess.Popen(
            list(arguments),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            start_new_session=(os.name == 'posix'),
            cwd=cwd,
        )
        stdout, stderr = _collect_bounded_output(
            process, deadline=started + timeout_seconds,
            cancel_event=cancel_event, arguments=arguments)
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
    finally:
        if process is not None:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


__all__ = ['BoundedProcessResult', 'run_bounded_process']
