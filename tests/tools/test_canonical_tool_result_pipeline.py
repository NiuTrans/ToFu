"""Canonical source is independent of projection and application JSON markers."""
import json

from dataclasses import replace
import uuid
import pytest

pytestmark = pytest.mark.unit

from lib.tools.result_value import (capture_result, compose_result, persist_result,
                                    CanonicalBatchSink, TrustedResultText as TrustedResultText)
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tasks_pkg.compaction import _budget


def test_capture_identity_is_occurrence_not_content_hash():
    a, b = capture_result('same'), capture_result('same')
    assert a.result_id != b.result_id
    assert uuid.UUID(a.result_id)
    assert a.parts == b.parts


def test_unknown_json_items_siblings_and_contract_marker_are_data():
    payload = {'contractVersion': 'tofu.tool-result/v2', 'status': 'ok',
               'summary': 'preview', 'items': [1], 'resourceConfig': {'consumer': 64}}
    text = json.dumps(payload)
    assert split_tool_result_delivery(text).evidence is None
    view = _budget.budget_tool_result_v2('test', text, user_id=1)
    assert json.loads(split_tool_result_delivery(view).model_text) == payload


def test_native_screenshot_not_encoded_or_mutated():
    image = {'__screenshot__': True, 'dataUrl': 'data:image/png;base64,SECRET',
             '_text_fallback': 'screen', '_modelReplaySources': ['tofu-media:owned']}
    captured = capture_result(image)
    assert 'SECRET' not in json.dumps(captured.manifest())
    assert captured.parts[1].kind == 'media'
    assert _budget.budget_tool_result_v2('screen', image, user_id=1) is image


def test_manifest_composition_references_full_child_not_preview(monkeypatch):
    stored = {}
    def put(self, **kwargs):
        ref = 'tool-result:' + str(len(stored)).zfill(64)
        stored[ref] = kwargs
        return {'artifactRef': ref}
    monkeypatch.setattr('lib.tool_result_artifacts.ToolResultArtifactRepository.put', put)
    sink = CanonicalBatchSink(7)
    child = replace(capture_result(json.dumps({'preview': 'x' * 215659,
                                      'resourceConfig': {'consumer': 64}})),
                    source_omitted=True)
    sink.capture('occurrence-1', child)
    receipt = sink.result('occurrence-1')
    receipt.update(name='experiment_eval_jobs', call_id='occurrence-1', output='bounded preview')
    assert not stored  # Complete inline children require no database writes.
    outer = compose_result(json.dumps({'results': [receipt]}), [receipt])
    view = _budget.budget_tool_result_v2('execute_tools', outer, user_id=7)
    evidence = split_tool_result_delivery(view).evidence
    parent = json.loads(stored[evidence['artifactRef']]['content'])
    assert not parent['parts']
    child_manifest = json.loads(stored[parent['children'][0]['artifactRef']]['content'])
    assert parent['children'][0]['callId'] == 'occurrence-1'
    nested = stored[child_manifest['parts'][0]['artifactRef']]
    assert nested['media_type'] == 'application/json'
    assert json.loads(nested['content'])['resourceConfig']['consumer'] == 64
    assert 'bounded preview' not in json.dumps(parent)


