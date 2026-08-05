"""lib/longform/engine.py — headless worker for long-form report jobs (P7).

Thin: the recipe owns the work, the substrate owns the checkpointed resume,
this file only bridges a TaskRuntime task to them and publishes the result
as a markdown artifact.
"""

from __future__ import annotations

import os

from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['run_longform_task', 'longform_root', 'resume_interrupted_reports']


def longform_root() -> str:
    """Writable root for long-form report state (job workdirs)."""
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'longform')
    os.makedirs(path, exist_ok=True)
    return path


def _emit(task: dict, event: dict) -> None:
    from lib.longform.runtime import _append_longform_event
    try:
        _append_longform_event(task, event)
    except Exception as e:
        logger.debug('[Longform] emit failed: %s', e)


#: Task fields persisted so a crashed process can re-spawn this job.
_MANIFEST_FIELDS = ('task_id', 'topic', 'lang', 'depth', 'conv_id', 'workdir')


def _write_manifest(task: dict, state: str) -> None:
    """Persist job params so a crashed process can re-spawn this job."""
    from lib.production.jobs import write_manifest
    write_manifest(task.get('workdir') or '', task, fields=_MANIFEST_FIELDS,
                   kind='longform-report', state=state, log_label='Longform')


def run_longform_task(task: dict) -> None:
    """Worker entry — topic → research report markdown artifact."""
    from lib.longform.recipe import build_report_from_topic
    from lib.longform.runtime import _longform_runtime

    task_id = task['task_id']
    try:
        _write_manifest(task, 'running')
        task['status'] = 'running'
        _emit(task, build_phase(Phase.START,
                                topic=task.get('topic', '')))
        result = build_report_from_topic(
            task['topic'], task['workdir'], lang=task.get('lang') or 'zh',
            depth=task.get('depth') or 'standard',
            abort_event=task.get('abort_event'),
            emit=lambda ev: _emit(task, {'type': 'stage', **ev}))

        artifact_id = ''
        conv_id = task.get('conv_id') or ''
        if conv_id:
            try:
                from lib.artifacts.core import create_artifact
                with open(result['path'], encoding='utf-8') as f:
                    row = create_artifact(
                        conv_id=conv_id, content=f.read(), format='markdown',
                        source='longform-report', task_id=task_id,
                        title=result.get('title') or task['topic'],
                        source_ref={'topic': task['topic']},
                        meta={'sections': result.get('sections'),
                              'sources': result.get('sources')})
                artifact_id = row.get('id') or ''
            except Exception as e:
                logger.warning('[Longform] artifact publish failed: %s', e)

        result['artifact_id'] = artifact_id
        task['result'] = result
        # Quality axis: the report is readable, so status is legitimately
        # 'done'. But a section whose stage never produced an artifact is
        # silently skipped by _run_assemble, and an outline thinner than the
        # requested depth passes _gate_outline (which only demands >= 2) —
        # both ship a structurally-valid report that is missing content.
        _written = result.get('sections_written', result.get('sections', 0))
        _requested = result.get('sections_requested', 0)
        _missing = max(0, result.get('sections', 0) - _written)
        _thin = bool(_requested and _written < _requested)
        _degraded = bool(_missing or _thin)
        _reason = ''
        if _missing:
            _reason = (f'{_missing} of {result.get("sections", 0)} outlined '
                       'section(s) produced no text and were dropped from the '
                       'report')
        elif _thin:
            _reason = (f'report has {_written} section(s) but this depth asks '
                       f'for {_requested} — the outline came back thin')
        _write_manifest(task, 'degraded' if _degraded else 'done')
        _emit(task, {'type': 'final', **result, 'degraded': _degraded,
                     'degraded_reason': _reason})
        _longform_runtime.finish(task_id, result=result, degraded=_degraded,
                                 degraded_reason=_reason)
        if _degraded:
            logger.error('[Longform] %s DEGRADED — %s', task_id, _reason)
        logger.info('[Longform] %s %s — %d chars, %d/%d sections written',
                    task_id, 'degraded' if _degraded else 'done',
                    result.get('chars', 0), _written,
                    result.get('sections', 0))
    except Exception as e:
        logger.error('[Longform] task %s failed: %s', task_id, e, exc_info=True)
        _write_manifest(task, 'error')
        _longform_runtime.finish(task_id, error=e,
                                 error_context='longform:engine')


def resume_interrupted_reports() -> int:
    """Re-spawn report jobs left ``running`` on disk by a crashed process."""
    from lib.longform.runtime import _longform_runtime, _new_longform_task
    from lib.production.jobs import resume_running_jobs

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        task = _new_longform_task(
            task_id, topic=m.get('topic') or '', workdir=workdir,
            lang=m.get('lang') or 'zh', depth=m.get('depth') or 'standard',
            conv_id=m.get('conv_id') or '')
        _longform_runtime.spawn(task_id, run_longform_task, task)

    return resume_running_jobs(
        os.path.join(longform_root(), 'jobs'),
        is_live=lambda tid: _longform_runtime.get(tid) is not None,
        respawn=_respawn, log_label='Longform')


def start_report_job(topic: str, *, lang: str = 'zh', depth: str = 'standard',
                     conv_id: str = '') -> dict:
    """Create + spawn a report job; returns {task_id, deduped}."""
    from lib.longform.runtime import (
        _cleanup_stale_longform_tasks, _longform_index_get,
        _longform_index_register, _longform_runtime, _longform_task_id,
        _new_longform_task)

    _cleanup_stale_longform_tasks()
    key = (topic.strip(), lang, depth)
    existing = _longform_index_get(key)
    if existing:
        return {'task_id': existing, 'deduped': True}
    tid = _longform_task_id()
    wd = os.path.join(longform_root(), 'jobs', tid)
    os.makedirs(wd, exist_ok=True)
    task = _new_longform_task(tid, topic=topic.strip(), workdir=wd, lang=lang,
                              depth=depth, conv_id=conv_id)
    _longform_index_register(key, tid)
    _longform_runtime.spawn(tid, run_longform_task, task)
    logger.info('[Longform] started %s topic=%r lang=%s depth=%s',
                tid, topic[:60], lang, depth)
    return {'task_id': tid, 'deduped': False}
