"""Evolution guidance: learned memory renders only for bound owner tasks."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _seed(store):
    from lib import evolution
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': 'goal-1',
        'statement': 'reproduce transformer baseline', 'domain': 'code',
        'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': 'true'}}],
        'budget_envelope': {'max_tokens': 100, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 1},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    evolution.save_plan({
        'schema_version': 1, 'plan_id': 'plan-1', 'goal_id': 'goal-1',
        'plan_revision': 1,
        'candidates': [{
            'path_id': 'path-a', 'title': 'Adapt the reference repo',
            'stages': [{'kind': 'index', 'detail': 'd'},
                       {'kind': 'execute', 'detail': 'd'}],
            'resource_refs': [],
            'cost_estimate': {
                'expected_tokens': 10, 'expected_wall_clock_seconds': 10,
                'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                'expected_information_gain': 0.5, 'expected_utility': 0.5,
                'priors_source': 'exploratory_flat'},
            'utility_over_cost': 1.0}],
        'selected_path_id': 'path-a', 'selection_rationale': 'r',
        'requires_confirmation': True, 'status': 'confirmed',
        'created_at': 1}, user_id=USER)
    from lib.evolution import records
    records.put_record(records.NAMESPACE_STRATEGIES, f'{USER}:tmpl-1', {
        'schema_version': 1, 'template_id': 'tmpl-1', 'domain': 'code',
        'stages': [{'kind': 'index', 'detail': 'd'},
                   {'kind': 'execute', 'detail': 'd'}],
        'applicability_terms': ['reproduce', 'transformer', 'baseline'],
        'lineage': ['e1'], 'samples': 2, 'success_rate': 0.75,
        'created_at': 1, 'updated_at': 1}, kind='strategy')
    records.put_record(records.NAMESPACE_ANTI_PATTERNS, f'{USER}:anti-1', {
        'schema_version': 1, 'failure_evidence_version': 1, 'pattern_id': 'anti-1', 'domain': 'code',
        'signature': 'failed:train', 'advice': 'never train without seeded data',
        'lineage': ['e1', 'e2'], 'samples': 2,
        'created_at': 1, 'updated_at': 1}, kind='anti-pattern')


def _task(binding=True):
    config = {}
    if binding:
        config['evolution'] = {'goal_id': 'goal-1', 'plan_id': 'plan-1',
                               'episode_id': 'ep-1'}
    return {'id': 'task-1', '_userId': USER, 'config': config}


def test_bound_task_receives_path_strategies_and_antipatterns(store):
    from lib.evolution.guidance import render_guidance
    _seed(store)
    content = render_guidance(_task(), user_id=USER)
    assert 'Adapt the reference repo' in content
    assert 'index → execute' in content
    assert '75% over 2' in content
    assert 'never train without seeded data' in content


def test_unbound_or_foreign_tasks_render_nothing(store):
    from lib.evolution.guidance import render_guidance
    _seed(store)
    assert render_guidance(_task(binding=False), user_id=USER) == ''
    assert render_guidance(_task(), user_id=99) == ''


def test_composer_registers_and_acquires_the_block(store):
    from lib.tasks_pkg.context_composer.registry import injection_spec
    spec = injection_spec('evolution_guidance')
    assert spec.max_tokens == 800
    assert spec.required is False
    _seed(store)
    from lib.tasks_pkg.context_composer._providers import _evolution_guidance

    class Request:
        task = _task()
        user_id = USER

    content, reason = _evolution_guidance(Request())
    assert 'Adapt the reference repo' in content and reason == ''

    class ForeignRequest:
        task = _task()
        user_id = 99

    assert _evolution_guidance(ForeignRequest()) == ('', 'no_evolution_binding')


def test_active_prompt_guidance_is_applied_and_rollback_removes_it(store):
    from lib.evolution import records
    from lib.evolution.guidance import render_guidance
    _seed(store)
    mutation={'mutation_id':'prompt-1','kind':'prompt_variant','status':'active',
              'payload':{'text':'Reproduce the failing case before changing implementation.'}}
    records.put_record(records.NAMESPACE_MUTATIONS,f'{USER}:prompt-1',mutation,kind='mutation')
    content=render_guidance(_task(),user_id=USER)
    assert mutation['payload']['text'] in content
    assert 'index: d' in content and 'execute: d' in content
    records.put_record(records.NAMESPACE_MUTATIONS,f'{USER}:prompt-1',dict(mutation,status='rolled_back'),kind='mutation')
    assert mutation['payload']['text'] not in render_guidance(_task(),user_id=USER)
    assert render_guidance(_task(binding=False),user_id=USER)==''


def test_user_shelf_is_discoverable_only_in_its_bound_goal(store,monkeypatch):
    from lib.evolution.guidance import render_guidance
    _seed(store)
    from lib.evolution import library
    monkeypatch.setattr('lib.evolution.runs.get_run',lambda rid,*,user_id:{'run_id':rid,'status':'executing','task_id':'task-1'} if user_id==USER else None)
    library.save_material('run-a',{'kind':'web','title':'Owner supplied reference','url':'https://example.org/reference'},user_id=USER)
    task=_task();task['config']['evolution']['run_id']='run-a'
    content=render_guidance(task,user_id=USER)
    assert 'Owner supplied reference' in content and 'action=list' in content
    task['config']['evolution']['run_id']='run-b'
    assert 'Owner supplied reference' not in render_guidance(task,user_id=USER)


def test_legacy_failure_advice_is_absent_from_actual_executor_guidance(store):
    from lib.evolution.guidance import render_guidance
    from lib.evolution.records import NAMESPACE_ANTI_PATTERNS
    _seed(store)
    store.rows[NAMESPACE_ANTI_PATTERNS, f'{USER}:anti-1'].pop('failure_evidence_version')
    content = render_guidance(_task(), user_id=USER)
    assert 'Adapt the reference repo' in content
    assert 'never train without seeded data' not in content