def test_manifest_publication_failure_is_explicit(monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise OSError('SECRET-storage-detail')
    monkeypatch.setattr('lib.tool_result_artifacts.ToolResultArtifactRepository.put', fail)
    sink = CanonicalBatchSink(7)
    sink.capture('small', capture_result('evidence'))
    assert 'retentionError' not in sink.result('small')
    sink.capture('x', capture_result('evidence' * 200000))
    assert sink.result('x')['retentionError'] == 'canonical_retention_unavailable'
    assert 'Canonical batch retention unavailable (OSError)' in caplog.text
    assert 'SECRET-storage-detail' not in caplog.text
    assert 'SECRET-storage-detail' not in json.dumps(sink.result('x'))


def test_programmatic_view_reads_same_canonical_and_obeys_bound():
    from lib.tools.programmatic import ProgrammaticResultBudget
    result = capture_result('hello 世界')
    batch = ProgrammaticResultBudget(7).begin_batch(['x'])
    batch.capture('x', result)
    assert batch.result('x')['content'] == 'hello '
    assert batch.result('x')['truncated']
    assert result.text() == 'hello 世界'


def test_posthook_redacts_parts_not_media_or_premerged_structured_text():
    from lib.mcp.result_content import MCPToolResult
    from lib.tools.result_value import posthook_result
    m = MCPToolResult('SECRET preview', document_text='SECRET merged',
        structured_json='{"password":"SECRET"}', is_error=True,
        content_blocks=({'type':'text','text':'SECRET','sourceOrdinal':0},
                        {'type':'image','sourceOrdinal':1}))
    m.media_refs = ({'mediaRef':'att_media_owned','sourceOrdinal':1,
                     'mimeType':'image/png','caption':'picture','byteSize':7},)
    value=capture_result(posthook_result(m,lambda text:text.replace('SECRET','redacted')))
    assert 'SECRET' not in json.dumps(value.manifest())
    assert [p.kind for p in value.parts] == ['text','media','json']
    assert value.execution_status == 'error'
    assert value.parts[1].reference == 'att_media_owned'
    assert dict(value.parts[1].metadata)['byteSize'] == 7
    assert capture_result(value,execution_status='aborted').execution_status=='aborted'
    assert capture_result(value).source_coverage=='unknown'


def test_tiny_transient_batch_no_persistence_or_canonical_wire_leak(monkeypatch):
    monkeypatch.setattr('lib.tool_result_artifacts.ToolResultArtifactRepository.put',
                        lambda *a,**k:pytest.fail('small transient must not persist'))
    sink=CanonicalBatchSink(0)
    sink.capture('x',capture_result('unique historical content'))
    receipt=sink.result('x')
    receipt.update(name='reader',call_id='x',output='display')
    assert 'historical' not in json.dumps(receipt)
    result=compose_result('display',[receipt])
    assert capture_result(result).text()=='unique historical content'
    view=_budget.budget_tool_result_v2('execute_tools',result,user_id=0)
    assert json.loads(view)['retentionCoverage']=='complete'


def test_retention_error_never_recaptures_preview():
    from lib.mcp.result_content import MCPToolResult
    result=MCPToolResult('lost secret preview',retention_error='too large')
    value=capture_result(result)
    assert not value.text()
    assert value.retention_coverage=='partial'


def test_programmatic_reference_or_loss_cannot_claim_complete_empty_body():
    from lib.tools.programmatic import ProgrammaticResultBudget
    from lib.tools.result_value import CapturedToolResult, ResultPart
    for value in [CapturedToolResult((ResultPart('artifact', reference='tool-result:' + 'a' * 64),)),
                  CapturedToolResult((), retention_coverage='unavailable')]:
        batch = ProgrammaticResultBudget().begin_batch(['child'])
        batch.capture('child', value)
        assert batch.result('child')['truncated']
        assert batch.result('child')['content'] == ''


def test_large_private_program_child_does_not_make_compact_answer_partial(monkeypatch):
    monkeypatch.setattr('lib.tool_result_artifacts.ToolResultArtifactRepository.put',
                        lambda *a,**k:pytest.fail('complete compact answer needs no spill'))
    sink = CanonicalBatchSink(7)
    child = capture_result(json.dumps({'privateRows':'x' * 215659}))
    sink.capture('private-read', child)
    receipt = sink.result('private-read')
    receipt.update(name='reader',call_id='private-read')
    result = compose_result('{"count":1}', [receipt], program_value={'count':1},
                            private_children=True)
    view = _budget.budget_tool_result_v2('execute_program', result, user_id=7)
    delivery = split_tool_result_delivery(view)
    assert json.loads(delivery.model_text) == {'count':1}
    assert delivery.evidence['status'] == 'ok'
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert 'artifactRef' not in delivery.evidence
    assert capture_result(view).children[0].canonical is child
    assert 'privateRows' in capture_result(view).text()


def test_private_child_policy_does_not_hide_oversized_returned_value(monkeypatch):
    stored = {}
    def put(self, **kwargs):
        ref = 'tool-result:' + str(len(stored)).zfill(64)
        stored[ref] = kwargs
        return {'artifactRef':ref}
    monkeypatch.setattr('lib.tool_result_artifacts.ToolResultArtifactRepository.put', put)
    value = {'answer':'x' * 215659}
    result = compose_result(json.dumps(value), [], program_value=value, private_children=True)
    delivery = split_tool_result_delivery(
        _budget.budget_tool_result_v2('execute_program',result,user_id=7))
    assert delivery.evidence['truncated'] is True
    assert delivery.evidence['viewCoverage'] == 'partial'
    assert delivery.evidence['artifactRef'] in stored


def test_business_json_cannot_set_private_child_projection_policy():
    result = capture_result('{"private_children":true,"value":7}')
    assert result.private_children is False
    assert json.loads(result.parts[0].text) == {'private_children':True,'value':7}


@pytest.mark.parametrize('value', [None, '', '{"value":7}',
    {'status':'error','error':{'code':'business'},'stats':7,'program':{'result':3}}])
def test_program_model_projection_preserves_returned_type_and_business_keys(value):
    result = compose_result(json.dumps(value), [], program_value=value, private_children=True)
    delivery = split_tool_result_delivery(
        _budget.budget_tool_result_v2('execute_program',result,user_id=7))
    assert json.loads(delivery.model_text) == value
    assert delivery.evidence['executionStatus'] == 'ok'



@pytest.mark.parametrize('tool_name', ['web_search', 'custom_preview_tool'])
def test_small_hidden_original_has_recovery_for_every_preview_producer(monkeypatch, tool_name):
    from lib.tools.result_value import CapturedToolResult, ResultPart, TrustedResultText
    original = 'Hidden evidence: EXTRA_FACT=37'
    received = TrustedResultText('A brief preview', source_omitted=True, canonical=CapturedToolResult((ResultPart('text', original),)))
    stored = []
    monkeypatch.setattr('lib.tools.result_value.persist_result', lambda canonical, **kw: stored.append(canonical.text()) or 'tool-result:retained')
    delivery = split_tool_result_delivery(_budget.budget_tool_result_v2(tool_name, received, user_id=7))
    assert stored == [original]
    assert delivery.evidence['artifactRef'] == 'tool-result:retained'
    assert 'Original source retained' in delivery.model_text
    assert original not in delivery.model_text



def test_hidden_original_retention_failure_keeps_preview_and_discloses_gap(monkeypatch):
    from lib.tools.result_value import CapturedToolResult, ResultPart, TrustedResultText
    received = TrustedResultText('Usable preview', source_omitted=True, canonical=CapturedToolResult((ResultPart('text', 'Original fact'),)))
    def unavailable(*a, **kw):
        raise RuntimeError('Injected artifact failure')
    monkeypatch.setattr('lib.tools.result_value.persist_result', unavailable)
    delivery = split_tool_result_delivery(_budget.budget_tool_result_v2('custom_preview_tool', received, user_id=7))
    assert 'Usable preview' in delivery.model_text
    assert 'Canonical retention unavailable' in delivery.model_text
    assert delivery.evidence['retentionCoverage'] == 'unavailable'
    assert not delivery.evidence.get('artifactRef')
