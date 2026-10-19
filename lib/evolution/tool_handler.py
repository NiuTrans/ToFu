"""Dispatch handler for the evolution_teach built-in tool.

Registered through `ToolSpec('evolution', ...)` in
`lib/tools/registry/_build.py` with an exposure gate: the tool is offered to
the model only when the task config carries an evolution binding, so ordinary
chat and every internal application never see it. The handler itself re-checks
the binding — exposure is prompt policy, the handler is the authority.
"""
from __future__ import annotations

from .contracts import EvolutionContractError, EvolutionStateError
from .teach import teach


def _entry(fn_name: str, title: str, snippet: str, *, fetched: bool = True,
           badge: str = '进化') -> dict:
    return {'toolName': fn_name, 'title': title, 'snippet': snippet,
            'source': 'self-evolution', 'fetched': fetched, 'badge': badge}


def handle_evolution_teach(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                           cfg, project_path, project_enabled, all_tools=None):
    binding = (cfg or {}).get('evolution') or {}
    if not binding.get('goal_id'):
        return [_entry(fn_name, 'teach unavailable',
                       'evolution_teach runs only in evolution-bound tasks',
                       fetched=False)]
    owner = int(task.get('_userId') or 0)
    if owner < 1:
        return [_entry(fn_name, 'teach refused', 'missing owner identity',
                       fetched=False)]
    args = fn_args or {}
    try:
        result = teach(
            str(args.get('name') or ''), str(args.get('description') or ''),
            knowledge=str(args.get('knowledge') or ''),
            examples=[str(e) for e in (args.get('examples') or [])][:8],
            code=args.get('code') or None, tests=args.get('tests') or None,
            user_id=owner)
    except (EvolutionContractError, EvolutionStateError, ValueError,
            TypeError) as exc:
        return [_entry(fn_name, 'teach rejected', str(exc)[:300],
                       fetched=False)]
    skill = result['skill']
    if result['mutation'] is not None:
        snippet = (f"verified in sandbox and proposed to the evolution gate "
                   f"(episode {result['episode']['episode_id'][:12]}); it "
                   'becomes active after human confirmation in the workbench')
        return [_entry(fn_name, f"taught: {skill['name']}", snippet)]
    output = (skill.get('sandbox_receipt') or {}).get('output') or ''
    return [_entry(fn_name, f"teach failed sandbox: {skill['name']}",
                   f'the draft failed its own tests — refine and retry: '
                   f'{output[-260:]}', fetched=False)]


def handle_evolution_reindex(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                             cfg, project_path, project_enabled, all_tools=None):
    """Dispatch handler for the evolution_reindex bound tool.

    Same binding discipline as the teach/material tools: exposure is prompt
    policy, ``authorize_task`` is the authority.
    """
    from .coordination import authorize_task
    from .refetch import reindex_for_run
    try:
        run, role, _state = authorize_task(task)
        if role not in ('executing', 'coordinator'):
            raise EvolutionStateError(
                'reindex requires a bound executor or coordinator')
        observation_actor = 'executor' if role == 'executing' else 'coordinator'
        args = fn_args or {}
        result = reindex_for_run(
            run['run_id'], [str(q) for q in (args.get('queries') or [])],
            user_id=int(task.get('_userId') or 0), actor=observation_actor,
            task_id=str(task.get('id') or task.get('task_id') or ''),
            reason=str(args.get('reason') or ''))
    except (EvolutionContractError, EvolutionStateError, ValueError,
            TypeError) as exc:
        return [_entry(fn_name, 'reindex refused', str(exc)[:300],
                       fetched=False)]
    if result.get('already_done'):
        return [_entry(fn_name, 'reindex already applied',
                       'the same refined queries already landed their '
                       'sources on the goal bookshelf')]
    return [_entry(fn_name, 'reindex complete',
                   f"landed {result['saved']} new source(s) on the goal "
                   f"bookshelf ({result['skipped']} skipped); read them "
                   'with evolution_material action=list')]
