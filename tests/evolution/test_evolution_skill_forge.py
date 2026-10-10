"""Skill forge (R1): authored code proves itself in the sandbox, then the gate."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12

GOOD_CODE = '''def normalize_title(title):
    """Lowercase, collapse whitespace, strip punctuation noise."""
    import re
    return re.sub(r'\\s+', ' ', (title or '').lower()).strip(' .')
'''

GOOD_TESTS = '''from skill_impl import normalize_title


def test_normalize_title_collapses_case_and_space():
    assert normalize_title('  Sparse   Attention. ') == 'sparse attention'


def test_normalize_title_tolerates_empty():
    assert normalize_title(None) == ''
'''

BAD_TESTS = '''from skill_impl import normalize_title


def test_intentionally_failing():
    assert normalize_title('A') == 'B'
'''


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settled_evidence(store, episode_id='e1'):
    from lib import evolution
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': 'g', 'statement': 'goal',
        'domain': 'meta', 'exploratory': False,
        'criteria': [{'criterion_id': 'c', 'kind': 'test_command',
                      'params': {'command': 'true'}}],
        'budget_envelope': {'max_tokens': 10, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 1},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    from lib.evolution import records
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': 'g',
        'plan_id': 'p', 'selected_path_id': 'a', 'exploratory': False,
        'task_id': 't', 'status': 'success',
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 1},
        'verdict': {'outcome': 'success', 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')
    return episode_id


def _forge(store, code=GOOD_CODE, tests=GOOD_TESTS, **request_overrides):
    from lib import evolution
    request = {'name': 'paper-title-normalizer',
               'description': 'normalize paper titles for dedup'}
    request.update(request_overrides)
    return evolution.forge_draft(
        request, user_id=USER,
        author=lambda req: {'code': code, 'tests': tests})


def test_forge_draft_shape_and_bounds(store):
    from lib import evolution
    skill = _forge(store)
    assert skill['status'] == 'draft'
    assert skill['language'] == 'python'
    assert skill['sandbox_receipt'] is None
    with pytest.raises(evolution.EvolutionContractError):
        _forge(store, code='x' * 17000)
    with pytest.raises(evolution.EvolutionStateError):
        _forge(store, name='')


def test_sandbox_executes_real_tests_and_records_the_receipt(store):
    from lib import evolution
    skill = _forge(store)
    passed = evolution.sandbox_run(skill['skill_id'], user_id=USER)
    assert passed['status'] == 'sandboxed'
    assert passed['sandbox_receipt']['verdict'] == 'pass'
    assert '2 passed' in passed['sandbox_receipt']['output']

    failing = _forge(store, tests=BAD_TESTS)
    rejected = evolution.sandbox_run(failing['skill_id'], user_id=USER)
    assert rejected['status'] == 'rejected'
    assert rejected['sandbox_receipt']['verdict'] == 'fail'


def test_full_pipeline_goes_through_the_gate_to_install(store, monkeypatch):
    from lib import evolution
    evidence = _settled_evidence(store)
    skill = _forge(store)
    evolution.sandbox_run(skill['skill_id'], user_id=USER)

    installed_packages = []
    monkeypatch.setattr(
        'lib.evolution.skill_forge._install_via_lib_skills',
        lambda skill, *, project_path, user_id: installed_packages.append(
            skill['name']))

    with pytest.raises(evolution.EvolutionStateError):
        evolution.install_skill(skill['skill_id'], user_id=USER)

    mutation = evolution.propose_skill(skill['skill_id'], user_id=USER,
                                       evidence_episode_ids=[evidence])
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    evolution.build_holdout_suite(user_id=USER)
    evolution.run_regression(mutation, user_id=USER,
                             probe=lambda m, item: 'pass')
    evolution.activate_mutation(mutation['mutation_id'], user_id=USER)
    installed = evolution.install_skill(skill['skill_id'], user_id=USER)
    assert installed['status'] == 'installed'
    assert installed_packages == ['paper-title-normalizer']


def test_taught_knowledge_and_examples_reach_the_author(store, monkeypatch):
    """The teach flow: owner knowledge grounds the draft and every example
    becomes a mandatory tested behavior in the authoring prompt."""
    from lib import evolution
    from lib.tasks_pkg import entry
    captured = {}

    class FakeResult:
        content = ('```python\ndef submit():\n    return "ok"\n```\n'
                   '```python\nfrom skill_impl import submit\n\n\n'
                   'def test_submit():\n    assert submit() == "ok"\n```')

    def fake_chat(messages, *, user_id, timeout_s):
        captured['prompt'] = messages[0]['content']
        return FakeResult()

    monkeypatch.setattr(entry, 'run_chat_sync', fake_chat)
    skill = evolution.forge_draft({
        'name': 'hop-training',
        'description': 'submit GPU jobs to the HOP cluster',
        'knowledge': 'Use the hop MCP submit_job tool; queue=gpu-a100; '
                     'images live under /shared/images.',
        'examples': ['submit a 4-GPU training job',
                     'poll job status until terminal'],
        'gap_id': 'gap-1',
    }, user_id=USER)  # author=None → default_author with the teach material
    prompt = captured['prompt']
    assert 'hop MCP submit_job' in prompt
    assert 'queue=gpu-a100' in prompt
    assert 'submit a 4-GPU training job' in prompt
    assert 'poll job status until terminal' in prompt
    assert 'MUST cover' in prompt
    assert skill['status'] == 'draft'
    sandboxed = evolution.sandbox_run(skill['skill_id'], user_id=USER)
    assert sandboxed['status'] == 'sandboxed'


def test_composition_requires_installed_dependencies(store):
    from lib import evolution
    with pytest.raises(evolution.EvolutionStateError):
        _forge(store, uses=['skill-not-installed'])
    with pytest.raises(evolution.EvolutionStateError):
        _forge(store, uses=['self-reference'], name='self-reference')
