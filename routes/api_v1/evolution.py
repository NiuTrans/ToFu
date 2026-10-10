"""routes/api_v1/evolution.py — Self-evolution module API surface.

Design authority: ``docs/EVOLUTION_DESIGN.md``; library owner:
``lib/evolution/``. This blueprint is a stateless adapter: every endpoint
parses input, resolves the principal, and delegates to one library call.

Routes:
  POST /api/v4/evolution/goals                       — admit a goal
  GET  /api/v4/evolution/goals                       — list owner goals
  POST /api/v4/evolution/goals/<goal_id>/plans       — propose a plan
  POST /api/v4/evolution/plans/<plan_id>/confirm     — confirm a plan
  POST /api/v4/evolution/plans/<plan_id>/launch      — open episode + bound task
  GET  /api/v4/evolution/episodes                    — list episodes
  POST /api/v4/evolution/episodes/<episode_id>/verdict — human verdict
  POST /api/v4/evolution/recompute                   — priors+strategies+gaps
  GET  /api/v4/evolution/priors|strategies|anti-patterns|mutations|capability-gaps
  POST /api/v4/evolution/mutations                   — propose a mutation
  POST /api/v4/evolution/mutations/<mutation_id>/<action> — gate transitions
"""

from __future__ import annotations

from quart import Blueprint, request

from lib import evolution
from lib.evolution import automation
from lib.api_response import (
    api_bad_request, api_conflict, api_created, api_not_found, api_ok,
)
from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import request_user_id, require_scope

logger = get_logger(__name__)

api_v1_evolution_bp = Blueprint('api_v1_evolution', __name__)

#: Installing the settlement listener is this surface's one boot-time effect;
#: the blueprint import happens once on route registration.
automation.install()

_GATE_ACTIONS = {
    'confirm': evolution.confirm_mutation,
    'reject': evolution.reject_mutation,
    'activate': evolution.activate_mutation,
    'rollback': evolution.rollback_mutation,
}


def _call(fn, *args, **kwargs):
    """Map the library's typed failures onto canonical envelopes."""
    try:
        return fn(*args, **kwargs)
    except evolution.EvolutionContractError as exc:
        return api_bad_request(str(exc))
    except evolution.EvolutionStateError as exc:
        return api_conflict(str(exc))
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return api_conflict('evolution state changed; reload before retrying')
        raise
    except (ValueError, TypeError) as exc:
        return api_bad_request(str(exc))


