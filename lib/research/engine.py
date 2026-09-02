"""lib/research/engine.py — headless worker for auto-research jobs (R4).

Thin bridge: the recipe owns harvest→survey→ideate→independent LLM evaluation,
the substrate owns checkpointed resume + the crash-resume manifest, this
file only connects a TaskRuntime task to them and exposes the ``produce_research``
entry point.
"""

from __future__ import annotations

import os

from lib.agent_core.events import Phase, build_phase
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['run_research_task', 'research_root', 'resume_interrupted_research',
           'produce_research']


def research_root() -> str:
    """Writable root for auto-research job state (workdirs)."""
    from lib.runtime_paths import data_root
    path = os.path.join(data_root(), 'research')
    os.makedirs(path, exist_ok=True)
    return path


def _emit(task: dict, event: dict) -> None:
    from lib.research.runtime import _append_research_event
    try:
        _append_research_event(task, event)
    except Exception as e:
        logger.debug('[Research] emit failed: %s', e)


#: Task fields persisted so a crashed process can re-spawn this job.
_MANIFEST_FIELDS = (
    'task_id', 'user_id', 'direction', 'lang', 'n_ideas', 'conv_id', 'workdir')


def _write_manifest(task: dict, state: str) -> None:
    from lib.production.jobs import write_manifest
    write_manifest(task.get('workdir') or '', task, fields=_MANIFEST_FIELDS,
                   kind='research', state=state, log_label='Research')


def run_research_task(task: dict) -> None:
    """Worker entry — direction → ideas, evidence map, and LLM review."""
    from lib.research.recipe import build_research_from_direction
    from lib.research.runtime import _research_runtime

    task_id = task['task_id']
    try:
        _write_manifest(task, 'running')
        _research_runtime.mark_running(task_id)
        _emit(task, build_phase(Phase.START,
                                direction=task.get('direction', '')))
        result = build_research_from_direction(
            task['direction'], task['workdir'], lang=task.get('lang') or 'en',
            user_id=int(task['user_id']), n_ideas=task.get('n_ideas') or 6,
            seed_arxiv_ids=task.get('seed_arxiv_ids'),
            abort_event=task.get('abort_event'),
            emit=lambda ev: _emit(task, {'type': 'stage', **ev}))

        task['result'] = result
        # A degraded run produced a VALID artifact from a SICK pipeline — it is
        # neither a clean success nor an error. Settling it as 'done' is what
        # made the R3 total-wipe bug invisible (state=done, accepted 0).
        _degraded = bool(result.get('degraded'))
        _write_manifest(task, 'degraded' if _degraded else 'done')
        _emit(task, {'type': 'final', 'accepted': len(result.get('accepted', [])),
                     'rejected': len(result.get('rejected', [])),
                     'corpus_size': result.get('corpus_size', 0),
                     'gate_reached': result.get('gate_reached'),
                     'degraded': _degraded,
                     'degraded_reason': result.get('degraded_reason', '')})
        _research_runtime.finish(task_id, result=result, degraded=_degraded,
                                 degraded_reason=result.get('degraded_reason', ''))
        if _degraded:
            logger.error('[Research] %s DEGRADED — %s', task_id,
                         result.get('degraded_reason'))
        logger.info('[Research] %s %s — %d accepted / %d rejected (corpus %d, gate %s)',
                    task_id, 'degraded' if _degraded else 'done',
                    len(result.get('accepted', [])),
                    len(result.get('rejected', [])), result.get('corpus_size', 0),
                    result.get('gate_reached'))
    except Exception as e:
        logger.error('[Research] task %s failed: %s', task_id, e, exc_info=True)
        _write_manifest(task, 'error')
        _research_runtime.finish(task_id, error=e, error_context='research:engine')


def resume_interrupted_research() -> int:
    """Re-spawn research jobs left ``running`` on disk by a crashed process."""
    from lib.production.jobs import resume_running_jobs
    from lib.research.runtime import _new_research_task, _research_runtime

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        user_id = int(m['user_id'])
        if user_id < 1:
            raise ValueError('research manifest has no valid owner')
        task = _new_research_task(
            task_id, direction=m.get('direction') or '', workdir=workdir,
            lang=m.get('lang') or 'en', n_ideas=m.get('n_ideas') or 6,
            conv_id=m.get('conv_id') or '', user_id=user_id)
        _research_runtime.spawn(task_id, run_research_task, task)

    return resume_running_jobs(
        os.path.join(research_root(), 'jobs'),
        is_live=lambda tid: _research_runtime.get(tid) is not None,
        respawn=_respawn, log_label='Research')


def produce_research(direction: str, *, lang: str = 'en', n_ideas: int = 6,
                     conv_id: str = '', seed_arxiv_ids=None,
                     user_id: int) -> dict:
    """Create + spawn an auto-research job; return {task_id, deduped}.

    The single "one direction → scored ideas" entry point. Poll/abort go
    through the generic /api/v1/tasks/* surface (the runtime is discovered by
    kind='research') — no bespoke routes.
    """
    from lib.research.runtime import (_claim_research_task,
                                      _cleanup_stale_research_tasks,
                                      _research_runtime, _research_task_id)

    _cleanup_stale_research_tasks()
    key = (user_id, direction.strip(), lang)
    tid = _research_task_id()
    wd = os.path.join(research_root(), 'jobs', tid)
    task, existing = _claim_research_task(
        key, tid, direction=direction.strip(), workdir=wd,
        lang=lang, n_ideas=n_ideas, conv_id=conv_id, user_id=user_id)
    if existing:
        return {'task_id': existing, 'deduped': True}
    os.makedirs(wd, exist_ok=True)
    if seed_arxiv_ids:
        task['seed_arxiv_ids'] = list(seed_arxiv_ids)
    _research_runtime.spawn(tid, run_research_task, task)
    logger.info('[Research] started %s direction=%r lang=%s', tid, direction[:60], lang)
    return {'task_id': tid, 'deduped': False}
