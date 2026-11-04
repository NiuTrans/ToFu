"""Owner-chosen six-hour cloud idle L1 policy and bounded activity evidence.

No provider/summary call, transcript mutation, timer, or TTL discovery. Completed
context restoration seeds activity; admitted preparation and return update the
live task. Publication records a conservative upper bound on model activity.
The existing model continuation is the only durable carrier of this timestamp.
"""
import time

from lib.log import get_logger

logger = get_logger(__name__)

# Product policy explicitly chosen by the owner on 2026-11-05; not a provider TTL.
CLOUD_IDLE_L1_SECONDS = 6 * 60 * 60


def activity_timestamp_ms(value):
    return value if type(value) is int and 0 < value <= 2**53 - 1 else 0


def record_model_activity(task):
    task['_lastModelActivityAtMs'] = max(
        activity_timestamp_ms(task.get('_lastModelActivityAtMs')),
        int(time.time() * 1000))


def idle_l1_activity(task, *, now):
    """Return an unconsumed eligible activity boundary, otherwise zero."""
    if not isinstance(task, dict) or task.get('aborted'):
        return 0
    activity = activity_timestamp_ms(task.get('_lastModelActivityAtMs'))
    from lib.tasks_pkg.invocation import is_child_invocation
    if not is_child_invocation(task) and not task.get('_transientRuntime'):
        activity = max(activity, activity_timestamp_ms(
            (task.get('config') or {}).get('_contextLastModelActivityAtMs')))
    if (not activity or int(now * 1000) - activity < CLOUD_IDLE_L1_SECONDS * 1000
            or activity == task.get('_lastIdleL1ActivityAtMs')):
        return 0
    from lib.llm_dispatch.context_policy import task_local_inference
    try:
        local = task_local_inference(task)
    except Exception as exc:
        logger.debug('[L1] idle serving identity unavailable: %s', type(exc).__name__)
        return 0
    return activity if local is False else 0
