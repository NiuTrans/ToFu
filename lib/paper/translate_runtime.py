"""Paper-translate task store — server-owned Babel-mode translation.

Mirrors the report task design: keyed by (paper_hash, lang), one running
task per pair, append-only events, persisted to ``paper_translations`` on
completion. The frontend just polls — chunking, SSE parsing, retry, and
cache management all live in the worker.
"""

import threading

from lib.log import get_logger
from lib.agent_core.task_runtime import TaskRuntime

logger = get_logger(__name__)


_translate_runtime = TaskRuntime(
    'paper-translate', ttl=3600,
    push_channel='paper-translate',
    error_source='routes.paper:translate',
)
_translate_dedup_index: dict[tuple, str] = {}
_translate_dedup_lock = threading.Lock()
_TRANSLATE_TASK_TTL = 3600

_TRANSLATE_CHUNK_SIZE = 2400  # chars per LLM call — tuned for context use

_LANG_NAMES = {
    'zh': 'Chinese', 'en': 'English', 'ja': 'Japanese',
    'ko': 'Korean', 'fr': 'French', 'de': 'German', 'es': 'Spanish',
}


def _translate_index_get(
    phash: str, lang: str, *, user_id: int,
) -> dict | None:
    """Find a paper-translate task by (paper_hash, lang)."""
    with _translate_dedup_lock:
        tid = _translate_dedup_index.get((user_id, phash, lang))
    if not tid:
        return None
    return _translate_runtime.get_owned(tid, user_id=user_id)


def _translate_index_register(
    phash: str, lang: str, task_id: str, *, user_id: int,
) -> None:
    with _translate_dedup_lock:
        _translate_dedup_index[(user_id, phash, lang)] = task_id


def _new_translate_task(task_id, phash, lang, model, *, user_id: int):
    """Create a fresh paper-translate task. Registers in the dedup index."""
    task = _translate_runtime.create(
        user_id=user_id,
        task_id=task_id,
        meta={'paper_hash': phash, 'lang': lang, 'model': model},
    )
    _translate_runtime.update_fields(task_id, fields={
        'task_id': task_id,
        'paper_hash': phash,
        'lang': lang,
        'model': model,
        'full_text': '',
        'progress': {'done': 0, 'total': 0},
    })
    _translate_index_register(phash, lang, task_id, user_id=user_id)
    return task


def _append_translate_event(task, event):
    """Thread-safe append (delegates to TaskRuntime, includes WS push)."""
    _translate_runtime.append_event(task['task_id'], event)


def _cleanup_stale_translate_tasks():
    """Drop finished tasks past TTL and remove their dedup entries."""
    n = _translate_runtime.cleanup_stale()
    if n:
        live_task_ids = _translate_runtime.task_ids()
        with _translate_dedup_lock:
            stale_keys = [k for k, tid in _translate_dedup_index.items()
                          if tid not in live_task_ids]
            for k in stale_keys:
                _translate_dedup_index.pop(k, None)