@api_v1_evolution_bp.route('/api/v4/evolution/programs', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Create a durable long-horizon exploration program', tags=['evolution'], scope='tasks')
def create_evolution_program():
    from lib.evolution.programs import create_program
    result = _call(create_program, parse_body() or {}, user_id=request_user_id(),
                   idempotency_key=request.headers.get('Idempotency-Key', ''))
    return api_created({'program': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/programs', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List owner exploration programs', tags=['evolution'], scope='tasks')
def list_evolution_programs():
    from lib.evolution.programs import program_page
    result = _call(lambda: program_page(user_id=request_user_id(),
        after=request.args.get('after', ''), before=request.args.get('before', ''),
        limit=int(request.args.get('limit', '20'))))
    return api_ok(result) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/programs/<program_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Read durable exploration progress', tags=['evolution'], scope='tasks')
def get_evolution_program(program_id):
    from lib.evolution.programs import get_program
    result = _call(get_program, program_id, user_id=request_user_id())
    return api_ok({'program': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/programs/<program_id>/memory', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Read recent source-bound semantic archive lookups', tags=['evolution'], scope='tasks')
def get_evolution_program_memory(program_id):
    from lib.evolution.semantic_memory import history
    result = _call(history, program_id, user_id=request_user_id())
    return api_ok({'memory': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/programs/<program_id>/commands', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Advance, pause, resume, cancel or retry exploration', tags=['evolution'], scope='tasks')
def command_evolution_program(program_id):
    from lib.evolution.programs import command_program
    result = _call(command_program, program_id, parse_body() or {}, user_id=request_user_id())
    return api_ok({'program': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/programs/<program_id>/steps', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Expand exploration before its final audit', tags=['evolution'], scope='tasks')
def append_evolution_program_steps(program_id):
    from lib.evolution.programs import append_steps
    result = _call(append_steps, program_id, parse_body() or {}, user_id=request_user_id(),
                   idempotency_key=request.headers.get('Idempotency-Key', ''))
    return api_ok({'program': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/programs/<program_id>/experiments', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Page immutable experiment receipts and long-term memory', tags=['evolution'], scope='tasks')
def page_evolution_experiments(program_id):
    from lib.evolution.programs import experiment_page
    result = _call(lambda: experiment_page(program_id, user_id=request_user_id(),
                    after=int(request.args.get('after', '0')),
                    limit=int(request.args.get('limit', '20'))))
    return api_ok(result) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/goals', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Admit an evolution goal', tags=['evolution'], scope='tasks')
def create_goal():
    body = parse_body() or {}
    result = _call(evolution.admit_goal, body, user_id=request_user_id())
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_created({'goal': result})


@api_v1_evolution_bp.route('/api/v4/evolution/goals', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List evolution goals', tags=['evolution'], scope='tasks')
def list_goals_route():
    return api_ok({'goals': evolution.list_goals(user_id=request_user_id())})


@api_v1_evolution_bp.route('/api/v4/evolution/goals/<goal_id>/plans',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Propose a plan for a goal', tags=['evolution'],
          scope='tasks')
def propose_plan(goal_id: str):
    user_id = request_user_id()
    goal = evolution.get_goal(goal_id, user_id=user_id)
    if goal is None:
        return api_not_found('goal')
    body = parse_body() or {}
    candidates = body.get('candidates')
    if not candidates:
        candidates = evolution.suggest_candidates(goal, user_id=user_id)
    result = _call(evolution.build_plan, goal, candidates, user_id=user_id)
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_created({'plan': result})


@api_v1_evolution_bp.route('/api/v4/evolution/plans/<plan_id>/confirm',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Confirm a proposed plan', tags=['evolution'], scope='tasks')
def confirm_plan(plan_id: str):
    user_id = request_user_id()
    plan = evolution.get_plan(plan_id, user_id=user_id)
    if plan is None:
        return api_not_found('plan')
    if plan.get('status') != 'proposed':
        return api_conflict(f"plan is {plan.get('status')}, not proposed")
    confirmed = dict(plan, status='confirmed')
    return api_ok({'plan': _call(evolution.save_plan, confirmed,
                                 user_id=user_id)})


@api_v1_evolution_bp.route('/api/v4/evolution/plans/<plan_id>/reject',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Reject a proposed plan', tags=['evolution'], scope='tasks')
def reject_plan(plan_id: str):
    user_id = request_user_id()
    plan = evolution.get_plan(plan_id, user_id=user_id)
    if plan is None:
        return api_not_found('plan')
    if plan.get('status') != 'proposed':
        return api_conflict(f"plan is {plan.get('status')}, not proposed")
    rejected = dict(plan, status='rejected')
    return api_ok({'plan': _call(evolution.save_plan, rejected,
                                 user_id=user_id)})


@api_v1_evolution_bp.route('/api/v4/evolution/plans/<plan_id>/launch',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Launch a confirmed plan as a bound chat task',
          tags=['evolution'], scope='tasks')
def launch_plan(plan_id: str):
    """Spawn the execution task, then bind a fresh episode to its id.

    The task config carries goal/plan for the guidance block; the durable
    settlement link is the episode's task_id, matched by the terminal
    listener — the live task config is never mutated after spawn.
    """
    user_id = request_user_id()
    plan = evolution.get_plan(plan_id, user_id=user_id)
    if plan is None:
        return api_not_found('plan')
    if plan.get('status') != 'confirmed':
        return api_conflict('launch requires a confirmed plan')
    goal = evolution.get_goal(plan.get('goal_id') or '', user_id=user_id)
    if goal is None:
        return api_not_found('goal')
    body = parse_body() or {}
    result = _call(automation.launch_plan, plan, goal, body, user_id=user_id)
    if not isinstance(result, dict) or 'episode' not in result:
        return result
    return api_created(result)


@api_v1_evolution_bp.route('/api/v4/evolution/plans', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List evolution plans', tags=['evolution'], scope='tasks')
def list_plans_route():
    from quart import request
    return api_ok({'plans': evolution.list_plans(
        user_id=request_user_id(),
        goal_id=request.args.get('goal_id') or None)})


@api_v1_evolution_bp.route('/api/v4/evolution/episodes', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List evolution episodes', tags=['evolution'], scope='tasks')
def list_episodes_route():
    return api_ok({'episodes': evolution.list_episodes(
        user_id=request_user_id())})


@api_v1_evolution_bp.route('/api/v4/evolution/episodes/<episode_id>/verdict',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Record a human verdict for a terminal task episode',
          tags=['evolution'], scope='tasks')
def episode_verdict(episode_id: str):
    user_id = request_user_id()
    episode = evolution.get_episode(episode_id, user_id=user_id)
    if episode is None:
        return api_not_found('episode')
    body = parse_body() or {}
    verdict = {
        'outcome': str(body.get('outcome') or ''),
        'assessed_by': 'human',
        'criterion_results': body.get('criterion_results') or [],
    }
    result = _call(evolution.settle_from_task, episode_id,
                   str(episode.get('task_id') or ''), user_id=user_id,
                   verdict=verdict, learning=body.get('learning'))
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_ok({'episode': result})


@api_v1_evolution_bp.route('/api/v4/evolution/recompute', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Run the full maintenance cycle (learning heartbeat)',
          tags=['evolution'], scope='tasks')
def recompute():
    return api_ok(evolution.run_maintenance_cycle(
        user_id=request_user_id()))


@api_v1_evolution_bp.route('/api/v4/evolution/goals/<goal_id>/report',
                           methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Render the evidence-backed experiment report for a goal',
          tags=['evolution'], scope='tasks')
def goal_report(goal_id: str):
    text = evolution.render_goal_report(goal_id, user_id=request_user_id())
    if not text:
        return api_not_found('goal')
    return api_ok({'report': text})


@api_v1_evolution_bp.route('/api/v4/evolution/distillations', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Build a verified-trajectory distillation dataset',
          tags=['evolution'], scope='tasks')
def build_distillation():
    body = parse_body() or {}
    result = _call(evolution.build_trajectory_dataset,
                   user_id=request_user_id(),
                   domain=str(body.get('domain') or 'general'))
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_created({'dataset': result})


@api_v1_evolution_bp.route('/api/v4/evolution/<store_name>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List an evolution store', tags=['evolution'], scope='tasks')
def list_store(store_name: str):
    user_id = request_user_id()
    readers = {
        'priors': lambda: evolution.list_priors(user_id=user_id),
        'strategies': lambda: evolution.list_strategies(user_id=user_id),
        'anti-patterns': lambda: evolution.list_anti_patterns(user_id=user_id),
        'mutations': lambda: evolution.list_mutations(user_id=user_id),
        'capability-gaps': lambda: evolution.list_capability_gaps(
            user_id=user_id),
        'hypotheses': lambda: evolution.list_hypotheses(user_id=user_id),
        'skills': lambda: evolution.list_forged_skills(user_id=user_id),
        'holdout': lambda: evolution.list_holdout(user_id=user_id),
    }
    reader = readers.get(store_name)
    if reader is None:
        return api_not_found('store')
    return api_ok({store_name.replace('-', '_'): reader()})


@api_v1_evolution_bp.route('/api/v4/evolution/teach', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Teach the system a capability (forge, sandbox, gate)',
          tags=['evolution'], scope='tasks')
def teach_route():
    body = parse_body() or {}
    result = _call(evolution.teach,
                   str(body.get('name') or ''),
                   str(body.get('description') or ''),
                   knowledge=str(body.get('knowledge') or ''),
                   examples=body.get('examples') or [],
                   code=body.get('code') or None,
                   tests=body.get('tests') or None,
                   gap_id=body.get('gap_id') or None,
                   user_id=request_user_id())
    if not isinstance(result, dict) or 'skill' not in result:
        return result
    return api_created(result)


@api_v1_evolution_bp.route('/api/v4/evolution/snapshot', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='System awareness snapshot', tags=['evolution'],
          scope='tasks')
def snapshot_route():
    return api_ok({'snapshot': evolution.system_snapshot(
        user_id=request_user_id())})


@api_v1_evolution_bp.route('/api/v4/evolution/observations', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List observations and their open clusters',
          tags=['evolution'], scope='tasks')
def list_observations_route():
    from quart import request
    user_id = request_user_id()
    return api_ok({
        'observations': evolution.list_observations(
            user_id=user_id, kind=request.args.get('kind') or None,
            status=request.args.get('status') or None, limit=50),
        'clusters': evolution.cluster_observations(user_id=user_id),
    })


@api_v1_evolution_bp.route('/api/v4/evolution/observations', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Record an owner observation (zero learning rights)',
          tags=['evolution'], scope='tasks')
def record_observation_route():
    # Admit exactly the observation contract's fields (single authority:
    # contracts/evolution_observation_v1.schema.json), never a bare parse.
    from lib.evolution.contracts import OBSERVATION_SCHEMA, load_schema
    body = parse_body(exact=tuple(sorted(
        load_schema(OBSERVATION_SCHEMA)['properties']))) or {}
    user_id = request_user_id()
    from lib.evolution.observations import observation_id
    kind = str(body.get('kind') or 'anomaly')
    subject = body.get('subject') or {'type': 'frontier', 'id': 'general'}
    statement = str(body.get('statement') or '')
    result = _call(evolution.record_observation, {
        'schema_version': 1,
        'observation_id': observation_id(kind, subject, statement,
                                         salt='owner'),
        'kind': kind,
        'statement': statement,
        'subject': subject,
        'lineage': [str(x)[:160] for x in (body.get('lineage') or [])][:8],
        'source': {'actor': 'owner'},
        'status': 'open',
        'created_at': 0,
        'updated_at': 0,
    }, user_id=user_id)
    if not isinstance(result, dict) or 'observation_id' not in result:
        return result
    return api_created({'observation': result})


@api_v1_evolution_bp.route(
    '/api/v4/evolution/observations/<observation_id>/promote',
    methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Promote an observation into a draft frame-change goal',
          tags=['evolution'], scope='tasks')
def promote_observation_route(observation_id: str):
    result = _call(evolution.promote_observation, observation_id,
                   user_id=request_user_id())
    if not isinstance(result, dict) or 'goal' not in result:
        return result
    return api_created(result)


@api_v1_evolution_bp.route(
    '/api/v4/evolution/observations/<observation_id>/dismiss',
    methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Park an observation (it stops clustering)',
          tags=['evolution'], scope='tasks')
def dismiss_observation_route(observation_id: str):
    result = _call(evolution.set_observation_status, observation_id,
                   'dismissed', user_id=request_user_id())
    if not isinstance(result, dict) or 'observation_id' not in result:
        return result
    return api_ok({'observation': result})


@api_v1_evolution_bp.route('/api/v4/evolution/skills', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List forged skills', tags=['evolution'], scope='tasks')
def list_skills_route():
    from quart import request
    return api_ok({'skills': evolution.list_forged_skills(
        user_id=request_user_id(), status=request.args.get('status') or None)})


@api_v1_evolution_bp.route('/api/v4/evolution/skills/forge', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Forge a skill draft (LLM author by default)',
          tags=['evolution'], scope='tasks')
def forge_skill_route():
    body = parse_body() or {}
    result = _call(evolution.forge_draft, {
        'name': body.get('name'), 'description': body.get('description'),
        'knowledge': body.get('knowledge') or '',
        'examples': body.get('examples') or [],
        'gap_id': body.get('gap_id'), 'uses': body.get('uses') or [],
        'lineage': body.get('lineage') or [],
    }, user_id=request_user_id())
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_created({'skill': result})


@api_v1_evolution_bp.route('/api/v4/evolution/skills/<skill_id>/<action>',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Advance a forged skill: sandbox, propose, or install',
          tags=['evolution'], scope='tasks')
def skill_action(skill_id: str, action: str):
    user_id = request_user_id()
    body = parse_body() or {}
    if action == 'sandbox':
        result = _call(evolution.sandbox_run, skill_id, user_id=user_id)
    elif action == 'propose':
        result = _call(evolution.propose_skill, skill_id, user_id=user_id,
                       evidence_episode_ids=body.get(
                           'evidence_episode_ids') or [])
    elif action == 'install':
        result = _call(evolution.install_skill, skill_id, user_id=user_id)
    else:
        return api_not_found('skill action')
    if isinstance(result, dict) and result.get('mutation_id'):
        return api_ok({'mutation': result})
    if not isinstance(result, dict) or (
            'schema_version' not in result and 'mutation_id' not in result):
        return result
    return api_ok({'skill': result})


@api_v1_evolution_bp.route('/api/v4/evolution/hypotheses', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Propose self-model hypotheses', tags=['evolution'],
          scope='tasks')
def propose_hypotheses_route():
    body = parse_body() or {}
    result = _call(evolution.propose_hypotheses,
                   body.get('hypotheses') or [], user_id=request_user_id())
    if isinstance(result, list):
        return api_created({'hypotheses': result})
    return result


@api_v1_evolution_bp.route('/api/v4/evolution/hypotheses/<action>',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Author (LLM) or validate self-model hypotheses',
          tags=['evolution'], scope='tasks')
def hypotheses_action(action: str):
    user_id = request_user_id()
    body = parse_body() or {}
    if action == 'author':
        result = _call(evolution.author_hypotheses,
                       str(body.get('domain') or ''), user_id=user_id)
    elif action == 'validate':
        result = _call(evolution.validate_hypotheses, user_id=user_id)
    else:
        return api_not_found('hypotheses action')
    if isinstance(result, list):
        return api_ok({'hypotheses': result})
    return result


@api_v1_evolution_bp.route('/api/v4/evolution/curriculum/frontier',
                           methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Read the curriculum frontier digest',
          tags=['evolution'], scope='tasks')
def frontier_route():
    return api_ok({'frontier': evolution.frontier_digest(
        user_id=request_user_id())})


@api_v1_evolution_bp.route('/api/v4/evolution/curriculum/propose',
                           methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Admit frontier probe goals', tags=['evolution'],
          scope='tasks')
def propose_probes_route():
    result = _call(evolution.propose_probes, user_id=request_user_id())
    if isinstance(result, list):
        return api_created({'probes': result})
    return result


@api_v1_evolution_bp.route('/api/v4/evolution/mutations', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Propose an evolution mutation', tags=['evolution'],
          scope='tasks')
def propose_mutation_route():
    body = parse_body() or {}
    result = _call(evolution.propose_mutation,
                   str(body.get('kind') or ''), body.get('payload') or {},
                   body.get('evidence_episode_ids') or [],
                   user_id=request_user_id())
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_created({'mutation': result})


@api_v1_evolution_bp.route(
    '/api/v4/evolution/mutations/<mutation_id>/<action>', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Transition a mutation through the evolution gate',
          tags=['evolution'], scope='tasks')
def mutation_action(mutation_id: str, action: str):
    user_id = request_user_id()
    if action == 'enable':
        return _enable_mutation(mutation_id, user_id)
    if action == 'enable_waiver':
        return _enable_mutation(mutation_id, user_id, waiver=True)
    fn = _GATE_ACTIONS.get(action)
    if fn is None:
        return api_not_found('gate action')
    kwargs = {}
    if action == 'reject':
        kwargs['reason'] = str((parse_body() or {}).get('reason') or '')
    result = _call(fn, mutation_id, user_id=user_id, **kwargs)
    if not isinstance(result, dict) or 'schema_version' not in result:
        return result
    return api_ok({'mutation': result})


def _enable_mutation(mutation_id: str, user_id, *, waiver: bool = False):
    """Composite gate journey for the workbench: confirm → holdout
    regression → activate. Stops honestly at the failing step and reports
    it; ``needs_waiver`` flags the explicit operator choice for
    unmeasurable kinds instead of ever waiving silently. A waiver retry
    skips the already-taken confirmation step."""
    try:
        mutation = evolution.get_mutation(mutation_id, user_id=user_id)
    except evolution.EvolutionStateError:
        return api_not_found('mutation')
    if mutation.get('status') == 'active':
        return api_ok({'mutation': mutation, 'final_status': 'active'})
    if mutation.get('activation_intent') is not None:
        activated = _call(evolution.activate_mutation, mutation_id, user_id=user_id)
        if not isinstance(activated, dict) or 'schema_version' not in activated:
            return activated
        return api_ok({'mutation': activated, 'final_status': 'active'})
    if mutation.get('status') == 'proposed':
        try:
            mutation = evolution.confirm_mutation(mutation_id,
                                                  user_id=user_id)
        except evolution.EvolutionStateError as exc:
            return api_conflict(str(exc))
    elif mutation.get('status') != 'confirmed':
        return api_conflict(
            f"mutation is {mutation.get('status')}, not proposed/confirmed")
    receipt = evolution.run_regression(mutation, user_id=user_id)
    if receipt['verdict'] == 'fail':
        return api_ok({'mutation': mutation, 'receipt': receipt,
                       'final_status': 'regression_failed'})
    if receipt['verdict'] == 'unmeasurable' and not waiver:
        return api_ok({'mutation': mutation, 'receipt': receipt,
                       'final_status': 'needs_waiver'})
    try:
        activated = evolution.activate_mutation(
            mutation_id, user_id=user_id, allow_unmeasurable=waiver)
    except evolution.EvolutionStateError as exc:
        return api_conflict(str(exc))
    except StorageError as exc:
        if exc.code == 'database_conflict':
            return api_conflict('evolution state changed; reload before retrying')
        raise
    return api_ok({'mutation': activated, 'receipt': receipt,
                   'final_status': 'active'})


@api_v1_evolution_bp.route('/api/v4/evolution/runs', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Execute an objective with automatic planning and independent review', tags=['evolution'], scope='tasks')
def submit_evolution_run():
    from quart import request
    from lib.evolution.runs import submit_run
    result = _call(submit_run, parse_body() or {}, user_id=request_user_id(),
                   idempotency_key=request.headers.get('Idempotency-Key', ''))
    return api_created({'run': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Read durable evolution progress and delivery evidence', tags=['evolution'], scope='tasks')
def get_evolution_run(run_id):
    from lib.evolution.runs import get_run_snapshot
    result = get_run_snapshot(run_id, user_id=request_user_id())
    from lib.evolution.workspace import pending_questions, goal_workspace
    return api_ok({'run': result, 'workspace': goal_workspace(result, user_id=request_user_id()), 'pending_questions': pending_questions(
        result, user_id=request_user_id())}) if result else api_not_found('run')


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/outcomes/<task_id>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Page committed terminal output without model invocation', tags=['evolution'], scope='tasks')
def get_evolution_stage_outcome(run_id, task_id):
    from lib.evolution.stage_outcomes import page
    result = _call(lambda: page(run_id, task_id, user_id=request_user_id(),
                               offset=int(request.args.get('offset', '0')),
                               source_kind=request.args.get('source_kind', 'deliverable')))
    return api_ok({'outcome': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/reconcile', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Reconcile a missed evolution terminal callback without replaying execution', tags=['evolution'], scope='tasks')
def reconcile_evolution_run(run_id):
    from lib.evolution.runs import advance_run
    result = _call(advance_run, run_id, user_id=request_user_id())
    return api_ok({'run': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/cancel', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Cancel the active evolution task and prevent further stages', tags=['evolution'], scope='tasks')
def cancel_evolution_run(run_id):
    from lib.evolution.runs import cancel_run
    result = _call(cancel_run, run_id, user_id=request_user_id())
    return api_ok({'run': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/answers', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Answer the active run Ask Human question', tags=['evolution'], scope='tasks')
def answer_evolution_run(run_id):
    from lib.evolution.runs import answer_run
    body = parse_body() or {}
    if not isinstance(body, dict) or set(body) != {'guidance_id', 'response'}:
        return api_bad_request('answers require guidance_id and response')
    result = _call(answer_run, run_id, body.get('guidance_id'), body.get('response'),
                   user_id=request_user_id())
    return api_ok({'run': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='List bounded owner evolution run records', tags=['evolution'], scope='tasks')
def list_evolution_runs():
    from lib.evolution.workspace import runs_panel
    return api_ok(runs_panel(user_id=request_user_id()))


@api_v1_evolution_bp.route('/api/v4/evolution/models', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Available owner-scoped evolution model choices', tags=['evolution'], scope='tasks')
def evolution_models():
    from lib.evolution.workspace import model_options
    return api_ok({'models': model_options(user_id=request_user_id())})


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/materials', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Save a material to one goal bookshelf', tags=['evolution'], scope='tasks')
def save_evolution_material(run_id):
    from lib.evolution.library import save_material
    from lib.evolution.runs import get_run
    owner = request_user_id()
    if not get_run(run_id, user_id=owner):
        return api_not_found('run')
    result = _call(save_material, run_id, parse_body() or {}, user_id=owner)
    return api_ok({'material': result}) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/materials/<material_id>', methods=['DELETE'])
@require_scope('tasks')
@api_meta(summary='Remove a material from one goal bookshelf', tags=['evolution'], scope='tasks')
def remove_evolution_material(run_id, material_id):
    from lib.evolution.library import remove_material
    from lib.evolution.runs import get_run
    owner = request_user_id()
    if not get_run(run_id, user_id=owner):
        return api_not_found('run')
    result = _call(remove_material, run_id, material_id, user_id=owner)
    return api_ok(result) if isinstance(result, dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/updates', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Add idempotent owner guidance, conversation or experimental evidence to a goal', tags=['evolution'], scope='tasks')
def publish_goal_update(run_id):
    from lib.evolution.coordination import publish
    from lib.evolution.coordinator import notify
    body=parse_body() or {};owner=request_user_id()
    result=_call(publish,run_id,body,user_id=owner,event_id=request.headers.get('Idempotency-Key',''))
    if not isinstance(result,dict):return result
    manager=notify(run_id,user_id=owner,enable=body.get('kind')=='message')
    # The update was committed even when the optional model wake failed.
    return api_ok({'update':result,'coordinator':manager if isinstance(manager,dict) else {'status':'pending','error':'use coordinator reconcile'}})


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/coordination', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Read bounded goal updates, explicit acknowledgements and coordinator state', tags=['evolution'], scope='tasks')
def get_goal_coordination(run_id):
    from lib.evolution.conversation import conversation_page
    try:
        after=int(request.args.get('after','0'))
        before=int(request.args['before']) if 'before' in request.args else None
    except ValueError:return api_bad_request('cursor must be an integer')
    if not 0<=after<=1000000 or (before is not None and not 1<=before<=1000000):
        return api_bad_request('cursor is out of range')
    result=_call(conversation_page,run_id,user_id=request_user_id(),after=after,
                 recent=request.args.get('view')=='recent',before=before)
    return api_ok(result) if isinstance(result,dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/coordinator/reconcile', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Reconcile a coordinator task without replaying unknown execution', tags=['evolution'], scope='tasks')
def reconcile_goal_coordinator(run_id):
    from lib.evolution.coordinator import reconcile
    result=_call(reconcile,run_id,user_id=request_user_id())
    return api_ok({'coordinator':result}) if isinstance(result,dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/followups', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Explicitly authorize one linked follow-up run with frozen source context', tags=['evolution'], scope='tasks')
def create_goal_followup(run_id):
    from lib.evolution.coordinator import start_followup
    result=_call(start_followup,run_id,parse_body() or {},user_id=request_user_id(),request_id=request.headers.get('Idempotency-Key',''))
    return api_created({'run':result}) if isinstance(result,dict) else result


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/updates/<int:seq>', methods=['GET'])
@require_scope('tasks')
@api_meta(summary='Read complete owner-visible detail of one goal update', tags=['evolution'], scope='tasks')
def get_goal_update_detail(run_id,seq):
    from lib.evolution.coordination import snapshot
    state=_call(snapshot,run_id,user_id=request_user_id())
    if not isinstance(state,dict):return state
    event=next((e for e in state['updates'] if e['seq']==seq),None)
    return api_ok({'update':event}) if event else api_not_found('update')


@api_v1_evolution_bp.route('/api/v4/evolution/runs/<run_id>/role-model', methods=['POST'])
@require_scope('tasks')
@api_meta(summary='Select a Chat model for future goal role tasks', tags=['evolution'], scope='tasks')
def update_goal_role_model(run_id):
    from lib.evolution.role_models import update_role_model
    result = _call(update_role_model, run_id, parse_body() or {}, user_id=request_user_id())
    return api_ok({'run': result}) if isinstance(result, dict) and 'run_id' in result else result
