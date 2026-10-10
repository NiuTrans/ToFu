"""Execution bridge between the task engine and self-evolution episodes.

Responsibility: project a terminal task-engine run into receipt-derived
episode actuals and settle the episode; retain authenticated running cost
lower bounds without granting replay authority. Entry points: ``derive_actuals``,
``open_for_task``, ``settle_from_task``, ``record_terminal_task``,
``execution_checkpoint`` and ``read_checkpoint_lower_bound``.
Dependencies: ``lib.cost.observed_usage_totals`` (the usage/caching authority),
an injectable owner-scoped task loader (default: the live chat
task runtime), the episodes store and owner-scoped task-result metadata reads.

The bridge never infers a verdict from task status: a finished pipeline is
not a met goal. Verdicts come from criteria checkers or explicit human
assessment. A task that errored or was aborted without a verdict settles as
``aborted`` — never as a silent failure and never as fabricated success.
"""
from __future__ import annotations

from lib.cost import observed_usage_totals, merge_usage_totals
from lib.identity import require_user_id

from .contracts import EvolutionStateError
from .episodes import open_episode, settle_episode

TASK_TERMINAL_STATES = ('done', 'error', 'aborted')
_ABORT_LIKE = ('error', 'aborted')
_OUTCOMES = ('success', 'partial', 'failure')


def default_task_loader(task_id: str, *, user_id):
    """Owner-scoped live-registry read; the injectable seam for other runtimes."""
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    return chat_task_runtime.get_owned(task_id, user_id=user_id)


def _resolve_task(task_or_id, *, user_id, loader):
    uid = require_user_id(user_id, context='evolution bridge task owner')
    if isinstance(task_or_id, dict):
        task = task_or_id
        if int(task.get('_userId') or 0) != int(uid):
            raise EvolutionStateError('task does not belong to this owner')
        return task
    task = (loader or default_task_loader)(str(task_or_id), user_id=uid)
    if task is None:
        raise EvolutionStateError('unknown or foreign task')
    return task


def actuals_complete(task: dict) -> bool:
    """Root receipt completeness includes still-unsettled physical requests."""
    budget = task.get('_executionBudget')
    return (task.get('_executionActualsComplete') is not False
            and (budget is None or budget.root_id != task.get('id')
                 or budget.actuals_complete()))


def derive_actuals(task: dict) -> dict:
    """Receipt-shaped consumption: tokens under either vendor spelling,
    wall-clock from the task's own clocks, tool calls from its rounds.
    Swarm child consumption rides the same receipt — a budget that cannot
    see spawned work is not a budget."""
    return terminal_actuals(task)[0]


def terminal_actuals(task: dict) -> tuple[dict, bool]:
    """Consumption and completeness form one terminal accounting snapshot."""
    usage = merge_usage_totals(task.get('usage'), task.get('_swarmChildUsage'))
    created = float(task.get('created_at') or 0)
    finished = float(task.get('finished_at') or 0)
    wall_clock = max(0, int(finished - created)) if created and finished else 0
    from lib.agent_core.execution_limits import count_tool_calls
    tool_calls = max(count_tool_calls(task),int(task.get('_executionToolsAdmitted') or 0)) + int(task.get('_swarmChildToolCalls') or 0)
    reported_tokens = max(observed_usage_totals(usage)[0],
                          int(task.get('_executionModelActualTokens') or 0))
    budget = task.get('_executionBudget')
    complete = task.get('_executionActualsComplete') is not False
    if budget is not None and budget.root_id == task.get('id'):
        reported_tokens, admitted_tools, account_complete = budget.consumption_snapshot()
        complete = complete and account_complete
        tool_calls = max(tool_calls, admitted_tools)
    if (task.get('kind') == 'research'
            and task.get('status') in ('done', 'error', 'aborted', 'interrupted')):
        from lib.research.budget_binding import read_model_budget
        from lib.research.model_attempt_journal import summarize_model_attempts
        owner = require_user_id(task.get('_userId'), context='research terminal accounting')
        if read_model_budget(task['id'], user_id=owner) is not None:
            summary = summarize_model_attempts(task['id'], user_id=owner)
            reported_tokens = max(reported_tokens, summary['account_token_lower_bound'])
            complete = complete and summary['recorded_usage_complete']
    return {
        'tokens': reported_tokens,
        'wall_clock_seconds': wall_clock,
        'tool_calls': int(tool_calls),
    }, complete


