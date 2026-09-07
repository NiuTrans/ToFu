"""Describe-to-recommend task store — server-owned streaming recommendation.

Mirrors ``qa_runtime`` (the proven per-question pattern): the describe flow runs
as a server-owned ``TaskRuntime`` task so the two-phase pipeline (LLM
interpretation → per-candidate arXiv grounding) can be streamed to the frontend
one grounded card at a time. The frontend polls
``/api/v1/paper/recommend/poll`` and replays the append-only event log —
refresh-safe and tab-switch-safe, no SSE (aligned with the Q&A tab's transport).

Unlike the report task (deduped by ``(paper_hash, lang)``), a recommend task is
per-DESCRIPTION — each submit spawns a fresh task keyed by its own ``task_id``.
We keep a light index of the most recent task per normalized description so a
reattach after refresh can find an in-flight run.
"""

import hashlib
import threading

from lib.log import get_logger
from lib.agent_core.task_runtime import TaskRuntime

logger = get_logger(__name__)


_recommend_runtime = TaskRuntime(
    'paper-recommend', ttl=1800,
    push_channel='paper',
    error_source='routes.paper:recommend',
)
# (owner, description-key) → most-recent task_id.
_recommend_latest_index: dict[tuple[int, str], str] = {}
_recommend_index_lock = threading.Lock()
_RECOMMEND_TASK_TTL = 1800


def _recommend_key(description: str) -> str:
    """Stable short key for a description (used to reattach the latest run)."""
    return hashlib.sha1((description or '').strip().encode('utf-8')).hexdigest()[:16]


def _recommend_register_latest(
    desc_key: str, task_id: str, *, user_id: int,
) -> None:
    with _recommend_index_lock:
        _recommend_latest_index[(user_id, desc_key)] = task_id


def _new_recommend_task(task_id, description, max_results, *, user_id: int):
    """Create a fresh recommend task. Registers it as the description's latest."""
    desc_key = _recommend_key(description)
    task = _recommend_runtime.create(
        user_id=user_id,
        task_id=task_id,
        meta={'desc_key': desc_key, 'max_results': max_results},
    )
    _recommend_runtime.update_fields(task_id, fields={
        'task_id': task_id,
        'description': description,
        'desc_key': desc_key,
        'max_results': max_results,
        'results': [],          # grounded cards, in emit order
        'correction': None,     # optional false-premise correction block
    })
    _recommend_register_latest(desc_key, task_id, user_id=user_id)
    return task


def _append_recommend_event(task, event):
    """Append an event to the recommend task's log (thread-safe; auto-pushes)."""
    _recommend_runtime.append_event(task['task_id'], event)


def _cleanup_stale_recommend_tasks():
    """Drop finished recommend tasks older than TTL and prune the latest index."""
    n = _recommend_runtime.cleanup_stale()
    if n:
        live_task_ids = _recommend_runtime.task_ids()
        with _recommend_index_lock:
            stale = [
                key for key, task_id in _recommend_latest_index.items()
                if task_id not in live_task_ids
            ]
            for k in stale:
                _recommend_latest_index.pop(k, None)
        logger.debug('[Paper:Recommend] Cleaned %d stale task(s)', n)
