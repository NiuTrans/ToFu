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
        if task.get('_research_record_enabled'):
            task['_research_persistence_error'] = e
            task['abort_event'].set()
        logger.warning('[Research] progress persistence failed: %s', e)




#: Task fields persisted so a crashed process can re-spawn this job.
_MANIFEST_FIELDS = (
    'task_id', 'user_id', 'direction', 'lang', 'n_ideas', 'seed_arxiv_ids',
    'harvest_n', 'model', 'conv_id', 'workdir', 'evolution')


def _write_manifest(task: dict, state: str) -> None:
    from lib.production.jobs import write_manifest
    projection = {**task, 'evolution': (task.get('config') or {}).get('evolution')}
    write_manifest(task.get('workdir') or '', projection, fields=_MANIFEST_FIELDS,
                   kind='research', state=state, log_label='Research')
    if task.get('_research_record_enabled'):
        from lib.research.records import persist_record
        persist_record(task)
        if state in ('done', 'error', 'aborted', 'interrupted') and projection.get('evolution'):
            # Production TaskRuntime publishes its own terminal frame; it does
            # not pass through chat's terminal listener. Observe only after
            # the exact research record commits, using the existing receiver.
            try:
                from lib.evolution.automation import enqueue_task_terminal
                enqueue_task_terminal(task)
            except Exception:
                logger.exception('[Research] evolution terminal handoff failed for %s', task['task_id'])


def _prepare_model_budget(task):
    """Activate only an acknowledged policy; never create a recovered account."""
    if not task.get('_research_record_enabled'):
        return None
    from lib.research.budget_binding import read_model_budget
    policy = read_model_budget(task['task_id'], user_id=task['user_id'])
    if policy is None:
        return None
    if task.get('_research_recovered') and task.get('_research_recovery_claim') is None:
        from lib.storage.errors import StorageError
        raise StorageError('database_conflict',
            'Budgeted discovery requires explicit account recovery; fresh reset refused')
    from lib.research.discovery_owner import claim_discovery_execution
    from lib.research.model_attempt_journal import install_model_attempt_journal
    from lib.agent_core.execution_budget import model_budget_admission
    claim = task.get('_research_recovery_claim')
    if claim is None:
        claim = claim_discovery_execution(task['task_id'], user_id=task['user_id'])
    carrier = {'id': task['task_id'], '_userId': task['user_id'],
               'abort_event': task['abort_event'],
               'config': {'executionLimits': dict(policy['execution_limits'])}}
    install_model_attempt_journal(carrier, execution_claim=claim)
    task['_research_model_account'] = carrier
    return model_budget_admission(carrier, cancelled=task['abort_event'].is_set)


