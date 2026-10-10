"""Skill forge (R1): executable skills authored, tested, installed, composed.

Responsibility: turn a capability gap into an executable skill — draft code
plus its own contract tests, run those tests in a bounded sandbox, and route
the result through the evolution gate into `lib/skills/`. Entry points:
``forge_draft`` / ``sandbox_run`` / ``propose_skill`` / ``install_skill`` /
``get_skill`` / ``list_forged_skills``. Dependencies: the gate, the skill
contract, `lib/skills/` installer (install only; the forge never bypasses
it), and an injectable code author.

Root-level honesty rules, pinned by tests:

- The author is an injectable seam. The default author issues one bounded
  chat turn through the public task-engine entry; the forge itself never
  opens an LLM loop.
- The sandbox is a bounded subprocess running the skill's own tests — a
  verification gate, not a security boundary. Authority comes from human
  confirmation through the gate and from `lib/skills/` policy at install.
- Composition is first-class: ``uses`` must reference installed skills and
  the composition graph stays acyclic, so capability grows combinatorially.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import time
import uuid

from lib.log import get_logger

from .contracts import (
    EvolutionStateError,
    validate_skill,
)
from .gate import list_mutations, propose_mutation
from .records import (
    NAMESPACE_SKILLS,
    entity_key,
    get_record,
    list_records,
    put_record,
)

logger = get_logger(__name__)

MAX_SKILLS = 64
SANDBOX_TIMEOUT_S = 120
POLICY_PREFIX = 'planner-policy'
_CODE_FENCE_RE = re.compile(r'```python\s*\n(.*?)```', re.DOTALL)

_POLICY_FIXTURE_CANDIDATE = {
    'path_id': 'fixture', 'title': 'fixture path',
    'stages': [{'kind': 'execute', 'detail': 'd'}],
    'resource_refs': [],
    'cost_estimate': {
        'expected_tokens': 100, 'expected_wall_clock_seconds': 10,
        'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
        'expected_information_gain': 0.5, 'expected_utility': 0.5,
        'priors_source': 'exploratory_flat'},
    'utility_over_cost': 1.0,
}


def _policy_fixture_check(code: str) -> tuple[bool, str]:
    """A planner policy must export score(candidate, context) -> real float."""
    namespace: dict = {}
    try:
        exec(compile(code, '<planner-policy>', 'exec'), namespace)
        score = namespace.get('score')
        if not callable(score):
            return False, 'policy exports no callable score'
        value = score(dict(_POLICY_FIXTURE_CANDIDATE),
                      {'domain': 'fixture', 'budget_envelope': {},
                       'goal_statement': ''})
        if not isinstance(value, (int, float)) or value != value:
            return False, 'score() did not return a real number'
    except Exception as exc:
        # The candidate policy raised; the (False, reason) receipt carries it.
        logger.debug('[Evolution:SkillForge] policy fixture failed: %s', exc)
        return False, f'policy fixture failed: {exc}'[:200]
    return True, ''


def _skill_key(skill_id: str, user_id) -> str:
    return entity_key(skill_id, user_id, kind='skill')


def _save(skill: dict, *, user_id) -> dict:
    skill['updated_at'] = int(time.time())
    put_record(NAMESPACE_SKILLS, _skill_key(skill['skill_id'], user_id),
               skill, kind='skill')
    return skill


def default_author(request: dict) -> dict:
    """One bounded chat turn authoring code+tests; the task engine owns the
    loop. Parse two ```python fences: implementation, then tests.

    Owner-taught material shapes the draft: ``knowledge`` is injected as
    grounding text and every ``examples`` entry becomes a mandatory tested
    behavior — taught knowledge enters the system only through verification.
    """
    from lib.tasks_pkg.entry import run_chat_sync
    knowledge = str(request.get('knowledge') or '')[:8000]
    examples = [str(e)[:500] for e in (request.get('examples') or [])][:8]
    prompt = (
        'Write a small python skill and its pytest contract tests.\n'
        f"Name: {request.get('name')}\n"
        f"Purpose: {request.get('description')}\n"
        + (f'Grounding knowledge from the owner:\n{knowledge}\n'
           if knowledge else '')
        + ('Mandatory behaviors — the tests MUST cover each of these:\n'
           + '\n'.join(f'- {e}' for e in examples) + '\n'
           if examples else '')
        + 'Rules: pure python stdlib only; one module; functions with clear '
        'names; tests import `skill_impl`. Reply with exactly two fenced '
        'blocks: ```python implementation ``` then ```python tests ```.')
    result = run_chat_sync(
        [{'role': 'user', 'content': prompt}],
        user_id=int(request['user_id']), timeout_s=600.0)
    blocks = _CODE_FENCE_RE.findall(result.content or '')
    if len(blocks) < 2:
        raise EvolutionStateError(
            'author returned no code/tests fences; the draft is discarded')
    return {'code': blocks[0].strip(), 'tests': blocks[1].strip()}


def forge_draft(request: dict, *, user_id, author=None, now: int | None = None
                ) -> dict:
    """Draft one skill artifact (status ``draft``) from an author."""
    name = str(request.get('name') or '').strip()
    description = str(request.get('description') or '').strip()
    if not name or not description:
        raise EvolutionStateError('a skill request needs name and description')
    write = author or default_author
    produced = write({**request, 'user_id': user_id})
    ts = int(now if now is not None else time.time())
    skill = validate_skill({
        'schema_version': 1,
        'skill_id': f'skill-{uuid.uuid4().hex[:12]}',
        'name': name[:200],
        'description': description[:1000],
        'language': 'python',
        'code': str(produced.get('code') or ''),
        'tests': str(produced.get('tests') or ''),
        'uses': [str(u)[:160] for u in (request.get('uses') or [])][:8],
        'gap_id': (str(request['gap_id'])[:160]
                   if request.get('gap_id') else None),
        'status': 'draft',
        'sandbox_receipt': None,
        'lineage': [str(e)[:160] for e in (request.get('lineage') or [])][:8],
        'created_at': ts,
        'updated_at': ts,
    })
    _check_composition(skill, user_id=user_id)
    return _save(skill, user_id=user_id)


def _check_composition(skill: dict, *, user_id) -> None:
    for used in skill.get('uses') or []:
        if used == skill['skill_id']:
            raise EvolutionStateError('a skill cannot compose itself')
        target = get_record(NAMESPACE_SKILLS, _skill_key(used, user_id))
        if target is None or target.get('status') != 'installed':
            raise EvolutionStateError(
                f'composed skill {used!r} is not installed')


def sandbox_run(skill_id: str, *, user_id, runner=None,
                now: int | None = None) -> dict:
    """Run the skill's own tests in a bounded subprocess; receipt on record."""
    skill = get_record(NAMESPACE_SKILLS, _skill_key(skill_id, user_id))
    if skill is None:
        raise EvolutionStateError('unknown skill for this owner')
    if skill.get('status') not in ('draft', 'rejected'):
        raise EvolutionStateError(
            f"sandbox runs from draft/rejected, not {skill.get('status')!r}")
    execute = runner or _pytest_subprocess
    verdict, output = execute(skill['code'], skill['tests'])
    if verdict == 'pass' and skill['name'].startswith(POLICY_PREFIX):
        ok, detail = _policy_fixture_check(skill['code'])
        if not ok:
            verdict, output = 'fail', detail
    skill = dict(skill)
    skill['sandbox_receipt'] = {'verdict': verdict, 'output': output[:4000]}
    skill['status'] = 'sandboxed' if verdict == 'pass' else 'rejected'
    return _save(validate_skill(skill), user_id=user_id)


def _pytest_subprocess(code: str, tests: str) -> tuple[str, str]:
    import os
    env = dict(os.environ)
    # The sandbox must measure the skill's tests, not the host's plugin soup
    # (a GL-less vispy import elsewhere in the environment must not matter).
    env['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
    with tempfile.TemporaryDirectory(prefix='evo-skill-') as tmp:
        from pathlib import Path
        Path(tmp, 'skill_impl.py').write_text(code, encoding='utf-8')
        Path(tmp, 'test_skill_impl.py').write_text(tests, encoding='utf-8')
        try:
            completed = subprocess.run(
                [sys.executable, '-m', 'pytest', '-q', '-x',
                 '-p', 'no:cacheprovider', 'test_skill_impl.py'],
                cwd=tmp, capture_output=True, text=True,
                timeout=SANDBOX_TIMEOUT_S, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 'fail', f'sandbox error: {exc}'[:4000]
    output = ((completed.stdout or '') + (completed.stderr or ''))[-4000:]
    return ('pass' if completed.returncode == 0 else 'fail'), output


def propose_skill(skill_id: str, *, user_id, evidence_episode_ids: list[str],
                  now: int | None = None) -> dict:
    """Route a sandboxed skill into the gate; evidence is mandatory."""
    skill = get_record(NAMESPACE_SKILLS, _skill_key(skill_id, user_id))
    if skill is None or skill.get('status') != 'sandboxed':
        raise EvolutionStateError(
            'only sandbox-passed skills may enter the gate')
    mutation = propose_mutation(
        'skill_candidate', {'name': skill['name']},
        evidence_episode_ids, user_id=user_id, now=now)
    skill = dict(skill, status='proposed')
    _save(validate_skill(skill), user_id=user_id)
    return mutation


def install_skill(skill_id: str, *, user_id, project_path: str | None = None,
                  installer=None) -> dict:
    """Install through `lib/skills/` only after the gate activated the
    matching skill_candidate mutation."""
    skill = get_record(NAMESPACE_SKILLS, _skill_key(skill_id, user_id))
    if skill is None or skill.get('status') != 'proposed':
        raise EvolutionStateError(
            'install requires a proposed skill past sandbox and gate proposal')
    activated = any(
        m.get('kind') == 'skill_candidate' and m.get('status') == 'active'
        and (m.get('payload') or {}).get('name') == skill['name']
        for m in list_mutations(user_id=user_id))
    if not activated:
        raise EvolutionStateError(
            'no active skill_candidate mutation for this skill; the gate '
            'decides installation')
    install = installer or _install_via_lib_skills
    install(skill, project_path=project_path, user_id=user_id)
    skill = dict(skill, status='installed')
    if skill['name'].startswith(POLICY_PREFIX):
        from .records import NAMESPACE_POLICY
        put_record(NAMESPACE_POLICY,
                   entity_key('active', user_id, kind='planner policy'),
                   {'skill_name': skill['name'], 'code': skill['code'],
                    'installed_at': int(time.time())},
                   kind='planner policy')
    return _save(validate_skill(skill), user_id=user_id)


def forge_policy(description: str, *, user_id, author=None,
                 now: int | None = None) -> dict:
    """Forge a planner-policy program (R3): code exporting
    ``score(candidate, context) -> float``, evolved as an ordinary skill."""
    slug = re.sub(r'[^a-z0-9]+', '-', (description or '').lower())[:32] \
        .strip('-') or 'unnamed'
    write = author or _default_policy_author
    return forge_draft({
        'name': f'{POLICY_PREFIX}-{slug}',
        'description': f'Planner policy: {description}',
    }, user_id=user_id, author=write, now=now)


def _default_policy_author(request: dict) -> dict:
    from lib.tasks_pkg.entry import run_chat_sync
    prompt = (
        'Write a planner policy as python and its pytest tests.\n'
        f"Goal: {request.get('description')}\n"
        'The module must export `def score(candidate, context) -> float` '
        'where candidate has keys path_id/title/stages/resource_refs/'
        'cost_estimate/utility_over_cost and context has domain/'
        'budget_envelope/goal_statement. Higher is better. Pure stdlib, '
        'deterministic, no I/O. Tests import `skill_impl`. Reply with '
        'exactly two ```python fences: implementation, then tests.')
    result = run_chat_sync(
        [{'role': 'user', 'content': prompt}],
        user_id=int(request['user_id']), timeout_s=600.0)
    blocks = _CODE_FENCE_RE.findall(result.content or '')
    if len(blocks) < 2:
        raise EvolutionStateError(
            'policy author returned no code/tests fences; draft discarded')
    return {'code': blocks[0].strip(), 'tests': blocks[1].strip()}


