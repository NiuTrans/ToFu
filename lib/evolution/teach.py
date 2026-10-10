"""Teach channel: owner-taught knowledge becomes a gated, episode-evidenced skill.

Responsibility: one orchestration for every surface (workbench Teach form,
API, future chat tool) — forge the draft, prove it in the sandbox, record
the lesson as a meta-goal episode whose verdict comes from the sandbox
receipt, and route the skill into the gate with that episode as evidence.
Entry points: ``teach`` / ``TEACH_TOOL_DEFINITION``. Dependencies: the forge,
goals/episodes stores, the gate.

The meta-goal episode is the honesty spine: the gate's "no evidence, no
mutation" rule is satisfied by a real executed check, not by the owner's
say-so. When the caller supplies code/tests inline (a chat model that
already has the context), no second authoring turn is spent; otherwise the
forge's bounded default author runs.
"""
from __future__ import annotations

import time

from .episodes import open_episode, settle_episode
from .goals import admit_goal
from .skill_forge import forge_draft, propose_skill, sandbox_run

#: Prompt artifact for any chat surface that offers the teach channel. The
#: description is the prompt design: it states when to teach and when not to.
TEACH_TOOL_DEFINITION = {
    'type': 'function',
    'function': {
        'name': 'evolution_teach',
        'description': (
            'Teach the system a durable, verified capability when the user '
            'explains how to do something it could not do before (e.g. '
            '"remember: GPU training is submitted through HOP MCP, queue '
            'gpu-a100"). The knowledge is forged into executable skill code, '
            'proven in a sandbox where each of the user\'s examples becomes '
            'a mandatory tested behavior, and proposed to the evolution gate '
            'for human confirmation. Use ONLY when the user teaches a '
            'reusable procedure, system, or tool usage. Do NOT use for '
            'one-off facts or preferences (those belong to memory), and '
            'never teach on the user\'s behalf without their knowledge.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string',
                         'description': 'short skill name, e.g. hop-training'},
                'description': {'type': 'string',
                                'description': 'what the capability does'},
                'knowledge': {'type': 'string',
                              'description': 'everything the user taught: '
                              'commands, endpoints, queues, pitfalls'},
                'examples': {
                    'type': 'array', 'items': {'type': 'string'},
                    'description': 'usage examples from the user; each one '
                    'becomes a mandatory tested behavior'},
            },
            'required': ['name', 'description', 'knowledge', 'examples'],
            'additionalProperties': False,
        },
    },
}

_META_BUDGET = {'max_tokens': 50000, 'max_wall_clock_seconds': 600,
                'max_tool_calls': 10}


def teach(name: str, description: str, *, knowledge: str = '',
          examples: list | None = None, code: str | None = None,
          tests: str | None = None, gap_id: str | None = None,
          user_id, author=None, now: int | None = None) -> dict:
    """Run the full teach flow; returns skill, episode, verdict, mutation."""
    if code and tests:
        author = lambda request: {'code': code, 'tests': tests}
    ts = int(now if now is not None else time.time())
    skill = forge_draft({
        'name': name, 'description': description, 'knowledge': knowledge,
        'examples': list(examples or []), 'gap_id': gap_id,
    }, user_id=user_id, author=author, now=ts)
    skill = sandbox_run(skill['skill_id'], user_id=user_id, now=ts)
    receipt = skill.get('sandbox_receipt') or {}

    goal = admit_goal({
        'schema_version': 1,
        'goal_id': f"teach-{skill['skill_id']}",
        'statement': f'Learn the taught capability: {skill["name"]}',
        'domain': 'meta',
        'exploratory': False,
        'criteria': [{
            'criterion_id': 'sandbox',
            'kind': 'test_command',
            'params': {'command': 'skill-sandbox:internal'},
        }],
        'budget_envelope': dict(_META_BUDGET),
        'status': 'draft',
        'created_at': ts,
    }, user_id=user_id)
    plan = _teach_plan(goal, user_id)
    episode = open_episode(goal_id=goal['goal_id'], plan_id=plan['plan_id'],
                           selected_path_id='teach',
                           task_id=f"teach-{skill['skill_id']}",
                           user_id=user_id, now=ts)
    verdict = {
        'outcome': 'success' if receipt.get('verdict') == 'pass' else 'failure',
        'assessed_by': 'checker',
        'criterion_results': [{
            'criterion_id': 'sandbox',
            'outcome': 'pass' if receipt.get('verdict') == 'pass' else 'fail',
            'evidence_ref': str(receipt.get('output') or '')[-500:],
        }],
    }
    settled = settle_episode(
        episode['episode_id'], user_id=user_id, status=verdict['outcome'],
        actuals={'tokens': 0, 'wall_clock_seconds': 0, 'tool_calls': 0},
        verdict=verdict, now=ts)
    result = {'skill': skill, 'episode': settled, 'verdict': verdict,
              'mutation': None}
    if receipt.get('verdict') == 'pass':
        result['mutation'] = propose_skill(
            skill['skill_id'], user_id=user_id,
            evidence_episode_ids=[episode['episode_id']], now=ts)
        from .skill_forge import get_skill
        result['skill'] = get_skill(skill['skill_id'], user_id=user_id)
    return result


def _teach_plan(goal: dict, user_id) -> dict:
    """The teach flow's single internal path, saved confirmed: there is no
    selection to authorize (one fixed forge-and-verify path), and the user's
    teach request is itself the explicit initiating act. The gate's human
    confirmation for the skill happens later, unchanged."""
    from .planner import build_plan
    from .goals import save_plan
    plan = build_plan(goal, [{
        'path_id': 'teach',
        'title': 'forge and sandbox-verify the taught skill',
        'stages': [{'kind': 'forge', 'detail': 'author + sandbox'}],
    }], user_id=user_id)
    return save_plan({**plan, 'status': 'confirmed'}, user_id=user_id)