def run_research_task(task: dict) -> None:
    """Worker entry — direction → ideas, evidence map, and LLM review."""
    from lib.research.recipe import build_research_from_direction
    from lib.research.runtime import _research_runtime

    task_id = task['task_id']
    try:
        if task.get('_research_recovered'):
            from lib.research.records import resume_evolution_binding
            resume_evolution_binding(task.get('_durable_research_record'),
                task_id=task_id, user_id=task['user_id'],
                manifest_evolution=(task.get('config') or {}).get('evolution'))
        if not _research_runtime.mark_running(task_id):
            return
        if task['abort_event'].is_set():
            _research_runtime.finish(task_id)
            _write_manifest(task, task['status'])
            return
        _write_manifest(task, 'running')
        model_budget_admit = _prepare_model_budget(task)
        _emit(task, build_phase(Phase.START,
                                direction=task.get('direction', '')))
        from lib.llm_dispatch.conv_affinity import conv_affinity
        # Background production threads do not inherit the chat conversation's
        # dispatcher affinity. Give all rounds of this job their own bounded,
        # task-scoped key so a multi-key provider can reuse prompt prefixes.
        with conv_affinity(f'production-research:{task_id}'):
            result = build_research_from_direction(
                task['direction'], task['workdir'],
                lang=task.get('lang') or 'en',
                user_id=int(task['user_id']), n_ideas=task.get('n_ideas'),
                seed_arxiv_ids=task.get('seed_arxiv_ids'),
                harvest_n=task.get('harvest_n'),
                model=task.get('model') or None,
                abort_event=task.get('abort_event'),
                emit=lambda ev: _emit(task, {'type': 'stage', **ev}),
                **({'model_budget_admit': model_budget_admit}
                   if model_budget_admit is not None else {}))

        if task.get('_research_persistence_error'):
            raise task['_research_persistence_error']
        if task['abort_event'].is_set():
            _research_runtime.finish(task_id)
            _write_manifest(task, task['status'])
            return
        # A degraded run produced a VALID artifact from a SICK pipeline — it is
        # neither a clean success nor an error. Settling it as 'done' is what
        # made the R3 total-wipe bug invisible (state=done, accepted 0).
        _degraded = bool(result.get('degraded'))

        _emit(task, {'type': 'final', 'accepted': len(result.get('accepted', [])),
                     'rejected': len(result.get('rejected', [])),
                     'corpus_size': result.get('corpus_size', 0),
                     'gate_reached': result.get('gate_reached'),
                     'degraded': _degraded,
                     'degraded_reason': result.get('degraded_reason', '')})
        if task.get('_research_persistence_error'):
            raise task['_research_persistence_error']
        if task.get('_research_record_enabled'):
            from lib.research.records import persist_record
            persist_record(task, status='degraded' if _degraded else 'done', result=result)

        _research_runtime.finish(task_id, result=result, degraded=_degraded,
                                 degraded_reason=result.get('degraded_reason', ''))

        _write_manifest(task, task['status'])
        if _degraded:
            logger.error('[Research] %s DEGRADED — %s', task_id,
                         result.get('degraded_reason'))
        logger.info('[Research] %s %s — %d accepted / %d rejected (corpus %d, gate %s)',
                    task_id, 'degraded' if _degraded else 'done',
                    len(result.get('accepted', [])),
                    len(result.get('rejected', [])), result.get('corpus_size', 0),
                    result.get('gate_reached'))
    except Exception as e:
        from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
        if isinstance(e, ModelBudgetAdmissionDenied) and str(e) in (
                'execution_token_budget_exhausted', 'execution_deadline_exceeded',
                'execution_token_usage_unknown'):
            task['_executionLimitReason'] = str(e)
        logger.error('[Research] task %s failed: %s', task_id, e, exc_info=True)
        persistence_error = task.get('_research_persistence_error')
        if persistence_error is not None:
            # The run was sacrificed to a durable-record write failure, so the
            # record must name THAT cause — not whichever stage noticed the
            # abort flag first ("aborted in stage 'harvest'" told the user
            # nothing about the storage starvation that actually killed it).
            task['abort_event'].clear()
            _research_runtime.finish(task_id, error=persistence_error,
                                     error_context='research:persistence')
        elif task['abort_event'].is_set():
            _research_runtime.finish(task_id)
        else:
            _research_runtime.finish(task_id, error=e, error_context='research:engine')
        try:
            _write_manifest(task, task['status'])
        except Exception:
            logger.exception('[Research] failed to persist terminal record for %s', task_id)


def resume_interrupted_research() -> int:
    """Re-spawn research jobs left ``running`` on disk by a crashed process."""
    from lib.production.jobs import resume_running_jobs
    from lib.research.runtime import _new_research_task, _research_runtime

    def _respawn(task_id: str, workdir: str, m: dict) -> None:
        from lib.research.contracts import normalize_research_request
        user_id = int(m['user_id'])
        if user_id < 1:
            raise ValueError('research manifest has no valid owner')
        request = normalize_research_request(
            m.get('direction') or '', lang=m.get('lang') or 'en',
            n_ideas=m.get('n_ideas'),
            seed_arxiv_ids=m.get('seed_arxiv_ids'), model=m.get('model'))
        from lib.research.records import (ACTIVE, load_record, persist_record,
                                          resume_evolution_binding, restore_pipeline_checkpoint)
        record = load_record(task_id, user_id=user_id, internal=True)
        if record is not None and record.get('status') not in ACTIVE:
            raise ValueError('terminal research record cannot be automatically resumed')
        binding = resume_evolution_binding(record, task_id=task_id, user_id=user_id,
                                           manifest_evolution=m.get('evolution'))
        recovery_claim = None
        if record is not None:
            from lib.research.budget_binding import read_model_budget
            if read_model_budget(task_id, user_id=user_id) is not None:
                from lib.research.discovery_owner import recover_discovery_execution
                recovery_claim = recover_discovery_execution(task_id, user_id=user_id)
        restore_pipeline_checkpoint(record, workdir)
        task = _new_research_task(
            task_id, direction=request.direction, workdir=workdir,
            lang=request.lang, n_ideas=request.n_ideas,
            seed_arxiv_ids=request.seed_arxiv_ids,
            harvest_n=m.get('harvest_n'), model=request.model,
            conv_id=m.get('conv_id') or '', user_id=user_id, evolution=binding)
        task['_research_record_enabled'] = True
        task['_research_recovered'] = True
        if recovery_claim is not None:
            task['_research_recovery_claim'] = recovery_claim
        if record:
            task['_durable_research_record'] = record
            task['research_progress'] = record.get('research_progress')
        try:
            resume_evolution_binding(record, task_id=task_id, user_id=user_id,
                                     manifest_evolution=binding)
            persist_record(task)
            _research_runtime.spawn(task_id, run_research_task, task)
        except Exception as exc:
            _research_runtime.finish(task_id, error=exc, error_context='research:resume')
            raise

    return resume_running_jobs(
        os.path.join(research_root(), 'jobs'),
        is_live=lambda tid: _research_runtime.get(tid) is not None,
        respawn=_respawn, log_label='Research')


