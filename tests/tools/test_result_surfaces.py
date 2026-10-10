"""Unified retained result observations and opt-in report delivery contracts."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib.tools.result_surfaces import (
    audit_model_result, inspect_result_document, inspect_swarm_notification,
    inspect_tool_round, project_model_source, result_policy,
)
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.agent_inbox import format_swarm_update

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_generated_surface_contract_is_current():
    result = subprocess.run([sys.executable, 'scripts/gen_tool_result_surfaces.py', '--check'],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_report_projection_preserves_metadata_and_original_program_value():
    report = {'found': True, 'agent_id': 'agent-1', 'status': 'ok',
              'agent_status': 'completed', 'final_answer': '# Conclusion\n\nVerified 世界.',
              'partial_output': '', 'error': '', 'tokens': 250, 'extra': {'cursor': 'next'}}
    raw = json.dumps(report, ensure_ascii=False)
    internal = budget_tool_result_v2('get_agent_result', raw, user_id=0)
    delivery = split_tool_result_delivery(internal)
    assert '# Conclusion\n\nVerified 世界.' in delivery.model_text
    assert '\\nVerified' not in delivery.model_text
    for key in report:
        assert key in delivery.model_text
    assert json.loads(internal.canonical.text()) == report
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']
    assert delivery.evidence['visibleBytes'] == len(delivery.model_text.encode())


def test_report_batch_preserves_running_unknown_and_failed_entries():
    value = {'status': 'ok', 'results': [
        {'agent_id': 'a', 'status': 'ok', 'agent_status': 'completed', 'result': 'Done\nEvidence'},
        {'agent_id': 'b', 'status': 'running', 'message': 'Still running'},
        {'agent_id': 'c', 'status': 'error', 'code': 'not_found'},
        {'agent_id': 'd', 'agent_status': 'failed', 'partial_output': 'Unfinished\nEvidence'},
    ]}
    text = project_model_source('get_agent_result', json.dumps(value))
    for index in range(4):
        assert f'results[{index}]' in text
    assert 'running' in text and 'not_found' in text and 'failed' in text
    assert 'partial_output' in text
    assert 'Done\nEvidence' in text


@pytest.mark.parametrize('name,value', [
    ('unknown_tool', {'final_answer': 'A\nB'}),
    ('search_tool_artifact', {'status': 'ok', 'final_answer': 'A\nB', 'nextCursor': 'opaque'}),
    ('get_agent_result', {'status': 'error', 'code': 'scope_denied', 'retryable': False}),
    ('get_agent_result', {'status': 'running', 'message': 'Wait'}),
    ('get_agent_result', ['unrecognized', {'result': 'a'}]),
])
def test_unreviewed_structures_and_typed_errors_pass_through(name, value):
    raw = json.dumps(value)
    assert project_model_source(name, raw) == raw


def test_report_formatting_cannot_bypass_existing_output_budget(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.compaction._budget.TOOL_RESULT_V2_MAX_TOKENS', 100)
    raw = json.dumps({'agent_id': 'a', 'status': 'ok', 'final_answer': 'report evidence\n' * 4000})
    internal = budget_tool_result_v2('get_agent_result', raw, user_id=0)
    evidence = split_tool_result_delivery(internal).evidence
    assert evidence['truncated'] and evidence['status'] == 'partial'
    assert json.loads(internal.canonical.text())['final_answer'].endswith('evidence\n')
    assert evidence['retentionCoverage'] == 'unavailable'  # ownerless, no artifact claim


def test_inspector_separates_model_blocks_from_frontend_text_and_metadata():
    row = {'toolName': 'inspect_image', 'toolCallId': 'same-id', 'status': 'done',
           'toolContent': 'Screenshot caption',
           'toolModelContent': [{'type': 'text', 'text': 'Screenshot caption'},
                                {'type': 'image_url', 'image_url': {'url': 'tofu-media:owned'}}],
           'results': [{'snippet': 'A separate thumbnail', 'source': 'image'}],
           'toolResultEvidence': {'artifactRef': 'tool-result:owned', 'rawBytes': 9000}}
    before = json.dumps(row)
    view = inspect_tool_round(row)
    assert view['model']['source'] == 'toolModelContent'
    assert 'image_url' in view['model']['text']
    assert view['frontend']['source'] == 'toolContent'
    assert view['frontend']['text'] == 'Screenshot caption'
    assert 'thumbnail' in view['frontend']['metadata']['text']
    assert view['canonical']['evidence']['artifactRef'] == 'tool-result:owned'
    assert json.dumps(row) == before


@pytest.mark.parametrize('value', ['', None, 0, False])
def test_explicit_empty_result_is_authoritative(value):
    view = inspect_tool_round({'toolContent': value, 'result': {'content': 'stale'},
                               'results': [{'content': 'unrelated'}]})
    assert view['model']['source'] == 'toolContent'
    assert view['model']['availability'] == 'retained'
    assert 'stale' not in view['model']['text']
    assert 'unrelated' not in view['model']['text']


def test_missing_delivery_and_deferred_frontend_preview_are_honest():
    view = inspect_tool_round({'toolName': 'fetch_url', 'results': [{'content': 'metadata'}],
                               '_snapshotToolResult': {'preview': 'visible prefix', 'chars': 9000}})
    assert view['model']['availability'] == 'missing'
    assert view['frontend']['source'] == '_snapshotToolResult.preview'
    assert view['audit']['findings'] == ['missing_model_result']


def test_audit_distinguishes_complex_json_from_internal_envelope_and_reports():
    value = {f'field-{i}': None for i in range(30)}
    review = audit_model_result(json.dumps(value))
    assert review['metrics']['jsonKeys'] == 30
    assert review['findings'] == ['many_json_fields', 'empty_json_fields']
    assert 'internal_envelope_leak' not in review['findings']
    assert 'escaped_report' in audit_model_result(json.dumps({'result': 'A\nB'}))['findings']
    assert 'internal_envelope_leak' in audit_model_result(json.dumps({
        'contractVersion': 'tofu.tool-result/v2', 'status': 'ok'}))['findings']
    assert not audit_model_result('Plain report')['findings']


def test_audit_traversal_and_inspector_output_are_bounded():
    assert not audit_model_result(json.dumps([0] * 4000))['metrics']['scanComplete']
    assert not audit_model_result('x' * 300000)['metrics']['scanComplete']
    view = inspect_tool_round({'toolContent': 'x' * 50000}, preview_chars=10)
    assert view['model']['text'] == 'x' * 10
    assert view['model']['chars'] == 50000 and view['model']['truncated']


def test_document_identity_filter_limit_and_notification_report():
    payload = format_swarm_update(agent_id='worker', role='general', status='completed',
                                  elapsed_seconds=1, tokens=2, result='Full\nReport', output_file='/audit.log')
    document = {'messages': [
        {'toolRounds': [{'toolName': 'read_files', 'toolCallId': 'same', 'toolContent': 'first'}]},
        {'toolRounds': [{'toolName': 'read_files', 'toolCallId': 'same', 'toolContent': 'second'}],
         '_inboxInjects': [{'previews': [{'agentId': 'worker', 'text': payload}]}]},
    ], 'diagnostic_unread_count': 7}
    report = inspect_result_document(document, tool_call_id='same')
    assert [row['turnIndex'] for row in report['records']] == [0, 1]
    assert report['coverage'] == 'read turns only'
    limited = inspect_result_document(document, max_results=1)
    assert limited['totalRecords'] == 3 and limited['truncated']
    notification = inspect_swarm_notification({'text': payload})
    assert notification['frontend']['text'] == 'Full\nReport'
    assert notification['canonical']['logPath'] == '/audit.log'
    assert notification['model']['availability'] == 'unverified'
    assert inspect_swarm_notification({'text': payload[:120]})['frontend']['availability'] == 'missing'


def test_default_policy_is_detached_and_preserves_json():
    policy = result_policy('custom-tool')
    policy['modelFormat'] = 'agent_report'
    assert result_policy('custom-tool')['modelFormat'] == 'producer'


def test_original_json_is_referenced_when_owned_report_changes_format(monkeypatch):
    retained = []
    def retain(canonical, *, user_id):
        retained.append((user_id, canonical.text()))
        return 'tool-result:' + 'a' * 64
    monkeypatch.setattr('lib.tools.result_value.persist_result', retain)
    raw = json.dumps({'agent_id': 'a', 'status': 'ok', 'final_answer': 'Report\nEvidence'})
    delivery = split_tool_result_delivery(budget_tool_result_v2('get_agent_result', raw, user_id=42))
    assert retained == [(42, raw)]
    assert delivery.evidence['artifactRef'] == 'tool-result:' + 'a' * 64
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']
    assert 'tool-result:' not in delivery.model_text


def test_poll_unwraps_agent_report_sections_without_changing_command_status():
    agent = {'agent_id': 'a', 'status': 'ok', 'agent_status': 'completed',
             'final_answer': 'Report HEAD\nReport TAIL'}
    command = '{"status":"running","id":"command-1","cursor":"opaque"}'
    raw = '== a (swarm agent) ==\n' + json.dumps(agent) + '\n== command-1 ==\n' + command
    view = project_model_source('poll', raw)
    assert 'Report HEAD\nReport TAIL' in view
    assert '== a (swarm agent) ==' in view
    assert view.endswith(command)
    receipt = {'status': 'ok', 'completed': [agent], 'partial': [],
               'unknown': ['missing'], 'still_running': ['running'], 'timed_out': True}
    text = project_model_source('poll', json.dumps(receipt))
    for field in receipt:
        assert field in text
    assert 'running' in text and 'missing' in text and 'timed_out: true' in text


def test_poll_preserves_all_nonreport_receipts_and_opaque_text():
    source = '== task ==\n' + json.dumps({'status': 'running', 'cursor': 'opaque'}) + '\nraw command output'
    assert project_model_source('poll', source) == source


def test_frontend_arguments_and_specialized_sidecars_are_visible_without_media_dump():
    view = inspect_tool_round({'toolName': 'spawn_agents', 'toolArgs': {'agents': [{'objective': 'Review'}]},
        'toolContent': 'started', '_swarmAgents': [{'id': 'worker', 'preview': 'Report'}],
        'toolModelContent': [{'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + 'SECRETIMAGE' * 100}}]})
    assert 'Review' in view['frontend']['arguments']['text']
    assert '_swarmAgents' in view['frontend']['roundSidecars']['text']
    assert 'Report' in view['frontend']['roundSidecars']['text']
    assert 'SECRETIMAGE' not in view['model']['text']
    assert view['model']['mediaOmitted']


def test_report_retention_failure_is_visible_without_leaking_backend_details(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('secret-backend-location')
    monkeypatch.setattr('lib.tools.result_value.persist_result', fail)
    raw = json.dumps({'agent_id': 'a', 'status': 'ok', 'final_answer': 'Report\nEvidence'})
    delivery = split_tool_result_delivery(budget_tool_result_v2('get_agent_result', raw, user_id=42))
    assert 'Report\nEvidence' in delivery.model_text
    assert 'Canonical retention unavailable' in delivery.model_text
    assert 'secret-backend-location' not in delivery.model_text
    assert delivery.evidence['retentionCoverage'] == 'unavailable'
    assert not delivery.evidence.get('artifactRef')
