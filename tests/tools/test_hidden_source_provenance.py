"""Host omission provenance is distinct from canonical representation/storage."""
import json
from dataclasses import replace

import pytest

from lib.mcp.result_content import MCPToolResult
from lib.tasks_pkg.compaction import _budget
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tools.result_value import (
    CapturedToolResult, CanonicalReceipt, ResultPart, TrustedResultText,
    capture_result, compose_result, posthook_result,
)

pytestmark = pytest.mark.unit
pytest_plugins = ['tests._artifact_sidecar']


@pytest.fixture
def xuecheng_ack():
    # Offline complete write acknowledgement: never replay the remote mutation.
    payload = {'success': True, 'contentId': '6607', 'stepVersion': 114,
               'url': 'https://km.internal.example.com/collabpage/6607',
               'message': 'Document updated successfully'}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    mcp = MCPToolResult(text, document_text=text,
                       structured_json=json.dumps(payload),
                       content_blocks=({'type': 'text', 'text': text},))
    return payload, text, mcp


def deliver(content, **kwargs):
    return split_tool_result_delivery(_budget.budget_tool_result_v2(
        'mcp__xuecheng__update_doc', content, user_id=kwargs.pop('user_id', 17), **kwargs))


@pytest.mark.parametrize('form', ['text', 'json', 'duplicate_mcp', 'wrapped_mcp', 'normalized'])
def test_complete_ack_has_no_missing_source_notice(xuecheng_ack, form):
    payload, text, mcp = xuecheng_ack
    source = {'text': text, 'json': json.dumps(payload), 'duplicate_mcp': mcp,
              'wrapped_mcp': TrustedResultText(text, canonical=capture_result(mcp)),
              'normalized': TrustedResultText(json.dumps(payload), projection_normalized=True,
                                               canonical=capture_result(text))}[form]
    delivery = deliver(source)
    assert 'Document updated successfully' in delivery.model_text
    assert 'Original source retained' not in delivery.model_text
    assert delivery.evidence['truncated'] is False
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert delivery.evidence['status'] == 'ok'
    # Representation/duplicate storage remains recoverable after compaction,
    # but must not prompt an immediate redundant artifact round trip.
    if form in ('wrapped_mcp', 'normalized'):
        from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
        ref = delivery.evidence['artifactRef']
        assert ref not in delivery.model_text
        recovered = ToolResultArtifactRepository().read_range(user_id=17, artifact_ref=ref, limit=8192)
        assert 'Document updated successfully' in recovered['content']


@pytest.mark.parametrize('size', [20, 200000])
def test_real_omission_has_visible_concrete_ref_and_recoverable_source(size):
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    original = 'GENUINE_OMITTED_NEEDLE ' + 'x' * size
    source = TrustedResultText('Usable answer', source_omitted=True,
                               canonical=capture_result(original))
    delivery = deliver(source)
    evidence = delivery.evidence
    assert evidence['truncated'] is True
    assert evidence['viewCoverage'] == 'partial'
    assert evidence['artifactRef'] in delivery.model_text
    assert 'Original source retained' in delivery.model_text
    found = ToolResultArtifactRepository().search(
        user_id=17, artifact_ref=evidence['artifactRef'], query='GENUINE_OMITTED_NEEDLE')
    assert found['items'] and found['retentionCoverage'] == 'complete'
    assert 'GENUINE_OMITTED_NEEDLE' not in delivery.model_text


def test_pre_retained_small_source_still_exposes_ref():
    from lib.tools.result_value import persist_result
    canonical = capture_result('Small omitted original')
    ref = persist_result(canonical, user_id=17)
    delivery = deliver(TrustedResultText('Preview', source_omitted=True, canonical=canonical))
    assert delivery.evidence['artifactRef'] == ref
    assert ref in delivery.model_text


