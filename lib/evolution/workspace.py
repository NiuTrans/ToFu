"""Read-only studio projections over owner routing and task-engine state.

Entry points: model_options, pending_questions. No credentials, task transcripts,
resident caches or new polling workers cross this presentation boundary.
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

def model_options(*, user_id):
    from lib.model_routing import ModelRoutingRepository, OwnerBoundary
    from lib.model_routing.controls import reasoning_controls
    document = ModelRoutingRepository().get(OwnerBoundary.create(user_id)).document
    ranks = {(m['creator_id'], m['model_id']): m.get('quality_rank', 0) for m in document['models']}
    names = {(m['creator_id'], m['model_id']): m['display_name'] for m in document['models']}
    options = []
    for row in reasoning_controls(document):
        if row['availability'] != 'available':
            continue
        if row['creator_id']:
            if row['provider_id'] or row['offering_id']:
                continue
            model = {key: row[key] for key in ('creator_id', 'model_id')}
        else:
            model = {key: row[key] for key in ('provider_id', 'offering_id')}
        options.append({'model': model, 'label': names.get(
            (row['creator_id'], row['model_id']), row['model_id'])})
        if len(options) >= 128:
            break
    return sorted(options, key=lambda row: (-ranks.get((row['model'].get('creator_id'), row['model'].get('model_id')), 0), row['label']))


def pending_questions(run, *, user_id):
    from .bridge import default_task_loader
    if run.get('status') != 'executing' or not run.get('task_id'):
        return []
    task = default_task_loader(run['task_id'], user_id=user_id)
    if not task:
        return []
    questions = []
    for entry in task.get('toolRounds') or []:
        if entry.get('status') != 'awaiting_human' or not entry.get('guidanceId'):
            continue
        batch = entry.get('guidanceBatchQuestions') or []
        # The whole batch is presented; no hidden truncation of an authorization.
        questions.append({'guidance_id': entry['guidanceId'],
                          'question': entry.get('guidanceQuestion') or '',
                          'response_type': entry.get('guidanceType') or 'free_text',
                          'options': entry.get('guidanceOptions') or [],
                          'questions': batch})
        if len(questions) >= 8:
            break
    return questions


def resolve_run_model(requested, *, user_id):
    """Pin the default owner-selectable model inherited by unconfigured roles.

    Auto picks the strongest declared eligible model. No global user default,
    secret lookup, probe traffic or model switch between repair attempts.
    """
    if requested:
        return dict(requested)  # create_task is the execution eligibility authority
    choices = model_options(user_id=user_id)
    if not choices:
        from .contracts import EvolutionStateError
        raise EvolutionStateError('No available model route; connect a model in Settings')
    return dict(choices[0]['model'])


def run_summaries(*, user_id):
    """List navigation metadata only; full results belong to the detail read.

    50 rows × 180 objective characters, no transcripts, reviews or learning data.
    This bounds browser transport independently of the durable artifact size.
    """
    from .runs import list_run_snapshots
    fields = ('run_id', 'status', 'task_id', 'attempt', 'created_at', 'updated_at')
    return [{**{key: row[key] for key in fields}, 'objective': row['objective'][:180]}
            for row in list_run_snapshots(user_id=user_id, limit=50)]


_FLOOR_LIMIT = 16
_MS_EPOCH_FLOOR = 100_000_000_000  # 1973-03-03 in ms; any larger value is ms


def _epoch_seconds(value) -> int:
    """Normalize mixed-precision store timestamps to seconds for sorting.

    Evolution runs stamp ``int(time.time())`` while tofu-db run rows stamp
    ``now_ms``; without normalization millisecond rows always outrank
    second rows and the unified strip mis-interleaves by decades.
    """
    stamp = int(value or 0)
    return stamp // 1000 if stamp > _MS_EPOCH_FLOOR else stamp


def _orchestration_run_rows(user_id):
    """Late-bound durable-run read; importable/replaceable for focused tests."""
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    return OrchestrationRunService(
        SidecarOrchestrationRunStore(user_id)).list(limit=50)


def floor_activity(rows, *, user_id, orchestration_lister=None):
    """Unified live-work projection for the office floor (read-only, bounded).

    ``rows`` is the already-fetched evolution run list (one store read shared
    with the summaries panel). Durable orchestration runs join the same strip:
    goal-mode rows are detected through the canonical ``chat-goal:``
    orchestration id and carry their conversation identity; studio-launched
    durable runs stay ``flow``. The orchestration read is fail-soft — a storage
    hiccup degrades the strip to evolution-only with an explicit flag instead
    of failing the whole panel.
    """
    from .runs import TERMINAL
    items = []
    for row in rows:
        status = str(row.get('status') or '')
        if status in TERMINAL:
            continue
        items.append({'kind': 'evolution', 'run_id': row['run_id'],
                      'title': str(row.get('objective') or '')[:180],
                      'status': status,
                      'updated_at': _epoch_seconds(row.get('updated_at')),
                      'conversation_id': ''})
    degraded = False
    try:
        lister = orchestration_lister or _orchestration_run_rows
        orchestration_rows = lister(user_id) or []
    except Exception as _e:
        logger.debug('read floor activity failed: %s', _e)
        orchestration_rows, degraded = [], True
    if not degraded:
        from lib.goal_runs.contract import GOAL_RUN_ORCHESTRATION_PREFIX
        from lib.orchestration.run_status import is_terminal_run_status
        for row in orchestration_rows:
            status = str(row.get('status') or '')
            if is_terminal_run_status(status):
                continue
            orch_id = str(row.get('orch_id') or '')
            goal = orch_id.startswith(GOAL_RUN_ORCHESTRATION_PREFIX)
            run_id = str(row.get('id') or '')
            items.append({
                'kind': 'goal' if goal else 'flow',
                'run_id': run_id,
                'title': (str(row.get('name') or '')[:180] or run_id),
                'status': status,
                'updated_at': _epoch_seconds(row.get('updated_at')),
                'conversation_id': (
                    orch_id[len(GOAL_RUN_ORCHESTRATION_PREFIX):] if goal else ''),
            })
    items.sort(key=lambda item: -(item['updated_at'] or 0))
    return {'items': items[:_FLOOR_LIMIT], 'degraded': degraded}


def gate_pending(*, user_id, mutations_lister=None):
    """Bounded office projection of gate decisions awaiting the owner.

    Only ``proposed``/``confirmed`` mutations cross this boundary, reduced to
    presentation fields; evidence reads stay with the capabilities detail
    surface. Fail-soft like the floor strip: a store hiccup degrades to an
    explicit empty, degraded panel instead of failing the studio's one flight.
    """
    try:
        lister = mutations_lister
        if lister is None:
            from .gate import list_mutations
            lister = lambda uid: list_mutations(user_id=uid)
        rows = lister(user_id) or []
    except Exception as _e:
        logger.debug('gate pending failed: %s', _e)
        return {'items': [], 'degraded': True}
    items = []
    for row in rows:
        if row.get('status') not in ('proposed', 'confirmed'):
            continue
        payload = row.get('payload') or {}
        items.append({
            'mutation_id': str(row.get('mutation_id') or ''),
            'kind': str(row.get('kind') or ''),
            'status': str(row.get('status') or ''),
            'version': int(row.get('version') or 0),
            'title': str(payload.get('title') or payload.get('name')
                        or row.get('kind') or '')[:180],
            'evidence_count': len(row.get('evidence_episode_ids') or []),
            'updated_at': int(row.get('updated_at') or 0),
            **({'activation_intent': dict(row['activation_intent'])}
               if row.get('activation_intent') is not None else {}),
        })
    items.sort(key=lambda item: -(item['updated_at'] or 0))
    return {'items': items[:8], 'degraded': False}


def runs_panel(*, user_id, orchestration_lister=None, mutations_lister=None):
    """One-flight read for the studio run panel: summaries + live floor."""
    from .runs import list_run_snapshots
    fields = ('run_id', 'status', 'task_id', 'attempt', 'created_at', 'updated_at')
    rows = list_run_snapshots(user_id=user_id, limit=50)
    summaries = [{**{key: row[key] for key in fields},
                  'objective': row['objective'][:180]} for row in rows]
    return {'runs': summaries,
            'floor': floor_activity(
                rows, user_id=user_id,
                orchestration_lister=orchestration_lister),
            'proposals': gate_pending(
                user_id=user_id, mutations_lister=mutations_lister)}


def goal_workspace(run, *, user_id):
    """Small explicit projection: shelf, selected plan, and acceptance requirements."""
    from .library import list_materials
    from .goals import get_plan, get_goal
    plan = get_plan(run['plan_id'], user_id=user_id) or {}
    goal = get_goal(run['goal_id'], user_id=user_id) or {}
    candidate = next((c for c in plan.get('candidates', [])
                      if c.get('path_id') == plan.get('selected_path_id')), {})
    envelope = goal.get('budget_envelope') or {}
    return {'materials': list_materials(run['run_id'], user_id=user_id),
            'stages': [{'kind': str(s.get('kind') or '')[:80], 'detail': str(s.get('detail') or '')[:1000]}
                       for s in candidate.get('stages', [])[:16]],
            'criteria': [{'criterion_id':c['criterion_id'],
                          'requirement':str(c.get('params', {}).get('requirement') or c['kind'])[:1000]}
                         for c in goal.get('criteria', [])[:8]],
            'budget': {key: int(envelope.get(key) or 0)
                       for key in ('max_tokens', 'max_wall_clock_seconds',
                                   'max_tool_calls')} if envelope else None}
