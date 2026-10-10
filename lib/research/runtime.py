"""lib/research/runtime.py — TaskRuntime for auto-research jobs (R4).

Rides :class:`lib.production.runtime.ProductionRuntime` — the FOURTH capability
to do so (after motion-video / paper-podcast / longform-report). Per the owner
directive, this file builds NO bespoke runtime: dedup index, create-with-field-
shape, append+touch, stale sweep and id minting all come from the substrate.
It is a near-copy of longform/runtime.py by design — that similarity is the
substrate working, not duplication to refactor.

Events: ``stage`` (from the stage graph) / ``phase`` / ``final`` / ``done`` /
``error``.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.production.runtime import ProductionRuntime

logger = get_logger(__name__)

_production = ProductionRuntime(
    'research', id_prefix='research', ttl=7200,
    push_channel='research', error_source='lib.research.engine',
    log_label='Research')

#: The underlying TaskRuntime discovered by the generic task API.
_research_runtime = _production.runtime


def _research_index_get(key: tuple):
    return _production.index_get(key)


def _research_index_register(key: tuple, task_id: str) -> None:
    _production.index_register(key, task_id)


def normalize_evolution_binding(evolution):
    """Copy the server's research execution binding; never accept partial identity."""
    if evolution is None:
        return None
    keys = ('run_id', 'goal_id', 'plan_id', 'stage')
    if (not isinstance(evolution, dict) or set(evolution) != set(keys)
            or evolution.get('stage') != 'executing'
            or any(not isinstance(evolution.get(key), str)
                   or not evolution[key].strip() or len(evolution[key]) > 160 for key in keys)):
        raise ValueError('invalid research evolution binding')
    return dict(evolution)


def _new_research_task(task_id: str, *, direction: str, workdir: str, lang: str,
                       user_id: int, n_ideas: int = 6, conv_id: str = '',
                       seed_arxiv_ids=(), harvest_n=None, model: str = '', evolution=None):
    """Create + register a pending research task with the engine's field shape."""
    from lib.research.contracts import normalize_research_harvest_count
    seeds = list(seed_arxiv_ids or ())
    binding = normalize_evolution_binding(evolution)
    return _production.create_task(
        task_id,
        user_id=user_id, meta={'direction': direction, 'lang': lang,
                              'n_ideas': n_ideas, 'seed_count': len(seeds),
                              'model': model,
                              **({'evolution_run_id': binding['run_id']} if binding else {})},
        fields={'direction': direction, 'workdir': workdir, 'lang': lang,
                'n_ideas': n_ideas, 'conv_id': conv_id,
                'seed_arxiv_ids': seeds,
                'harvest_n': normalize_research_harvest_count(harvest_n),
                'model': model,
                **({'config': {'evolution': binding}} if binding else {})})


def _claim_research_task(key: tuple, task_id: str, *, direction: str,
                         workdir: str, lang: str, user_id: int, n_ideas: int = 6,
                         conv_id: str = '', seed_arxiv_ids=(), harvest_n=None,
                         model: str = '', evolution: dict | None = None):
    """Atomic start path; resume continues to use _new_research_task."""
    from lib.research.contracts import normalize_research_harvest_count
    seeds = list(seed_arxiv_ids or ())
    binding = normalize_evolution_binding(evolution)
    fields = {'direction': direction, 'workdir': workdir, 'lang': lang,
              'n_ideas': n_ideas, 'conv_id': conv_id,
              'seed_arxiv_ids': seeds,
              'harvest_n': normalize_research_harvest_count(harvest_n),
              'model': model}
    if binding:
        # The task-engine terminal listener keys off this binding to settle
        # the owning evolution run; it is inert for chat/page-started jobs.
        fields['config'] = {'evolution': binding}
    return _production.claim_task(
        key, task_id, user_id=user_id,
        meta={'direction': direction, 'lang': lang, 'n_ideas': n_ideas,
              'seed_count': len(seeds), 'model': model,
              **({'evolution_run_id': binding['run_id']} if binding else {})},
        fields=fields)


def _append_research_event(task, event):
    from lib.research.progress import project_progress
    # Presentation never revives a settled task or turns Stop into completion.
    if (task.get('status') not in ('pending', 'running')
            or task.get('_executionTerminalizing')
            or (task.get('abort_event') and task['abort_event'].is_set())):
        return None
    import time
    progress = project_progress(task.get('research_progress'), event,
                                run_id=task['task_id'], now_ms=int(time.time() * 1000))
    if task.get('_research_record_enabled'):
        from lib.research.records import persist_record
        # Persist semantic receipts, not token deltas or heartbeat traffic.
        if event.get('type') in ('paper_done', 'literature_discovered', 'corpus_admitted', 'search_round',
                                  'stage_started', 'stage_done', 'stage_skipped',
                                  'stage_failed', 'survey_map_failed', 'stage_checkpoint_invalidated', 'final'):
            task['research_progress'] = progress
            record = persist_record(task, event=event)
            if event.get('type') == 'literature_discovered':
                event = {**event, **{key: record[key] for key in ('papers', 'citation_edges', 'source_statuses')}}
            elif event.get('type') == 'survey_map_failed':
                event = {**event, 'survey_batch_failures': record['survey_batch_failures']}
    seq = _production.append_event(task, {**event, 'research_progress': progress})
    if seq is not None:
        _research_runtime.update_fields(task['task_id'], fields={'research_progress': progress},
                                        only_if_status=('pending', 'running'))
    return seq


def _cleanup_stale_research_tasks():
    return _production.cleanup_stale()


def _research_task_id():
    return _production.new_task_id()


__all__ = [
    '_production', '_research_runtime', '_research_index_get',
    '_research_index_register', '_new_research_task',
    '_claim_research_task',
    '_append_research_event', '_cleanup_stale_research_tasks',
    '_research_task_id',
]
