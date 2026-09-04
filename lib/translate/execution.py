"""Shared finite execution lane for optional translation.

General text, synchronous send, PPTX, settled-turn, and paper translation use
this one owner-fair process-local lane. TaskRuntime remains the durable
lifecycle/error authority; this module owns admission, worker residency, and
queued cancellation for durable and attended callers, plus explicitly
reconstructible follow-up work that may be dropped on saturation.
"""

from __future__ import annotations

import os
from concurrent.futures import Future
from typing import Any, Callable

from lib.agent_core.fair_work_lane import (
    FairWorkLaneQueueFull,
    OwnerFairWorkLane,
)
from lib.agent_core.task_runtime import TaskRuntime
from lib.log import get_logger
from runtime_guards import resolve_resource_budget


logger = get_logger(__name__)


def _worker_idle_seconds() -> float:
    if os.environ.get('TOFU_TRANSLATE_WORKER_IDLE_SECONDS', '').strip() == '0':
        return 0.0
    return float(resolve_resource_budget(
        'TOFU_TRANSLATE_WORKER_IDLE_SECONDS',
        maximum=86_400,
    ))


_translation_lane = OwnerFairWorkLane(
    max_workers=resolve_resource_budget(
        'TOFU_TRANSLATE_WORKERS', maximum=64),
    queue_capacity=resolve_resource_budget(
        'TOFU_TRANSLATE_QUEUE_CAPACITY', maximum=1024),
    idle_seconds=_worker_idle_seconds(),
    thread_name_prefix='tofu-translate',
    metric_pool='translation',
)


def _lane_job_id(runtime: TaskRuntime, task_id: str) -> str:
    return f'{runtime.kind}:{task_id}'


def _attended_lane_job_id(job_id: str) -> str:
    normalized_job_id = str(job_id or '')
    if not normalized_job_id:
        raise ValueError('job_id must be non-empty')
    return f'attended:{normalized_job_id}'


def _reconstructible_lane_job_id(job_id: str) -> str:
    normalized_job_id = str(job_id or '')
    if not normalized_job_id:
        raise ValueError('job_id must be non-empty')
    return f'reconstructible:{normalized_job_id}'


def submit_reconstructible_translation(
    job_id: str,
    *,
    owner_user_id: int,
    function: Callable[[], Any],
) -> Future[Any]:
    """Admit optional derived work behind the owner's existing queue.

    The work has no independent durable task lifecycle: saturation may reject
    it and process restart may drop it. Callers may use this only for state
    that can be reconstructed from an already-durable source artifact.
    """
    return _translation_lane.submit_task(
        _reconstructible_lane_job_id(job_id),
        owner_user_id,
        function,
    )


def submit_attended_translation(
    job_id: str,
    *,
    owner_user_id: int,
    function: Callable[[], Any],
) -> Future[Any]:
    """Admit one attended call without creating a request-local carrier.

    Attended work moves ahead of older optional work for the same owner only;
    cross-owner round-robin fairness and the process-wide queue ceiling remain
    authoritative.
    """
    return _translation_lane.submit_task(
        _attended_lane_job_id(job_id),
        owner_user_id,
        function,
        front_of_owner_queue=True,
    )


def cancel_attended_translation(job_id: str) -> bool:
    """Remove an attended call if it has not entered a worker yet."""
    return _translation_lane.cancel_task(_attended_lane_job_id(job_id))


def submit_translation_task(
    runtime: TaskRuntime,
    task_id: str,
    function: Callable[..., Any],
    *args: Any,
    running_fields: dict[str, Any] | None = None,
    **kwargs: Any,
) -> bool:
    """Admit a TaskRuntime worker and move it to running only on entry."""
    lane_job_id = _lane_job_id(runtime, task_id)

    def _run_if_live() -> None:
        task = runtime.get(task_id)
        if task is None:
            return
        if task['abort_event'].is_set():
            runtime.finish(task_id)
            return
        if not runtime.mark_running(task_id, fields=running_fields):
            return
        function(*args, **kwargs)

    def _submit(
        _task_id: str,
        owner_user_id: int,
        worker: Callable[[], None],
    ):
        return _translation_lane.submit_task(
            lane_job_id,
            owner_user_id,
            worker,
        )

    try:
        runtime.submit_worker(task_id, _submit, _run_if_live)
    except FairWorkLaneQueueFull:
        from lib.error_envelope import make_envelope

        runtime.finish(
            task_id,
            error=make_envelope(
                'server_busy',
                detail='Translation queue is full; retry shortly',
                context='translation:queue_saturated',
                source='lib.translate.execution',
            ),
            error_context='translation:queue_saturated',
        )
        return False
    except Exception as submission_error:
        from lib.error_envelope import make_envelope

        logger.error(
            '[Translate] background worker admission failed task=%s: %s',
            task_id[:8], submission_error, exc_info=True,
        )
        runtime.finish(
            task_id,
            error=make_envelope(
                'task_start_failed',
                detail='Translation worker could not start; retry shortly',
                context='translation:worker_start_failed',
                source='lib.translate.execution',
            ),
            error_context='translation:worker_start_failed',
        )
        return False
    return True


def abort_translation_task(
    runtime: TaskRuntime,
    task_id: str,
    *,
    user_id: int,
) -> bool:
    """Signal running work or immediately settle a removed queued task."""
    if not runtime.abort_owned(task_id, user_id=user_id):
        return False
    if _translation_lane.cancel_task(_lane_job_id(runtime, task_id)):
        runtime.finish(task_id)
    return True


def translation_lane_snapshot() -> dict[str, int | float | bool]:
    """Expose bounded scheduling evidence for diagnostics and tests."""
    return _translation_lane.snapshot()


__all__ = [
    'abort_translation_task',
    'cancel_attended_translation',
    'submit_reconstructible_translation',
    'submit_attended_translation',
    'submit_translation_task',
    'translation_lane_snapshot',
]
