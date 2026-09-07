"""Background worker for streaming describe-to-recommend.

Drives :func:`lib.paper.recommend_engine.iter_recommend_events` and mirrors each
yielded event into the task's append-only event log so the frontend can reveal
the interpretation agent's research activity + grounded cards one at a time
(aligned with the chatInner streaming aesthetic).

The interpretation step is agentic: it runs the SAME ``web_search`` /
``fetch_url`` tool loop the report/QA engines use (via ``execute_paper_tool``),
so — exactly like those workers — this runs safely in the TaskRuntime thread
pool (fetch_url can pull a PDF; PyMuPDF thread-safety is handled the same way
the report/QA tasks already handle it). The grounding path
(``search_arxiv`` / ``fetch_arxiv_title``) remains metadata-only.
"""

import time

from lib.log import get_logger

from .recommend_engine._events import iter_recommend_events
from .recommend_runtime import (
    _append_recommend_event,
    _cleanup_stale_recommend_tasks,
    _recommend_runtime,
)

logger = get_logger(__name__)


def _run_recommend_task(task):
    """Background worker: stream the recommend pipeline into task events.

    Args:
        task: the recommend task dict (from ``_new_recommend_task``).
    """
    task_id = task['task_id']
    _recommend_runtime.mark_running(task_id)
    _append_recommend_event(task, {'type': 'status', 'status': 'running'})

    abort_event = task['abort_event']
    description = task.get('description', '')
    max_results = task.get('max_results', 6)
    t0 = time.time()
    results = []
    correction = None
    terminal_event = None

    # Forward the interpretation agent's research tool activity (web_search /
    # fetch_url) straight into the task event log so the frontend can show a
    # live "researching…" trail before the grounded cards land. Chat-compatible
    # event shape (tool_start / tool_done), same as the report/QA engines.
    def _on_tool_event(ev):
        _append_recommend_event(task, ev)

    try:
        for ev in iter_recommend_events(
                description, max_results, abort=abort_event.is_set,
                on_tool_event=_on_tool_event,
                user_id=task.get('_userId')):
            etype = ev.get('type')
            if etype == 'candidate':
                results.append(ev['card'])
                _recommend_runtime.update_fields(
                    task_id, fields={'results': list(results)},
                    only_if_status='running')
            elif etype == 'correction':
                correction = ev['correction']
                _recommend_runtime.update_fields(
                    task_id, fields={'correction': correction},
                    only_if_status='running')
            if etype in ('done', 'error'):
                terminal_event = ev
                if etype == 'error':
                    llm_error = bool(ev.get('llmError'))
                    _recommend_runtime.update_fields(
                        task_id, fields={'llmError': llm_error},
                        only_if_status='running')
                    _recommend_runtime.finish(
                        task_id,
                        error=ev.get('error') or 'recommendation failed',
                        error_context='paper-recommend',
                        terminal_event_fields=ev,
                    )
                    logger.info('[Paper:Recommend] Task %s errored (llmError=%s) after %.1fs',
                                task_id, llm_error, time.time() - t0)
                    return
                break
            _append_recommend_event(task, ev)

        _recommend_runtime.finish(
            task_id,
            result={'results': results, 'correction': correction},
            terminal_event_fields=terminal_event or {'type': 'done'},
        )
        logger.info('[Paper:Recommend] Task %s done — %d card(s)%s in %.1fs',
                    task_id, len(results),
                    ' (+correction)' if correction else '', time.time() - t0)

    except Exception as e:
        logger.error('[Paper:Recommend] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-recommend', source='routes.paper:recommend')
        _recommend_runtime.finish(
            task_id,
            error=envelope,
            error_context='paper-recommend',
        )
    finally:
        _cleanup_stale_recommend_tasks()
