"""Synchronous task wrapper + progress reporting for non-streaming consumers.

Extracted from the monolithic ``lib/tasks_pkg/endpoint.py``.  Houses
``run_task_sync`` — the entry point for the Feishu bot / scheduled tasks /
API consumers that just need the final answer text — plus its progress
helpers ``_format_progress_event`` and ``_drain_progress`` (both exercised
directly by ``tests/test_run_task_sync_progress.py``).

Dependency direction: leaf module — imports only ``agent_core.events`` and
``tasks_pkg.manager`` / ``tasks_pkg.orchestrator``; does not touch the
endpoint loop, so no cycle.
"""

import threading

from lib.ids import short_id
from lib.log import get_logger

logger = get_logger(__name__)

from lib.agent_core.events import EventType
from lib.tasks_pkg.manager import create_task
from lib.tasks_pkg.orchestrator import run_task


# ══════════════════════════════════════════════════════════
#  run_task_sync — synchronous wrapper for Feishu/API consumers
# ══════════════════════════════════════════════════════════
def _format_progress_event(ev: dict) -> str:
    """Render a task event into a one-line progress string for a non-streaming
    consumer (Feishu bot), or '' when the event is not progress-worthy.

    Only ``tool_start`` is surfaced — it's the glanceable "the bot is doing
    something" signal. Everything else (deltas, usage, snapshots) is noise for
    a chat-bot progress ping.
    """
    if not isinstance(ev, dict):
        return ''
    if ev.get('type') == EventType.TOOL_START:
        name = ev.get('toolName') or 'tool'
        query = (ev.get('query') or '').strip()
        return f'Running {name}: {query}' if query else f'Running {name}…'
    return ''


def _drain_progress(task: dict, cursor: int, progress_fn) -> int:
    """Forward any task events appended since ``cursor`` to ``progress_fn``.

    Returns the new cursor. Errors raised by ``progress_fn`` are swallowed
    (logged at debug) — progress reporting must never break the task.
    """
    try:
        from lib.task_replay import task_memory_replay_page
        page = task_memory_replay_page(task, cursor)
        new = page.events
    except Exception as e:
        logger.debug('[run_task_sync] progress drain failed (ignored): %s', e)
        return cursor
    advanced = page.next_cursor
    for ev in new:
        line = _format_progress_event(ev)
        if not line:
            continue
        try:
            progress_fn(line)
        except Exception as e:
            logger.debug('[run_task_sync] progress_fn raised (ignored): %s', e)
    return advanced


def run_task_sync(config: dict, *, timeout: float = 600, progress_fn=None) -> str:
    """Run a task synchronously and return the final content string.

    This is the entry point for non-streaming consumers (Feishu bot,
    scheduled tasks, etc.) that just need the final answer text.

    Spawns ``run_task`` in a dedicated daemon thread (matching the web-UI
    pattern) and waits for completion via ``threading.Event``.

    Parameters
    ----------
    config : dict
        Task config dict with 'model', 'messages', and optional tool settings.
    timeout : float
        Maximum seconds to wait (default 600 = 10 min).
    progress_fn : callable, optional
        Called with a one-line progress string each time a long-running tool
        starts (e.g. ``"Running web_search: latest news"``). Lets a
        non-streaming consumer stream intermediate progress while the task
        runs. Exceptions from the callback are swallowed (logged at debug).

    Returns
    -------
    str
        The assistant's final response text, or an error message.
    """
    cfg = dict(config)
    conv_id = cfg.pop('conversationId', short_id('sync-', 8))
    messages = cfg.pop('messages', [])

    task = create_task(conv_id, messages, cfg)
    done_event = threading.Event()
    result_box: list = []

    def _worker():
        try:
            run_task(task)
        except Exception as exc:
            logger.error('[run_task_sync] Task %s failed: %s',
                         task['id'][:8], exc, exc_info=True)
            from lib.error_envelope import from_exception as _err_from_exc
            task['error'] = _err_from_exc(
                exc, model=cfg.get('model', ''),
                context='run_task_sync', source='endpoint-sync',
            )
            task['status'] = 'error'
        finally:
            with task['content_lock']:
                result_box.append(task.get('content', ''))
            done_event.set()

    worker = threading.Thread(target=_worker, daemon=True,
                              name=f'run_task_sync-{task["id"][:8]}')
    worker.start()

    if progress_fn is None:
        finished = done_event.wait(timeout=timeout)
    else:
        # Poll for new events while waiting so a non-streaming consumer can
        # forward live tool-start progress. ``done_event.wait(0.5)`` returns
        # True the instant the worker finishes, so this adds at most ~0.5s of
        # post-completion latency and zero overhead once done.
        import time as _time
        _deadline = _time.monotonic() + timeout
        _cursor = 0
        finished = False
        while True:
            if done_event.wait(timeout=0.5):
                _cursor = _drain_progress(task, _cursor, progress_fn)
                finished = True
                break
            _cursor = _drain_progress(task, _cursor, progress_fn)
            if _time.monotonic() >= _deadline:
                break

    if not finished:
        task['aborted'] = True
        logger.error('[run_task_sync] Task %s timed out after %.0fs',
                     task['id'][:8], timeout)
        return f'Task timed out after {timeout:.0f}s'

    content = result_box[0] if result_box else task.get('content', '')
    _err = task.get('error')
    if _err:
        _err_summary = _err.get('detail') or _err.get('message') or _err.get('raw') if isinstance(_err, dict) else str(_err)
        logger.warning('[run_task_sync] Task %s completed with error: %s',
                       task['id'][:8], _err_summary)
    return content or ''