def _install_via_lib_skills(skill: dict, *, project_path, user_id) -> None:
    from lib.skills import install_skill_package
    manifest = (
        '---\n'
        f"name: {skill['name']}\n"
        f"description: {skill['description']}\n"
        '---\n\n'
        f"# {skill['name']}\n\n"
        'Forged by the self-evolution module; verified by its own contract '
        'tests in a bounded sandbox. Implementation: `skill_impl.py`.\n')
    with tempfile.TemporaryDirectory(prefix='evo-skill-pkg-') as tmp:
        from pathlib import Path
        root = Path(tmp, skill['name'].replace(' ', '-'))
        root.mkdir()
        (root / 'SKILL.md').write_text(manifest, encoding='utf-8')
        (root / 'skill_impl.py').write_text(skill['code'], encoding='utf-8')
        (root / 'test_skill_impl.py').write_text(skill['tests'],
                                                 encoding='utf-8')
        install_skill_package(str(root), scope='global',
                              owner_user_id=user_id, overwrite=True)


def get_skill(skill_id: str, *, user_id) -> dict | None:
    return get_record(NAMESPACE_SKILLS, _skill_key(skill_id, user_id))


def list_forged_skills(*, user_id, status: str | None = None) -> list[dict]:
    skills = list_records(NAMESPACE_SKILLS, user_id, kind='skill',
                          limit=MAX_SKILLS)
    if status:
        skills = [s for s in skills if s.get('status') == status]
    return skills