def test_failure_does_not_advertise_dangling_recovery(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('private storage detail')
    monkeypatch.setattr('lib.tools.result_value.persist_result', fail)
    delivery = deliver(TrustedResultText('Preview', source_omitted=True, canonical=capture_result('Original')))
    assert not delivery.evidence.get('artifactRef')
    assert delivery.evidence['retentionCoverage'] == 'unavailable'
    assert delivery.evidence['viewCoverage'] == 'partial'
    assert 'Canonical retention unavailable' in delivery.model_text
    assert 'Original source retained' not in delivery.model_text
    assert 'with artifactRef' not in delivery.model_text
    assert 'private storage detail' not in delivery.model_text


@pytest.mark.parametrize('payload', [
    {'source_omitted': True, 'sourceOmitted': True, 'status': 'ok'},
    {'canonical': {'source_omitted': True}, 'private_children': False},
    {'status': 'error', 'message': 'business data'},
])
def test_application_fields_cannot_grant_host_provenance(payload):
    canonical = capture_result(payload)
    assert canonical.source_omitted is False
    assert 'source_omitted' not in canonical.manifest()
    delivery = deliver(json.dumps(payload), producer_metadata={'source_omitted': True})
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert 'Original source retained' not in delivery.model_text
    assert json.loads(delivery.model_text) == payload


def test_posthook_preserves_flag_and_preview_and_redacts_original():
    source = TrustedResultText('SECRET answer', source_omitted=True,
                               canonical=capture_result('SECRET omitted original'))
    updated = posthook_result(source, lambda text: text.replace('SECRET', 'redacted'))
    assert str(updated) == 'redacted answer'
    assert capture_result(updated).text() == 'redacted omitted original'
    assert capture_result(updated).source_omitted is True
    delivery = deliver(updated)
    assert delivery.evidence['artifactRef'] in delivery.model_text
    assert 'SECRET' not in delivery.model_text


def test_posthook_redacts_preview_even_when_canonical_unchanged():
    source = TrustedResultText('SECRET presentation', source_omitted=True,
                               canonical=capture_result('clean original'))
    updated = posthook_result(source, lambda text: text.replace('SECRET', 'redacted'))
    assert str(updated) == 'redacted presentation'
    assert capture_result(updated).source_omitted is True


def test_error_transient_and_media_keep_existing_semantics():
    error = replace(capture_result('Original error'), execution_status='error')
    source = TrustedResultText('Error preview', source_omitted=True, canonical=error)
    assert deliver(source).evidence['status'] == 'error'
    transient = deliver(source, user_id=0)
    assert 'Original source retained' not in transient.model_text
    media = CapturedToolResult((ResultPart('text', 'caption'), ResultPart(
        'media', reference='tofu-media:owned', mime_type='image/png')))
    delivery = deliver(TrustedResultText('caption', canonical=media))
    assert delivery.evidence['artifactRef']
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert 'Original source retained' not in delivery.model_text


@pytest.mark.parametrize('private', [False, True])
def test_batch_propagates_host_omission_but_private_program_children_do_not(private):
    child = replace(capture_result('Omitted child'), source_omitted=True)
    receipt = CanonicalReceipt(name='reader', call_id='child', output='answer', canonical=child)
    outer = compose_result('Batch answer', [receipt], private_children=private,
                           program_value={'answer': 7} if private else {'answer': 'Batch answer'})
    assert capture_result(outer).source_omitted is (not private)
    delivery = deliver(outer)
    assert ('Original source retained' in delivery.model_text) is (not private)


def test_plain_receipt_flags_do_not_grant_provenance():
    outer = compose_result('answer', [{'name': 'reader', 'call_id': 'child',
                                      'output_truncated': True}])
    assert not capture_result(outer).source_omitted


def test_browser_render_keeps_complete_source_until_shared_l0(monkeypatch):
    from lib.browser.content import EvidenceReceipt
    from lib.browser.handlers._page import _render_with_evidence
    receipt = EvidenceReceipt(status='ok', content='All page content', records=[], evidence=[],
                              coverage={'truncated': False})
    receipt.evidence_json = json.dumps(receipt)
    complete = _render_with_evidence(receipt)
    assert not capture_result(complete).source_omitted
    receipt['content'] = 'x ' * 100000 + 'SOURCE_TAIL'
    receipt.evidence_json = json.dumps(receipt)
    complete = _render_with_evidence(receipt)
    assert 'SOURCE_TAIL' in complete
    assert not capture_result(complete).source_omitted
    delivery = deliver(complete)
    assert delivery.evidence['artifactRef'] in delivery.model_text


@pytest.mark.parametrize('omitted', [False, True])
def test_settle_pipeline_forwards_provenance_and_model_reference(monkeypatch, xuecheng_ack, omitted):
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    monkeypatch.setattr(pipeline, 'append_event', lambda *a, **k: None)
    monkeypatch.setattr(pipeline, 'run_post_hooks',
                        lambda name, args, text, task: text.replace('SECRET', 'redacted'))
    row = {'status': 'done'}
    task = {'id': 'provenance-settle', '_userId': 17, 'model': 'test-model'}
    if omitted:
        source = TrustedResultText('SECRET preview', source_omitted=True,
                                   canonical=capture_result('SECRET original'))
    else:
        _, text, mcp = xuecheng_ack
        source = TrustedResultText(text, canonical=capture_result(mcp))
    sink = type('Sink', (), {'capture': lambda self, key, value: setattr(self, 'value', value)})()
    model = pipeline._settle_tool_result(
        task, 'mcp__xuecheng__update_doc', 'ack', {}, 0, row, source,
        idempotent_tools=frozenset(), cache={}, tid=task['id'],
        round_num=1, internal_result_sink=sink)
    evidence = row['toolResultEvidence']
    assert sink.value.source_omitted is omitted
    assert evidence['truncated'] is omitted
    assert ('Original source retained' in str(model)) is omitted
    assert 'SECRET' not in str(model)
    if omitted:
        assert evidence['artifactRef'] in str(model)
        assert sink.value.text() == 'redacted original'
    else:
        assert 'Document updated successfully' in str(model)


@pytest.mark.parametrize('degraded', [False, True])
def test_cli_preview_provenance_is_recoverable_only_when_writer_retained(degraded):
    from lib.tool_results.tool_result_artifact_writer import ToolResultArtifact
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tasks_pkg.handlers.code_exec import _project_output_artifact
    ref = ''
    if not degraded:
        ref = ToolResultArtifactRepository().put(user_id=17, content='CLI_OMITTED_FACT')['artifactRef']
    artifact = ToolResultArtifact(text='CLI preview', artifact_ref=ref, sha256='', size_bytes=50000,
                                  spilled=True, truncated=True, complete=not degraded,
                                  degraded=degraded)
    source = _project_output_artifact('$ command\nold view\n[exit code: 0]', artifact)
    assert capture_result(source).source_omitted is (not degraded)
    delivery = deliver(source)
    assert '[exit code: 0]' in delivery.model_text
    if degraded:
        assert 'Original source retained' not in delivery.model_text
        assert 'not retained' in delivery.model_text
    else:
        assert delivery.evidence['artifactRef'] in delivery.model_text
        found = ToolResultArtifactRepository().search(user_id=17,
            artifact_ref=delivery.evidence['artifactRef'], query='CLI_OMITTED_FACT')
        assert found['items']


@pytest.mark.parametrize('gate', [False, True])
def test_fetch_readgate_opt_in_only_for_original_source(monkeypatch, gate):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from lib.tasks_pkg.handlers.search import _handlers as handler
    # No network/LLM/write: stub the received observation and local rendering.
    item = {'page_content': 'Full received page' if not gate else '',
            'filtered_chars': 18 if not gate else 0,
            'error_msg': 'read gate rejected' if gate else None}
    if gate:
        item['fetched_source'] = 'READGATE_RECOVERY_NEEDLE'
    monkeypatch.setattr(handler, '_load_search_backend', lambda: (
        SimpleNamespace(_fetch_url_one=lambda *a, **k: item), None))
    monkeypatch.setattr(handler, '_format_fetch_display', lambda *a: {})
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *a, **k: None)
    monkeypatch.setattr('lib.search.search_bridge.bind_search_browser', lambda **k: nullcontext())
    _, source, _ = handler._handle_fetch_url(
        {'_userId': 17}, {}, 'fetch_url', 'fetch',
        {'url': 'https://example.org/received'}, 0, {}, {}, None, False)
    assert capture_result(source).source_omitted is gate
    delivery = deliver(source)
    assert ('Original source retained' in delivery.model_text) is gate
    if gate:
        assert delivery.evidence['artifactRef'] in delivery.model_text