def open_for_task(task_or_id, *, goal_id: str, plan_id: str,
                  selected_path_id: str, user_id, now: int | None = None,
                  loader=None) -> dict:
    task = _resolve_task(task_or_id, user_id=user_id, loader=loader)
    task_id = str(task.get('id') or '')
    if not task_id:
        raise EvolutionStateError('task has no id to bind the episode to')
    return open_episode(goal_id=goal_id, plan_id=plan_id,
                        selected_path_id=selected_path_id, task_id=task_id,
                        user_id=user_id, now=now)


def settle_from_task(episode_id: str, task_or_id, *, user_id,
                     verdict: dict | None = None,
                     learning: dict | None = None,
                     stage_receipts: list | None = None,
                     now: int | None = None, loader=None) -> dict:
    task = _resolve_task(task_or_id, user_id=user_id, loader=loader)
    actuals, complete = terminal_actuals(task)
    return settle_terminal_receipt(
        episode_id, user_id=user_id, task_status=task.get('status'),
        actuals=actuals, verdict=verdict, learning=learning,
        actuals_complete=complete,
        stage_receipts=stage_receipts, now=now)


def settle_terminal_receipt(episode_id: str, *, user_id, task_status: str,
                            actuals: dict, verdict: dict | None = None,
                            learning: dict | None = None,
                            stage_receipts: list | None = None,
                            now: int | None = None, actuals_complete: bool = True) -> dict:
    """Settle a compact receipt derived from the committed task by this bridge."""
    if task_status not in TASK_TERMINAL_STATES:
        raise EvolutionStateError(
            f'task is {task_status!r}, not terminal; settlement waits for receipts')
    if task_status in _ABORT_LIKE and verdict is None:
        status, final_verdict = 'aborted', None
    else:
        if not isinstance(verdict, dict) or verdict.get('outcome') not in _OUTCOMES:
            raise EvolutionStateError(
                'a finished task still needs a criteria verdict — done is not success')
        status = verdict['outcome']
        final_verdict = verdict
    return settle_episode(
        episode_id, user_id=user_id, status=status,
        actuals_complete=actuals_complete,
        actuals=actuals, stage_receipts=stage_receipts,
        verdict=final_verdict, learning=learning, now=now)


def record_terminal_task(task_or_id, *, goal_id: str, plan_id: str,
                         selected_path_id: str, user_id,
                         verdict: dict | None = None,
                         learning: dict | None = None,
                         stage_receipts: list | None = None,
                         now: int | None = None, loader=None) -> dict:
    """Wrap one already-terminal run as a settled episode (the P0 manual path)."""
    episode = open_for_task(task_or_id, goal_id=goal_id, plan_id=plan_id,
                            selected_path_id=selected_path_id, user_id=user_id,
                            now=now, loader=loader)
    return settle_from_task(episode['episode_id'], task_or_id, user_id=user_id,
                            verdict=verdict, learning=learning,
                            stage_receipts=stage_receipts, now=now, loader=loader)


def execution_checkpoint(task):
    """Snapshot one server-bound stage's observed consumption, never completeness."""
    binding=task.get('_evolutionCheckpointBinding')
    if not binding:return None
    if binding.get('task_id')!=task.get('id') or binding.get('user_id')!=task.get('_userId'):
        raise EvolutionStateError('execution checkpoint carrier or owner changed')
    import time
    from .runs import _validate
    observed,_=terminal_actuals({**task,'finished_at':time.time()})
    value={'schema_version':1,**binding,'actuals':observed}
    _validate(value,'execution_checkpoint')
    return value


