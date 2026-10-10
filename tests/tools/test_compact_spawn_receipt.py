"""Native launch receipts pay for identities once, not repeated task inputs."""
import json
import pytest
from lib.tools.result_surfaces import project_model_source
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tasks_pkg.handlers.misc._agents import _build_spawn_post_build

pytestmark = pytest.mark.unit


def launch():
    return {'status': 'async_launched', 'swarm_key': 'conv-owner', 'swarm_id': 'wave',
            'agents': [{'id': 'audit', 'role': 'coder', 'objective': '任务\n' * 1000,
                        'required_for_completion': True, 'future_field': {'mode': 'safe'}}],
            'orchestration': {'mode': 'read_only', 'maxConcurrentAgents': 3},
            'message': 'Continue useful work; poll only when waiting.',
            'deduplicated': [{'id': 'previous', 'objective': 'duplicate diagnostic'}],
            'unknown': {'opaque': 'token', 'count': 9007199254740993}}


def test_compact_spawn_preserves_every_nonobjective_field_and_unknowns():
    value = launch()
    raw = json.dumps(value, ensure_ascii=False)
    view = json.loads(project_model_source('spawn_agents', raw))
    assert 'objective' not in view['agents'][0]
    expected = {**value, 'agents': [{k: v for k, v in a.items() if k != 'objective'}
                                  for a in value['agents']]}
    assert view == expected
    assert len(project_model_source('spawn_agents', raw).encode()) < len(raw.encode()) / 4
    assert project_model_source('custom_tool', raw) == raw


@pytest.mark.parametrize('value', [
    {'status': 'error', 'error': {'code': 'capacity', 'retryable': True}},
    {'status': 'async_launched', 'agents': [{'id': 'a', 'objective': 'x'}]},
    {'status': 'async_launched', 'swarm_key': 'c', 'agents': []},
    {'status': 'async_launched', 'swarm_key': 'c', 'agents': [{'id': 'a'}]},
])
def test_unrecognized_or_failed_spawn_never_reinterpreted(value):
    raw = json.dumps(value)
    assert project_model_source('spawn_agents', raw) == raw


def test_frontend_full_launch_metadata_and_canonical_are_separate_from_model(monkeypatch):
    value = launch()
    raw = json.dumps(value, ensure_ascii=False)
    meta = {}
    _build_spawn_post_build()(meta, raw, {})
    assert meta['swarmLaunchReceipt'] == {
        'status': 'async_launched', 'agents': value['agents'],
    }
    saved = []
    def persist(canonical, *, user_id):
        saved.append((user_id, canonical.text()))
        return 'tool-result:' + 'a' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2('spawn_agents', raw, user_id=42)
    delivery = split_tool_result_delivery(envelope)
    assert saved == [(42, raw)]
    assert 'objective' not in json.loads(delivery.model_text)['agents'][0]
    assert json.loads(envelope.canonical.text()) == value
    assert delivery.evidence['artifactRef'] == 'tool-result:' + 'a' * 64
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert delivery.evidence['retentionCoverage'] == 'complete'
    assert not delivery.evidence['truncated']
    assert meta['swarmLaunchReceipt']['agents'][0]['objective'] == value['agents'][0]['objective']


def test_compact_spawn_still_obeys_output_budget(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._budget.TOOL_RESULT_V2_MAX_TOKENS', 60)
    value = launch()
    value['agents'][0]['future_field'] = 'still real data\n' * 4000
    raw = json.dumps(value)
    envelope = budget_tool_result_v2('spawn_agents', raw, user_id=0)
    evidence = split_tool_result_delivery(envelope).evidence
    assert evidence['truncated']
    assert evidence['retentionCoverage'] == 'unavailable'
    assert json.loads(envelope.canonical.text()) == value


def test_spawn_errors_do_not_create_frontend_launch_metadata():
    meta = {}
    _build_spawn_post_build()(meta, '{"status":"error","agents":[]}', {})
    assert not meta
