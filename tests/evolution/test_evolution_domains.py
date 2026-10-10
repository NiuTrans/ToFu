"""Research domain plugin: directions become goals, run records become verdicts."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[2] / 'contracts'
USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def test_research_goal_payload_validates_against_the_goal_contract():
    from lib.evolution.domains import research_goal_payload
    payload = research_goal_payload(
        'sparse attention for long context', goal_id='goal-r1',
        budget_envelope={'max_tokens': 100000,
                         'max_wall_clock_seconds': 7200,
                         'max_tool_calls': 200},
        lang='en', created_at=1)
    schema = json.loads((CONTRACTS / 'evolution_goal_v1.schema.json')
                        .read_text(encoding='utf-8'))
    jsonschema.validate(payload, schema)
    assert payload['domain'] == 'research'
    assert payload['criteria'][0]['kind'] == 'falsification_protocol'
    assert payload['exploratory'] is True


def test_research_record_maps_terminal_status_to_verdict():
    from lib.evolution.domains import verdict_from_research_record
    done = verdict_from_research_record({'status': 'done', 'task_id': 't1'})
    assert done['outcome'] == 'partial'
    assert done['criterion_results'][0]['outcome'] == 'unknown'
    assert done['assessed_by'] == 'checker'
    assert done['criterion_results'][0]['evidence_ref'] == 'research-run:t1'
    degraded = verdict_from_research_record({'status': 'degraded',
                                             'task_id': 't2'})
    assert degraded['outcome'] == 'partial'
    assert degraded['criterion_results'][0]['outcome'] == 'unknown'
    assert verdict_from_research_record(
        {'status': 'error', 'task_id': 't3'})['outcome'] == 'failure'
    assert verdict_from_research_record({'status': 'running'}) is None
    assert verdict_from_research_record(None) is None


def test_wrapped_research_run_settles_as_episode(store):
    """The plugin composes with the bridge: a research run is one episode."""
    from lib import evolution
    payload = evolution.research_goal_payload(
        'sparse attention', goal_id='goal-r2',
        budget_envelope={'max_tokens': 100, 'max_wall_clock_seconds': 10,
                         'max_tool_calls': 1}, created_at=1)
    evolution.admit_goal(payload, user_id=USER)
    plan = evolution.build_plan(evolution.get_goal('goal-r2', user_id=USER), [{
        'path_id': 'path-a', 'title': 'run the research recipe',
        'stages': [{'kind': 'recipe', 'detail': 'five-stage pipeline'}],
    }], user_id=USER)
    evolution.save_plan({**plan, 'status': 'confirmed'}, user_id=USER)
    task = {'id': 'research-task-1', '_userId': USER, 'status': 'done',
            'created_at': 10.0, 'finished_at': 40.0,
            'usage': {'input_tokens': 100, 'output_tokens': 50},
            'toolRounds': []}
    settled = evolution.record_terminal_task(
        task, goal_id='goal-r2', plan_id=plan['plan_id'],
        selected_path_id='path-a', user_id=USER,
        verdict=evolution.verdict_from_research_record(
            {'status': 'degraded', 'task_id': 'research-task-1'}))
    assert settled['status'] == 'partial'
    assert settled['actuals']['tokens'] == 150
    assert settled['verdict']['criterion_results'][0][
        'evidence_ref'] == 'research-run:research-task-1'


def test_only_explicit_legacy_policy_can_map_pipeline_done_to_success():
    from lib.evolution.domains import verdict_from_research_record, research_goal_payload
    record = {'status': 'done', 'task_id': 'historical'}
    assert verdict_from_research_record(record, acceptance_version=0)['outcome'] == 'success'
    assert research_goal_payload('legacy', goal_id='g', budget_envelope={},
                                 acceptance_version=0)['exploratory'] is False
    with pytest.raises(ValueError):
        verdict_from_research_record(record, acceptance_version=2)
