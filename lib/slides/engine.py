"""lib/slides/engine.py — headless worker for slide-deck jobs.

Thin by design (same posture as lib/longform/engine.py): the recipe owns the
work, the production substrate owns the checkpointed resume, this file only
bridges a TaskRuntime task to them and shapes the result.
"""

from __future__ import annotations

import os

from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['run_slides_task', 'slides_root', 'resume_interrupted_decks',
           'start_slides_job']


def slides_root() -> str:
    """Writable root for slide-deck state (job workdirs)."""
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'slides')
    os.makedirs(path, exist_ok=True)
    return path


def _emit(task: dict, event: dict) -> None:
    from lib.slides.runtime import _append_slides_event
    try:
        _append_slides_event(task, event)
    except Exception as e:
        logger.debug('[Slides] emit failed: %s', e)


#: Task fields persisted so a crashed process can re-spawn this job.
_MANIFEST_FIELDS = ('task_id', 'topic', 'lang', 'style', 'max_pages', 'size',
                    'conv_id', 'workdir', 'model', 'user_id')


def _write_manifest(task: dict, state: str) -> None:
    from lib.production.jobs import write_manifest
    write_manifest(task.get('workdir') or '', task, fields=_MANIFEST_FIELDS,
                   kind='slides-deck', state=state, log_label='Slides')


def run_slides_task(task: dict) -> None:
    """Worker entry — topic → editable PPTX + preview grid."""
    from lib.slides.recipe import build_deck_from_topic
    from lib.slides.runtime import _slides_runtime

    task_id = task['task_id']
    try:
        _write_manifest(task, 'running')
        _slides_runtime.mark_running(task_id)
        _emit(task, build_phase(Phase.START, topic=task.get('topic', '')))
        result = build_deck_from_topic(
            task['topic'], task['workdir'], lang=task.get('lang') or 'zh',
            style=task.get('style') or '', size=task.get('size') or (1280, 720),
            max_pages=int(task.get('max_pages') or 12),
            model=task.get('model') or None,
            abort_event=task.get('abort_event'),
            emit=lambda ev: _emit(task, {'type': 'stage', **ev}))
        result['workdir'] = task['workdir']

        # Quality axis: a deck whose pages all degraded to fallbacks is a
        # structurally valid file out of a broken pipeline — status stays
        # 'done' by design; artifact_quality carries the truth.
        total = result.get('pages', 0)
        authored = result.get('authored_pages', 0)
        degraded = bool(total and authored < total)
        reason = ''
        if degraded:
            reason = (f'{total - authored} of {total} pages fell back to the '
                      'minimal layout')
        _write_manifest(task, 'degraded' if degraded else 'done')
        _emit(task, {'type': 'final', **result, 'degraded': degraded,
                     'degraded_reason': reason})
        _slides_runtime.finish(task_id, result=result, degraded=degraded,
                               degraded_reason=reason)
        logger.info('[Slides] %s %s — %d/%d pages authored, %d bytes',
                    task_id, 'degraded' if degraded else 'done',
                    authored, total, result.get('bytes', 0))
    except InterruptedError:
        logger.info('[Slides] task %s aborted', task_id)
        _write_manifest(task, 'aborted')
        _slides_runtime.finish(task_id, error='aborted',
                               error_context='slides:abort')
    except Exception as e:
        logger.error('[Slides] task %s failed: %s', task_id, e, exc_info=True)
        _write_manifest(task, 'error')
        _slides_runtime.finish(task_id, error=e,
                               error_context='slides:engine')


def resume_interrupted_decks() -> int:
    """Re-spawn deck jobs left ``running`` on disk by a crashed process."""
    from lib.production.jobs import resume_running_jobs
    from lib.slides.runtime import _new_slides_task, _slides_runtime

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        user_id = int(m['user_id'])
        if user_id < 1:
            raise ValueError('slides manifest has no valid owner')
        task = _new_slides_task(
            task_id, topic=m.get('topic') or '', workdir=workdir,
            lang=m.get('lang') or 'zh', style=m.get('style') or '',
            max_pages=int(m.get('max_pages') or 12),
            size=tuple(m.get('size') or (1280, 720)),
            conv_id=m.get('conv_id') or '', model=m.get('model') or '',
            user_id=user_id)
        _slides_runtime.spawn(task_id, run_slides_task, task)

    return resume_running_jobs(
        os.path.join(slides_root(), 'jobs'),
        is_live=lambda tid: _slides_runtime.get(tid) is not None,
        respawn=_respawn, log_label='Slides')


def start_slides_job(topic: str, *, lang: str = 'zh', style: str = '',
                     max_pages: int = 12, size=(1280, 720),
                     conv_id: str = '', model: str = '', user_id: int) -> dict:
    """Create + spawn a deck job; returns {task_id, deduped}."""
    from lib.slides.runtime import (
        _cleanup_stale_slides_tasks, _new_slides_task,
        _slides_index_get, _slides_index_register, _slides_runtime,
        _slides_task_id)

    _cleanup_stale_slides_tasks()
    key = (user_id, topic.strip(), lang, style.strip(), int(max_pages), tuple(size),
           model.strip())
    existing = _slides_index_get(key)
    if existing:
        return {'task_id': existing, 'deduped': True}
    tid = _slides_task_id()
    wd = os.path.join(slides_root(), 'jobs', tid)
    os.makedirs(wd, exist_ok=True)
    task = _new_slides_task(tid, topic=topic.strip(), workdir=wd, lang=lang,
                            style=style.strip(), max_pages=int(max_pages),
                            size=tuple(size), conv_id=conv_id,
                            model=model.strip(), user_id=user_id)
    _slides_index_register(key, tid)
    _slides_runtime.spawn(tid, run_slides_task, task)
    logger.info('[Slides] started %s topic=%r lang=%s pages=%d',
                tid, topic[:60], lang, max_pages)
    return {'task_id': tid, 'deduped': False}
