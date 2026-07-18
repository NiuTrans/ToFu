"""Background worker for streaming describe-to-recommend.

Drives :func:`lib.paper.recommend_engine.iter_recommend_events` and mirrors each
yielded event into the task's append-only event log so the frontend can reveal
the interpretation agent's research activity + grounded cards one at a time
(aligned with the chatInner streaming aesthetic).

The interpretation step is agentic: it runs the SAME ``web_search`` /
``fetch_url`` tool loop the report/QA engines use (via ``_execute_report_tool``),
so — exactly like those workers — this runs safely in the TaskRuntime thread
pool (fetch_url can pull a PDF; PyMuPDF thread-safety is handled the same way
the report/QA tasks already handle it). The grounding path
(``search_arxiv`` / ``fetch_arxiv_title``) remains metadata-only.
"""

import time

from lib.log import get_logger

from .recommend_engine import iter_recommend_events
from .recommend_runtime import _append_recommend_event, _cleanup_stale_recommend_tasks

logger = get_logger(__name__)


def _run_recommend_task(task):
    """Background worker: stream the recommend pipeline into task events.

    Args:
        task: the recommend task dict (from ``_new_recommend_task``).
    """
    task['status'] = 'running'
    _append_recommend_event(task, {'type': 'status', 'status': 'running'})

    abort_event = task['abort_event']
    description = task.get('description', '')
    max_results = task.get('max_results', 6)
    t0 = time.time()

    # Forward the interpretation agent's research tool activity (web_search /
    # fetch_url) straight into the task event log so the frontend can show a
    # live "researching…" trail before the grounded cards land. Chat-compatible
    # event shape (tool_start / tool_done), same as the report/QA engines.
    def _on_tool_event(ev):
        _append_recommend_event(task, ev)

    try:
        for ev in iter_recommend_events(
                description, max_results, abort=abort_event.is_set,
                on_tool_event=_on_tool_event):
            etype = ev.get('type')
            if etype == 'candidate':
                task['results'].append(ev['card'])
            elif etype == 'correction':
                task['correction'] = ev['correction']
            _append_recommend_event(task, ev)
            if etype in ('done', 'error'):
                # The generator's terminal event; the loop naturally ends here.
                if etype == 'error':
                    task['status'] = 'error'
                    task['finished_at'] = time.time()
                    task['llmError'] = bool(ev.get('llmError'))
                    logger.info('[Paper:Recommend] Task %s errored (llmError=%s) after %.1fs',
                                task['task_id'], task.get('llmError'), time.time() - t0)
                    return

        task['status'] = 'done'
        task['finished_at'] = time.time()
        logger.info('[Paper:Recommend] Task %s done — %d card(s)%s in %.1fs',
                    task['task_id'], len(task['results']),
                    ' (+correction)' if task.get('correction') else '', time.time() - t0)

    except Exception as e:
        logger.error('[Paper:Recommend] Task %s failed after %.1fs: %s',
                     task['task_id'], time.time() - t0, e, exc_info=True)
        from lib.error_envelope import from_exception as _err_from_exc
        envelope = _err_from_exc(
            e, model='', context='paper-recommend', source='routes.paper:recommend')
        task['status'] = 'error'
        task['error'] = envelope
        task['finished_at'] = time.time()
        _append_recommend_event(task, {'type': 'error', 'error': envelope})
    finally:
        _cleanup_stale_recommend_tasks()
