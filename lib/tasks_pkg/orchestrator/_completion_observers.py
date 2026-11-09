"""Best-effort terminal experiment metadata, metrics and completion logging.

Entry observe_completion runs after cost projection and before terminal
publication. Each observer retains its independent failure boundary; none
commits a Turn, changes its outcome or releases execution resources.
"""
import time

from lib.conversation_sync.settled_rows import is_settled, settle
from lib.cost import normalize_usage
from lib.log import get_logger, task_log_tag

logger = get_logger(__name__)


def observe_completion(task, done_event, *, model, finish_reason):
    # Same real-cost outcome sent on the live terminal event. The durable copy
    # is carried through the same terminal writer; observer failure never
    # prevents that authoritative handoff.
    try:
        from lib.cost_experiments.cost_experiments import build_cost_experiment_outcome
        _outcome = build_cost_experiment_outcome(
            task.get('_costExperiment'),
            usage=done_event.get('usage'),
            cost=done_event.get('cost'),
            api_rounds=done_event.get('apiRounds'),
            finish_reason=(done_event.get('finishReason')
                           or task.get('finishReason')),
            error=done_event.get('error') or task.get('error'),
            elapsed_ms=(time.time() - task.get('created_at', time.time())) * 1000,
            compactions=task.get('_costExperimentCompactions', 0),
            model=done_event.get('model') or task.get('model') or '',
            provider_id=task.get('provider_id') or '',
            task=task,
        )
        if _outcome:
            done_event['costExperiment'] = _outcome
            task['costExperiment'] = _outcome
            _round_tag = {
                'experiment_id': _outcome.get('experiment_id'),
                'arm': _outcome.get('arm'),
            }
            _api_rounds = done_event.get('apiRounds') or []
            for _index, _rd in enumerate(_api_rounds):
                if isinstance(_rd, dict) and _outcome.get('arm'):
                    if not is_settled(_rd):
                        _rd.setdefault('costExperiment', _round_tag)
                    elif 'costExperiment' not in _rd:
                        # A priced round is settled: tag it copy-on-write in
                        # the ledger list it shares with the task.
                        _api_rounds[_index] = settle(
                            {**_rd, 'costExperiment': _round_tag})
    except Exception as _xe:
        logger.warning('[CostExperiment] done-event outcome failed '
                       '(non-fatal): %s', _xe, exc_info=True)

    # Terminal SLO sample. Correlation ids stay in events/logs only; the
    # Prometheus dimensions are deliberately limited to provider/model/outcome.
    try:
        from lib.observability import record_llm_task
        _metric_error = done_event.get('error') or task.get('error')
        _metric_code = (_metric_error or {}).get('code') \
            if isinstance(_metric_error, dict) else ''
        if 'budget' in str(_metric_code):
            _metric_outcome = 'budget'
        elif task.get('aborted') or finish_reason == 'aborted':
            _metric_outcome = 'aborted'
        elif _metric_error:
            _metric_outcome = 'error'
        else:
            _metric_outcome = 'success'
        record_llm_task(
            done_event.get('model') or task.get('model') or '',
            task.get('provider_id') or '',
            time.time() - task.get('created_at', time.time()),
            len(done_event.get('apiRounds') or []),
            done_event.get('usage') or {},
            float((done_event.get('cost') or {}).get('costUsd') or 0.0),
            _metric_outcome,
        )
    except Exception as _metric_err:
        logger.debug('[Metrics] terminal LLM sample skipped: %s', _metric_err)

    # Comprehensive task-completion summary — keyed on the FULL task id so a
    #   user who quotes the id from the cost popover can grep ONE line that
    #   spans the whole turn (all tool rounds). Includes the per-round cache
    #   miss count so "why did cache break" is answerable straight from the
    #   log without re-deriving it. INFO level → lands in logs/app.log.
    try:
        _rounds = done_event.get('apiRounds') or []
        _miss_rounds = [r.get('round') for r in _rounds
                        if isinstance(r, dict) and r.get('cacheBreak')]
        _nu = normalize_usage(done_event.get('usage') or {})
        _cw = _nu['cache_write']
        _cr = _nu['cache_read']
        _cost = (done_event.get('cost') or {}).get('costCny')
        logger.info(
            '[Task:%s] ■ DONE conv=%s model=%s rounds=%d finish=%s '
            'cache_write=%d cache_read=%d cost=%s elapsed=%.1fs%s',
            task['id'], task.get('convId', '') or '-', model, len(_rounds),
            finish_reason or '-', _cw, _cr,
            (f'\u00a5{_cost:.3f}' if isinstance(_cost, (int, float)) else '?'),
            time.time() - task.get('created_at', time.time()),
            (f' \u26a0 CACHE_MISS rounds={_miss_rounds}' if _miss_rounds else ''),
        )
    except Exception as _se:
        logger.debug('[Task:%s] completion summary log failed: %s',
                     task_log_tag(task['id']), _se)