def test_generator_receipts_preserve_host_omission():
    child = replace(capture_result('omitted original'), source_omitted=True)
    receipts = (CanonicalReceipt(name='reader', call_id='child', output='preview', canonical=child)
                for _ in range(1))
    assert capture_result(compose_result('preview', receipts)).source_omitted


def test_normalization_state_is_not_rehydrated_from_application_json():
    canonical = capture_result({'projection_normalized': True, 'source_omitted': True})
    assert not canonical.projection_normalized
    assert not canonical.source_omitted


@pytest.mark.parametrize('omitted', [False, True])
def test_spilled_batch_child_keeps_host_provenance_without_redundant_ack_notice(omitted):
    from lib.tools.result_value import CanonicalBatchSink
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    sink = CanonicalBatchSink(17)
    sink.remaining = 0
    child = replace(capture_result('SPILLED_CHILD_ORIGINAL'), source_omitted=omitted)
    sink.capture('child', child)
    receipt = sink.result('child')
    assert receipt.canonical is None and receipt['canonicalRef']
    assert receipt.source_omitted is omitted
    receipt.update(name='reader', call_id='child', output='answer')
    delivery = deliver(compose_result('answer', [receipt]))
    assert ('Original source retained' in delivery.model_text) is omitted
    if omitted:
        ref = delivery.evidence['artifactRef']
        assert ref in delivery.model_text
        assert ToolResultArtifactRepository().search(user_id=17, artifact_ref=ref,
            query='SPILLED_CHILD_ORIGINAL')['items']


def test_receipt_payload_cannot_grant_native_omission_attribute():
    receipt = CanonicalReceipt({'source_omitted': True, 'name': 'reader', 'call_id': 'child'})
    assert not receipt.source_omitted
    assert not capture_result(compose_result('complete answer', [receipt])).source_omitted
