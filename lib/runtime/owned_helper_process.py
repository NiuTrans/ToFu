"""Owned one-job helper processes: scrubbed environment, cancellation, join.

Responsibility
--------------
Run one child for one job with caller-owned stdin/stdout files. The child
inherits loader, temporary-directory and locale paths plus explicitly named
non-secret variables, never account credentials or application lifecycle
authority. Cancellation terminates, escalates to kill if needed and joins the
exact child before the caller's exception propagates; signal failures keep
waiting instead of abandoning a live child. A failed launch never retries.

Entry points
------------
``run_owned_helper`` runs a child to exit and returns its status;
``join_owned_process`` settles an already-started child. Adopters: the VLM PDF
renderer (``lib.pdf_parser.images._render``) and knowledge OCR
(``lib.knowledge.ocr``). Each starts one child per job from a
``PinnedHelperSource``; there is no resident pool.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import os
import subprocess

from lib.log import get_logger

logger = get_logger(__name__)

HELPER_ENVIRONMENT_NAMES = (
    'PATH', 'SystemRoot', 'WINDIR', 'LD_LIBRARY_PATH', 'LD_PRELOAD',
    'DYLD_LIBRARY_PATH', 'PYTHONPATH', 'TMPDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL',
)
_CANCELLATION_POLL_SECONDS = 0.05
_TERMINATE_GRACE_SECONDS = 0.25


def helper_environment(extra_names: Iterable[str] = ()) -> dict[str, str]:
    names = (*HELPER_ENVIRONMENT_NAMES, *extra_names)
    return {name: os.environ[name] for name in names if name in os.environ}


def join_owned_process(process: subprocess.Popen) -> None:
    """Terminate, escalate to kill, and wait until the exact child has exited."""
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass  # The exact owned child exited between poll and terminate.
        except OSError as error:
            # A failed signal grants no permission to close borrowed files.
            logger.warning('Owned helper terminate failed; retaining child ownership: %s', error)
        try:
            process.wait(timeout=_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # The exact owned child exited between wait and kill.
            except OSError as error:
                logger.warning('Owned helper kill failed; waiting for owned child: %s', error)
    process.wait()


def run_owned_helper(
    command: list[str],
    *,
    stdin,
    stdout,
    extra_environment_names: Iterable[str] = (),
    cancelled: Callable[[], None] | None = None,
) -> int:
    """Run one owned child to exit; ``cancelled`` raises to abort it.

    Without ``cancelled`` the calling thread blocks in ``wait`` and releases
    the GIL for the whole job. With it, the callback runs before launch and
    between short waits; whatever it raises propagates after the child joined.
    """
    if cancelled is not None:
        cancelled()
    process = subprocess.Popen(
        command, stdin=stdin, stdout=stdout, stderr=subprocess.DEVNULL,
        env=helper_environment(extra_environment_names), close_fds=True)
    try:
        if cancelled is None:
            return process.wait()
        while True:
            cancelled()
            try:
                return process.wait(timeout=_CANCELLATION_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                continue
    finally:
        join_owned_process(process)


__all__ = ['HELPER_ENVIRONMENT_NAMES', 'helper_environment', 'join_owned_process', 'run_owned_helper']
