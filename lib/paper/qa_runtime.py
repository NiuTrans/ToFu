"""Paper Q&A task store — server-owned agentic background generation.

Mirrors ``report_runtime`` (the proven pattern): Q&A runs as a server-owned
``TaskRuntime`` task so it can run the SAME tool-calling loop the report engine
uses (web_search / fetch_url). The frontend polls ``/api/v1/paper/qa/poll`` and
replays the append-only event log — refresh-safe and tab-switch-safe, no SSE.

Unlike the report task (deduped by ``(paper_hash, lang)`` because a report is
generated once per paper), a Q&A task is per-QUESTION — each ask spawns a fresh
task keyed by its own ``task_id``. We keep a light per-paper index of the most
recent task so a reattach after refresh can find an in-flight answer.
"""

import threading
import time

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)


_qa_runtime = TaskRuntime(
    'paper-qa', ttl=1800,
    push_channel='paper',
    error_source='routes.paper:qa',
)
# paper_hash → most-recent qa task_id (for reattach after refresh).
_qa_latest_index: dict[str, str] = {}
_qa_index_lock = threading.Lock()
_QA_TASK_TTL = 1800


def _qa_latest_for(phash: str) -> dict | None:
    """Return the most recent Q&A task for a paper hash, or None."""
    with _qa_index_lock:
        tid = _qa_latest_index.get(phash)
    if not tid:
        return None
    return _qa_runtime.get(tid)


def _qa_register_latest(phash: str, task_id: str) -> None:
    with _qa_index_lock:
        _qa_latest_index[phash] = task_id


def _new_qa_task(task_id, phash, lang, model, *, question='', client_title=''):
    """Create a fresh Q&A task. Registers it as the paper's latest."""
    task = _qa_runtime.create(
        task_id=task_id,
        meta={'paper_hash': phash, 'lang': lang, 'model': model},
    )
    task.update({
        'task_id': task_id,
        'paper_hash': phash,
        'lang': lang,
        'model': model,
        'question': question,
        'client_title': client_title,
        'status': 'pending',
        'finished_at': None,
        'full_text': '',
        'tool_rounds': [],
        'round_counter': 0,
    })
    _qa_register_latest(phash, task_id)
    return task


def _append_qa_event(task, event):
    """Append an event to the Q&A task's log (thread-safe; auto-pushes)."""
    _qa_runtime.append_event(task['task_id'], event)


def _cleanup_stale_qa_tasks():
    """Drop finished Q&A tasks older than TTL and prune the latest index."""
    finished_ids: set = set()
    with _qa_runtime._lock:
        for tid, t in _qa_runtime._tasks.items():
            if t['status'] in ('done', 'error', 'aborted') and t.get('finished_at'):
                if time.time() - t['finished_at'] > _QA_TASK_TTL:
                    finished_ids.add(tid)
    n = _qa_runtime.cleanup_stale()
    if n:
        with _qa_index_lock:
            stale = [k for k, tid in _qa_latest_index.items() if tid in finished_ids]
            for k in stale:
                _qa_latest_index.pop(k, None)
        logger.debug('[Paper:QA] Cleaned %d stale task(s)', n)


# Compatibility aliases (tests / introspection).
_qa_tasks = _qa_runtime._tasks       # type: ignore[attr-defined]
_qa_tasks_lock = _qa_runtime._lock   # type: ignore[attr-defined]
