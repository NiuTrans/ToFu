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

from lib.log import get_logger
from lib.agent_core.task_runtime import TaskRuntime
from lib.paper.request_policy import paper_request_policy_telemetry

logger = get_logger(__name__)


_qa_runtime = TaskRuntime(
    'paper-qa', ttl=1800,
    push_channel='paper',
    error_source='routes.paper:qa',
)
# (owner, paper_hash) → most-recent qa task_id (for reattach after refresh).
_qa_latest_index: dict[tuple[int, str], str] = {}
_qa_index_lock = threading.Lock()
_QA_TASK_TTL = 1800


def _qa_register_latest(phash: str, task_id: str, *, user_id: int) -> None:
    with _qa_index_lock:
        _qa_latest_index[(user_id, phash)] = task_id


def _new_qa_task(
    task_id, phash, lang, model, *, user_id: int, question='', client_title='',
    config=None,
):
    """Create a fresh Q&A task. Registers it as the paper's latest."""
    detached_config = dict(config or {})
    request_policy = paper_request_policy_telemetry(
        model=model, config=detached_config)
    task = _qa_runtime.create(
        user_id=user_id,
        task_id=task_id,
        meta={
            'paper_hash': phash,
            'lang': lang,
            'model': model,
            'execution_fingerprint': request_policy[
                'executionFingerprint'],
        },
    )
    _qa_runtime.update_fields(task_id, fields={
        'task_id': task_id,
        'paper_hash': phash,
        'lang': lang,
        'model': model,
        'question': question,
        'client_title': client_title,
        'config': detached_config,
        'execution_fingerprint': request_policy['executionFingerprint'],
        'requestPolicyV1': request_policy,
        'full_text': '',
        'tool_rounds': [],
        'round_counter': 0,
    })
    _qa_register_latest(phash, task_id, user_id=user_id)
    return task


def _append_qa_event(task, event):
    """Append an event to the Q&A task's log (thread-safe; auto-pushes)."""
    _qa_runtime.append_event(task['task_id'], event)


def _cleanup_stale_qa_tasks():
    """Drop finished Q&A tasks older than TTL and prune the latest index."""
    n = _qa_runtime.cleanup_stale()
    if n:
        live_task_ids = _qa_runtime.task_ids()
        with _qa_index_lock:
            stale = [
                key for key, task_id in _qa_latest_index.items()
                if task_id not in live_task_ids
            ]
            for k in stale:
                _qa_latest_index.pop(k, None)
        logger.debug('[Paper:QA] Cleaned %d stale task(s)', n)
