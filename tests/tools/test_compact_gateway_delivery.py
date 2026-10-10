"""Opt-in gateway text views preserve canonical values through real budgeting."""
import json
import pytest
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tools.result_surfaces import project_model_source

pytestmark = pytest.mark.unit


def test_multiline_gateway_projection_is_live_and_original_json_is_retained(monkeypatch):
    value = {'status': 'partial_failure', 'warnings': ['one rejected call'], 'results': [
        {'name': 'read_files', 'call_id': 'one', 'status': 'done', 'duration': 120,
         'source': 'execute_tools', 'approval': {'required': False, 'status': 'not_required'},
         'output': '# Complete\nExact 世界\nTAIL', 'output_truncated': False,
         'output_bytes': 80, 'raw_output_bytes': 80, 'resultId': 'capture'},
        {'name': 'write_file', 'call_id': 'two', 'status': 'rejected',
         'approval': {'required': True, 'status': 'rejected'},
         'error': 'No write performed\nOwner refused', 'future': {'important': True}}]}
    raw = json.dumps(value, ensure_ascii=False)
    saved = []
    def persist(canonical, *, user_id):
        saved.append((user_id, canonical.text()))
        return 'tool-result:' + 'd' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2('execute_tools', raw, user_id=42)
    delivery = split_tool_result_delivery(envelope)
    assert '# Complete\nExact 世界\nTAIL' in delivery.model_text
    assert 'No write performed\nOwner refused' in delivery.model_text
    assert 'partial_failure' in delivery.model_text and 'rejected' in delivery.model_text
    assert '"required":true' in delivery.model_text
    assert '"duration"' not in delivery.model_text
    assert saved == [(42, raw)]
    assert json.loads(envelope.canonical.text()) == value
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']


def test_gateway_formatting_cannot_escape_result_budget(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._budget.TOOL_RESULT_V2_MAX_TOKENS', 80)
    value = {'status': 'ok', 'warnings': [], 'results': [
        {'name': 'app', 'call_id': 'one', 'status': 'done', 'output': 'kept evidence\n'*5000}]}
    raw = json.dumps(value)
    envelope = budget_tool_result_v2('execute_tools', raw, user_id=0)
    evidence = split_tool_result_delivery(envelope).evidence
    assert evidence['truncated']
    assert evidence['retentionCoverage'] == 'unavailable'
    assert json.loads(envelope.canonical.text()) == value


def test_gateway_multimodal_result_remains_in_existing_media_lane():
    raw = json.dumps({'status': 'ok', 'warnings': [], 'results': [
        {'name': 'inspect_image', 'call_id': 'image', 'status': 'done', 'output': [
            {'type': 'text', 'text': 'caption'},
            {'type': 'image_url', 'image_url': {'url': 'tofu-media:owned'}}]}]})
    assert project_model_source('execute_tools', raw) == raw


def test_spawn_precision_and_duplicate_key_boundaries():
    raw = '{"status":"async_launched","swarm_key":"c","agents":[{"id":"a","objective":"echo","unknown":{"n":1.234567890123456789,"small":1e-999,"zero":-0}}]}'
    view = project_model_source('spawn_agents', raw)
    assert 'objective' not in view
    for token in ('1.234567890123456789', '1e-999', '-0'):
        assert token in view
    duplicate = raw.replace('"status":"async_launched"', '"status":"error","status":"async_launched"')
    assert project_model_source('spawn_agents', duplicate) == duplicate
