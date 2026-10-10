"""Teach channel: the meta-goal episode is the evidence; surfaces share one flow."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def test_teach_pass_flow_evidence_gated_mutation(store):
    from lib import evolution
    result = evolution.teach(
        'hop-training', 'submit GPU jobs to the HOP cluster',
        knowledge='use the hop MCP submit_job tool; queue=gpu-a100',
        examples=['submit a 4-GPU job', 'poll until terminal'],
        code='def submit():\n    return "submitted"\n',
        tests=('from skill_impl import submit\n\n\n'
               'def test_submit():\n    assert submit() == "submitted"\n'),
        user_id=USER, now=100)
    assert result['skill']['status'] == 'proposed'
    assert result['verdict']['outcome'] == 'success'
    episode = result['episode']
    assert episode['status'] == 'success'
    assert episode['verdict']['assessed_by'] == 'checker'
    assert episode['exploratory'] is False
    mutation = result['mutation']
    assert mutation['kind'] == 'skill_candidate'
    # The gate's evidence rule is met by the real sandbox run, not prose.
    assert mutation['evidence_episode_ids'] == [episode['episode_id']]


def test_teach_failure_records_diagnosis_without_mutation(store):
    from lib import evolution
    result = evolution.teach(
        'broken-skill', 'will not pass',
        code='def f():\n    return 1\n',
        tests=('from skill_impl import f\n\n\n'
               'def test_f():\n    assert f() == 2\n'),
        user_id=USER, now=100)
    assert result['skill']['status'] == 'rejected'
    assert result['episode']['status'] == 'failure'
    assert result['mutation'] is None
    assert evolution.list_forged_skills(
        user_id=USER, status='installed') == []


def test_teach_tool_definition_is_a_valid_prompt_artifact(store):
    from lib.evolution.teach import TEACH_TOOL_DEFINITION as tool
    fn = tool['function']
    assert fn['name'] == 'evolution_teach'
    assert 'HOP MCP' in fn['description']
    assert 'Do NOT use' in fn['description']
    params = fn['parameters']
    assert params['required'] == ['name', 'description', 'knowledge',
                                  'examples']
    assert params['additionalProperties'] is False


def test_system_snapshot_perceives_all_modules(store):
    from lib import evolution
    evolution.teach(
        'hop-training', 'submit GPU jobs',
        code='def submit():\n    return "ok"\n',
        tests=('from skill_impl import submit\n\n\n'
               'def test_ok():\n    assert submit() == "ok"\n'),
        user_id=USER, now=100)
    evolution.run_maintenance_cycle(user_id=USER)
    snapshot = evolution.system_snapshot(user_id=USER)
    assert snapshot['episodes']['settled'] == 1
    assert 'meta' in snapshot['domains']
    assert snapshot['domains']['meta']['samples'] == 1
    assert snapshot['capability_gaps'] == []
    assert snapshot['anti_patterns'] == 0
    assert snapshot['installed_skills'] == []
    assert snapshot['pending_hypotheses'] == 0
    assert isinstance(snapshot['active_mutations'], list)