def _read_task_checkpoint_lower_bound(run,*,user_id):
    """Read authenticated partial costs after confirmed loss; no replay authority."""
    import json
    from lib.storage import get_storage_client
    from .runs import _validate
    from .contracts import EvolutionContractError
    row=get_storage_client().query('task_results.replay_get',
        {'key':run['task_id'],'user_id':user_id,'include_metadata':True},deadline=0.5)
    value=row or {}
    marker=run.get('stage_dispatch') or {}
    if (value.get('user_id')!=user_id or value.get('task_id')!=run['task_id']
            or value.get('conv_id')!='' or value.get('status') not in ('running','interrupted')
            or type((row or {}).get('version')) is not int
            or row['version']<=marker.get('birth_version',0)):
        return None
    try:
        metadata=value.get('metadata') or {}
        if isinstance(metadata,str):metadata=json.loads(metadata)
        checkpoint=metadata.get('evolutionConsumption')
        _validate(checkpoint,'execution_checkpoint')
    except (ValueError,TypeError,AttributeError,EvolutionContractError):return None
    expected={key:run[key] for key in ('run_id','task_id','goal_id','plan_id')}
    expected.update(intent_id=marker.get('intent_id'),birth_version=marker.get('birth_version'),stage=(run.get('stage_launch') or {}).get('stage'),user_id=user_id)
    if any(checkpoint.get(key)!=item for key,item in expected.items()):return None
    return {'task_id':run['task_id'],'stage':checkpoint['stage'],'actuals':checkpoint['actuals'],
            'actuals_complete':False,'checkpoint_lower_bound':True,'checkpoint_version':row['version']}


def read_checkpoint_lower_bound(run,*,user_id):
    """Combine witnessed lower bounds for one identity, never sums of duplicates."""
    from .stage_inputs import read_working_checkpoint_metadata, read_model_attempt_token_lower_bound, read_tool_account_lower_bound
    from .contracts import EvolutionContractError
    ordinary=_read_task_checkpoint_lower_bound(run,user_id=user_id)
    try:working=read_working_checkpoint_metadata(run,user_id=user_id)
    except (EvolutionStateError,EvolutionContractError):working=None
    receipt=ordinary
    if working is not None:
        observed=working['checkpoint']['binding']
        receipt={'task_id':run['task_id'],'stage':observed['stage'],
            'actuals':dict(observed['actuals']),'actuals_complete':False,'checkpoint_lower_bound':True,
            'checkpoint_version':working['version'],'checkpoint_source':'working_context'}
        if ordinary:
            receipt.update(checkpoint_version=ordinary['checkpoint_version'],checkpoint_source='task_result_and_working_context',
                working_checkpoint_version=working['version'])
            receipt['actuals']={key:max(value,ordinary['actuals'][key]) for key,value in receipt['actuals'].items()}
    try:attempts=read_model_attempt_token_lower_bound(run,user_id=user_id)
    except (EvolutionStateError,EvolutionContractError):attempts=None
    if attempts is not None:
        if receipt is None:
            receipt={'task_id':run['task_id'],'stage':attempts['stage'],
                'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':0},
                'actuals_complete':False,'checkpoint_lower_bound':True,'checkpoint_source':'model_attempts'}
        receipt['actuals']['tokens']=max(receipt['actuals']['tokens'],attempts['tokens'],attempts.get('account_tokens',0))
        receipt.update(model_attempts_count=attempts['count'],model_attempt_tokens=attempts['tokens'])
        if 'account_tokens' in attempts:receipt['model_account_tokens']=attempts['account_tokens']
    try:tools=read_tool_account_lower_bound(run,user_id=user_id)
    except (EvolutionStateError,EvolutionContractError):tools=None
    if tools is not None:
        if receipt is None:
            receipt={'task_id':run['task_id'],'stage':tools['stage'],
                'actuals':{'tokens':0,'tool_calls':0,'wall_clock_seconds':0},
                'actuals_complete':False,'checkpoint_lower_bound':True,'checkpoint_source':'tool_attempts'}
        receipt['actuals']['tool_calls']=max(receipt['actuals']['tool_calls'],tools['calls'])
        receipt.update(tool_attempts_count=tools['count'],tool_account_calls=tools['calls'])
    return receipt