def produce_research(direction: str, *, lang: str = 'en', n_ideas: int = 6,
                     conv_id: str = '', seed_arxiv_ids=None, harvest_n=None,
                     model: str | None = None,
                     evolution: dict | None = None,
                     execution_limits: dict | None = None,
                     user_id: int) -> dict:
    """Create + spawn an auto-research job; return {task_id, deduped}.

    The single "one direction → scored ideas" entry point. Poll/abort go
    through the generic /api/v1/tasks/* surface (the runtime is discovered by
    kind='research') — no bespoke routes. ``evolution`` binds the task to an
    evolution run stage (``{run_id, goal_id, plan_id, stage}``) so the
    terminal listener settles the owning run from the research record.
    """
    from lib.research.runtime import (_claim_research_task,
                                      _cleanup_stale_research_tasks,
                                      _research_runtime, _research_task_id)
    from lib.identity import require_user_id
    from lib.research.contracts import (normalize_research_harvest_count,
                                        normalize_research_request)

    if execution_limits is not None:
        from .budget_binding import normalize_model_limits
        execution_limits = normalize_model_limits(execution_limits)
    _cleanup_stale_research_tasks()
    owner_user_id = require_user_id(user_id, context='produce research')
    request = normalize_research_request(
        direction, lang=lang, n_ideas=n_ideas,
        seed_arxiv_ids=seed_arxiv_ids, model=model)
    harvest_n = normalize_research_harvest_count(harvest_n)
    key = request.dedup_key(owner_user_id)
    if evolution is not None:
        from lib.research.runtime import normalize_evolution_binding
        evolution = normalize_evolution_binding(evolution)
        # Distinct Evolution runs cannot adopt each other's same-direction worker.
        key = (*key, evolution['run_id'])
    tid = _research_task_id()
    wd = os.path.join(research_root(), 'jobs', tid)
    task, existing = _claim_research_task(
        key, tid, direction=request.direction, workdir=wd,
        lang=request.lang, n_ideas=request.n_ideas,
        seed_arxiv_ids=request.seed_arxiv_ids, harvest_n=harvest_n,
        model=request.model, evolution=evolution,
        conv_id=conv_id, user_id=owner_user_id)
    if existing:
        if execution_limits is not None:
            from .budget_binding import read_model_budget
            policy = read_model_budget(existing, user_id=owner_user_id)
            if policy is None or policy['execution_limits'] != execution_limits:
                raise ValueError('existing research task has a different model budget')
        return {'task_id': existing, 'deduped': True}
    try:
        os.makedirs(wd, exist_ok=True)
        from lib.research.records import persist_record
        task['_research_record_enabled'] = True
        persist_record(task)
        if execution_limits is not None:
            from .budget_binding import bind_model_budget
            bind_model_budget(tid, user_id=owner_user_id, execution_limits=execution_limits)
        _research_runtime.spawn(tid, run_research_task, task)
    except Exception as exc:
        _research_runtime.finish(
            tid, error=exc, error_context='research:start')
        raise
    logger.info('[Research] started %s direction=%r lang=%s ideas=%d seeds=%d harvest=%d model=%s',
                tid, request.direction[:60], request.lang, request.n_ideas,
                len(request.seed_arxiv_ids), harvest_n, request.model or 'auto')
    return {'task_id': tid, 'deduped': False}
