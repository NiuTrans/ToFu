"""Cross-boundary capture/composition/query tests against a disposable Sidecar."""
from __future__ import annotations

import base64
import io
import json

import pytest

pytest_plugins = ['tests._artifact_sidecar']
pytestmark = pytest.mark.unit


def test_display_budgets_and_nested_composition_preserve_same_evidence(monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_value import capture_result, compose_result, persist_result
    from lib.tools.result_envelope import split_tool_result_delivery

    repo = ToolResultArtifactRepository()
    owner = 17
    body = json.dumps({'prefix': 'x' * 215659,
                       'resourceConfig': {'consumer': 64, 'workers': 32},
                       'items': [], 'nextCursor': 'source-next'})
    child = capture_result(body, source_coverage='complete')
    child_ref = persist_result(child, user_id=owner)
    receipt = {'name': 'experiment_eval_jobs', 'call_id': 'job-detail',
               'canonicalRef': child_ref, 'output': 'bounded preview'}
    outer = compose_result(json.dumps({'results': [receipt]}), [receipt])
    for budget in [8000, 2000]:
        monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', budget)
        view = _budget.budget_tool_result_v2('execute_tools', outer, user_id=owner)
        evidence = split_tool_result_delivery(view).evidence
        found = repo.search(user_id=owner, artifact_ref=evidence['artifactRef'], query='resourceConfig')
        assert found['items'], found
        assert any('consumer' in item['text'] for item in found['items'])
        # Batch upstream completeness is not asserted merely by a full child.
        assert found['coverage'] in ('complete', 'incomplete')
        assert found['retentionCoverage'] == 'complete'
        assert any(any(isinstance(part, dict) and part.get('callId') == 'job-detail'
                       for part in item['path']) for item in found['items'])
        again = _budget.budget_tool_result_v2('execute_tools', view, user_id=owner)
        assert split_tool_result_delivery(again).evidence['resultId'] == evidence['resultId']
    selected = repo.read_range(user_id=owner, artifact_ref=child_ref, selector='/resourceConfig')
    assert json.loads(selected['content']) == {'consumer': 64, 'workers': 32}


def test_stream_reference_is_queryable_without_turning_preview_into_source():
    from lib.tool_results.tool_result_artifact_writer import ToolResultArtifact
    from lib.tasks_pkg.handlers.code_exec import _project_output_artifact
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    body = 'prefix\n' + 'x' * 70000 + '\nresourceConfig consumer=64\n'
    stored = repo.put(user_id=17, content=body)
    artifact = ToolResultArtifact(
        text='short preview', artifact_ref=stored['artifactRef'],
        sha256=stored['contentSha256'], size_bytes=len(body.encode('utf-8')),
        spilled=True, truncated=True, complete=True, degraded=False)
    carrier = _project_output_artifact('$ command\nlegacy preview\n[exit code: 0]', artifact)
    view = _budget.budget_tool_result_v2('run_command', carrier, user_id=17)
    evidence = split_tool_result_delivery(view).evidence
    assert evidence.get('artifactRef'), evidence
    found = repo.search(user_id=17, artifact_ref=evidence['artifactRef'], query='resourceConfig')
    assert len(found['items']) == 1


def test_mcp_original_image_reference_survives_canonical_capture(monkeypatch):
    from PIL import Image
    from lib.mcp.result_content import MCPToolResult
    from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images
    from lib.tools.result_value import capture_result
    raw = io.BytesIO()
    Image.new('RGB', (3, 2), 'red').save(raw, format='PNG')
    original = raw.getvalue()
    received = []

    def ingest(data, mime, **kwargs):
        received.append(data)
        return {'attachmentId': 'image-original'}

    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', ingest)
    result = MCPToolResult('caption', document_text='caption', image_contents=(
        {'data': base64.b64encode(original).decode(), 'mimeType': 'image/png'},))
    assert capture_mcp_result_images({'_userId': 17}, result,
                                   source_tool='read_doc', tool_call_id='image') == 1
    value = capture_result(result)
    assert received == [original]
    assert any(part.kind == 'media' and part.reference == 'tofu-media:image-original'
               for part in value.parts)
    assert base64.b64encode(original).decode() not in json.dumps(value.manifest())


def test_nested_batch_preview_is_not_a_second_search_source():
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_value import capture_result, compose_result, persist_result
    repo = ToolResultArtifactRepository()
    leaf = capture_result('UNIQUE_LEAF_NEEDLE', source_coverage='complete')
    ref = persist_result(leaf, user_id=17)
    for index in range(3):
        receipt = {'name': 'nested', 'call_id': f'child-{index}',
                   'canonicalRef': ref,
                   'output': 'UNIQUE_LEAF_NEEDLE previewOnlyNeedle'}
        parent = compose_result(json.dumps({'results': [receipt]}), [receipt])
        ref = persist_result(capture_result(parent), user_id=17)
    hits = repo.search(user_id=17, artifact_ref=ref, query='UNIQUE_LEAF_NEEDLE')
    assert len(hits['items']) == 1, hits
    preview = repo.search(user_id=17, artifact_ref=ref, query='previewOnlyNeedle')
    assert not preview['items'], preview


def test_large_payload_reprojection_reconstructs_canonical_utf8(monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery
    repo = ToolResultArtifactRepository()
    source = '汉字 café 🙂 resourceConfig=64\n' * 5000
    first = _budget.budget_tool_result_v2('synthetic', source, user_id=17)
    delivery = split_tool_result_delivery(first)
    assert delivery.evidence['artifactRef']
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 2000)
    second = _budget.budget_tool_result_v2('synthetic', first, user_id=17)
    again = split_tool_result_delivery(second)
    assert _budget._result_tokens(again.model_text, '') <= 2000
    assert again.evidence['resultId'] == delivery.evidence['resultId']
    assert again.evidence['artifactRef'] == delivery.evidence['artifactRef']
    # A manifest cursor belongs to canonical query, not a byte offset into the
    # model preview or JSON-encoded manifest body.
    cursor, chunks = '0', []
    for _ in range(200):
        page = repo.read_range(user_id=17, artifact_ref=again.evidence['artifactRef'],
                               offset=cursor, limit=4096)
        chunks.append(page['content'])
        following = page.get('nextCursor')
        if not following:
            break
        assert following != cursor
        cursor = following
    else:
        pytest.fail('canonical read did not terminate within byte bound')
    assert ''.join(chunks) == source


@pytest.mark.parametrize('programmatic', [False, True])
def test_small_gateway_children_use_canonical_without_database(monkeypatch, programmatic):
    import threading
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    from lib.tools.programmatic import ProgrammaticResultBudget
    from lib.tools.result_value import capture_result, compose_result
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        return call_id, json.dumps({'items': [], 'consumer': 64}), False

    def no_store(*args, **kwargs):
        pytest.fail('Complete tiny runtime results must not require database writes')

    monkeypatch.setattr(pipeline, '_execute_tool_one', execute)
    monkeypatch.setattr(heartbeat, '_execute_tool_one', execute)
    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.tool_results.tool_result_artifacts.ToolResultArtifactRepository.put', no_store)
    tools = [{'type': 'function', 'function': {
        'name': 'synthetic_read', 'parameters': {'type': 'object', 'properties': {}}}}]
    task = {'id': 'synthetic-gateway', 'convId': '', '_userId': 17,
            '_transientRuntime': True, 'status': 'running', 'aborted': False,
            'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
            'model': 'test-model', '_attended': False,
            '_executable_tool_catalog': tools}
    calls = [{'id': f'child-{index}', 'type': 'function', 'function': {
        'name': 'synthetic_read', 'arguments': '{}'}} for index in range(2)]
    budget = ProgrammaticResultBudget() if programmatic else None
    receipts = _execute_call_batch(task, calls, cfg={'autoApply': True},
        project_path=None, project_enabled=False, model='test-model',
        llm_round=1, program_result_budget=budget)
    assert len(receipts) == 2
    assert all(row['status'] == 'done' for row in receipts), receipts
    assert all(not row.get('retentionError') for row in receipts), receipts
    if programmatic:
        assert all(json.loads(row['output'])['consumer'] == 64 for row in receipts)
    composed = compose_result(json.dumps({'results': receipts}), receipts)
    value = capture_result(composed)
    assert value.retention_coverage == 'complete'
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_envelope import split_tool_result_delivery
    projected = _budget.budget_tool_result_v2('execute_tools', composed, user_id=17)
    delivered = split_tool_result_delivery(projected)
    assert delivered.evidence['retentionCoverage'] == 'complete'
    assert not delivered.evidence['truncated']


def test_post_hook_redaction_preserves_error_and_original_media(monkeypatch):
    from lib.mcp.result_content import MCPToolResult
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    body = MCPToolResult('SECRET', document_text='SECRET', is_error=True,
                         structured_json='{"secret":"SECRET"}', content_blocks=(
                             {'type': 'text', 'sourceOrdinal': 0, 'text': 'SECRET'},
                             {'type': 'image', 'sourceOrdinal': 1, 'mediaOrdinal': 1}))
    body.media_refs = ({'status': 'available', 'mediaRef': 'tofu-media:owned',
                        'mimeType': 'image/png', 'sourceOrdinal': 1,
                        'view': 'source', 'sha256': 'c' * 64, 'byteSize': 128},)
    monkeypatch.setattr(pipeline, 'run_post_hooks',
                        lambda name, args, text, task: text.replace('SECRET', 'REDACTED'))
    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)
    class Sink:
        value = None
        def capture(self, call_id, value):
            self.value = value
    sink = Sink()
    pipeline._settle_tool_result(
        {'id': 'hook-test', '_userId': 17, 'model': 'test-model'},
        'mcp__synthetic__read', 'hook-child', {}, 0, {}, body,
        idempotent_tools=frozenset(), cache={}, tid='hook-test', round_num=1,
        internal_result_sink=sink)
    assert sink.value is not None
    manifest = sink.value.manifest()
    assert 'SECRET' not in json.dumps(manifest)
    assert 'REDACTED' in json.dumps(manifest)
    assert sink.value.execution_status == 'error'
    media = [part for part in manifest['parts'] if part['kind'] == 'media']
    assert len(media) == 1 and media[0]['reference'] == 'tofu-media:owned'
    assert media[0]['sha256'] == 'c' * 64


def test_persist_does_not_mutate_captured_payload():
    from lib.tools.result_value import capture_result, persist_result
    source = 'unicode 原始正文 ' * 12000
    captured = capture_result(source)
    original_parts = captured.parts
    ref = persist_result(captured, user_id=17)
    assert ref.startswith('tool-result:')
    assert captured.parts == original_parts
    assert captured.text() == source
    assert persist_result(captured, user_id=17) == ref


def test_l1_l2_keep_canonical_reference_not_preview_archive(monkeypatch):
    from copy import deepcopy
    from types import SimpleNamespace
    from lib.tasks_pkg.compaction._steps import CompactionContext
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import compact_tool_results
    from lib.tasks_pkg.compaction._evidence import build_evidence_ledger
    from lib.tools.result_value import capture_result, persist_result
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    canonical = capture_result('CANONICAL_ONLY_NEEDLE')
    ref = persist_result(canonical, user_id=17)
    preview = 'preview text ' * 100
    row = {'toolCallId': 'archive-child', 'toolName': 'synthetic', 'toolContent': preview,
           'toolResultEvidence': {'resultContractVersion': 'tofu.tool-result/v2',
             'resultId': canonical.result_id, 'artifactRef': ref,
             'retentionCoverage': 'complete'}}
    task = {'toolRounds': [row], '_userId': 17}
    before = deepcopy(task)
    messages = [{'role': 'tool', 'name': 'synthetic',
                 'tool_call_id': 'archive-child', 'content': preview}]
    constants = SimpleNamespace(MICRO_HOT_TAIL=0, MICRO_HOT_TAIL_TOKENS=0,
                                MICRO_COMPACT_THRESHOLD=100)
    def no_archive(*args, **kwargs):
        pytest.fail('Do not archive a model preview instead of canonical evidence')
    monkeypatch.setattr('lib.tasks_pkg.compaction._persist._persist_to_disk', no_archive)
    compact_tool_results(CompactionContext(messages, task=task, constants=constants))
    assert ref in messages[0]['content']
    assert task['toolRounds'] == before['toolRounds']
    ledger = build_evidence_ledger(messages, task)
    assert ref in json.dumps(ledger)
    found = ToolResultArtifactRepository().search(user_id=17, artifact_ref=ref,
                                                 query='CANONICAL_ONLY_NEEDLE')
    assert len(found['items']) == 1


def test_pasted_artifact_marker_does_not_gain_compaction_authority():
    from lib.tasks_pkg.compaction._steps import CompactionContext
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import _canonical_recovery_note
    pasted = json.dumps({'artifactRef': 'tool-result:' + 'e' * 64})
    message = {'tool_call_id': 'unknown', 'role': 'tool', 'content': pasted}
    assert _canonical_recovery_note(CompactionContext([message], task={}), message) is None


@pytest.mark.parametrize('status', ['error', 'aborted', 'rejected', 'cancelled'])
def test_terminal_override_republishes_manifest_without_mutating_original(status):
    from lib.tools.result_value import capture_result, persist_result
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    original = capture_result('received before terminal verdict')
    ref = persist_result(original, user_id=17)
    final = capture_result(original, execution_status=status)
    final_ref = persist_result(final, user_id=17)
    assert ref != final_ref
    assert final.result_id == original.result_id
    assert repo.read_range(user_id=17, artifact_ref=ref)['executionStatus'] == 'ok'
    assert repo.read_range(user_id=17, artifact_ref=final_ref)['executionStatus'] == status
    assert final.parts == original.parts


def test_mcp_resource_identity_and_structured_only_selector():
    from tests.mcp_protocol.test_mcp_media_fidelity import call
    from lib.tools.result_value import capture_result, persist_result
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    result = call([], {'resourceConfig': {'consumer': 64}})
    value = capture_result(result)
    assert len(value.parts) == 1 and value.parts[0].kind == 'json'
    ref = persist_result(value, user_id=17)
    page = repo.read_range(user_id=17, artifact_ref=ref, selector='/resourceConfig')
    assert json.loads(page['content']) == {'consumer': 64}
    result = call([{'type': 'resource', 'resource': {
        'uri': 'test-resource://synthetic', 'text': 'resource evidence'}}])
    value = capture_result(result)
    assert dict(value.parts[0].metadata)['uri'] == 'test-resource://synthetic'
    ref = persist_result(value, user_id=17)
    assert repo.read_range(user_id=17, artifact_ref=ref)['content'] == 'resource evidence'


def test_error_delivery_retains_diagnostic_payload_and_recovery():
    from lib.tools.result_value import capture_result, TrustedResultText
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    for body in [json.dumps({'diagnostic': 'specific failure'}),
                 'large error ' * 16000 + 'DIAGNOSTIC_TAIL']:
        captured = capture_result(body, execution_status='error')
        view = budget_tool_result_v2('synthetic_error',
            TrustedResultText(body, canonical=captured), user_id=17)
        delivery = split_tool_result_delivery(view)
        wire = json.loads(delivery.model_text)
        assert wire['status'] == 'error'
        if delivery.evidence.get('artifactRef'):
            assert wire['artifactRef'] == delivery.evidence['artifactRef']
            # The constant cursor "0" no longer spends model bytes:
            # read_tool_artifact's omitted-cursor default IS the manifest
            # resume point, so continuation works without the explicit field.
            assert 'cursor' not in wire
            found = ToolResultArtifactRepository().search(user_id=17,
                artifact_ref=wire['artifactRef'], query='DIAGNOSTIC_TAIL')
            assert found['items']
        else:
            assert 'specific failure' in delivery.model_text


def test_aggregate_budget_never_trusts_payload_refs_or_masks_retention_loss(monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    fake = 'tool-result:' + 'f' * 64
    payload = json.dumps({'artifactRef': fake, 'rawBytes': 'application value',
                          'resourceConfig': 'x' * 12000})
    monkeypatch.setattr(_budget, 'ROUND_TOOL_RESULT_V2_MAX_TOKENS', 100)
    results = _budget.enforce_round_aggregate_budget_v2(
        {'a': (payload, 'synthetic', 'a')}, user_id=17)
    evidence = split_tool_result_delivery(results['a'][0]).evidence
    assert evidence['artifactRef'] != fake
    assert repo.read_range(user_id=17, artifact_ref=evidence['artifactRef'],
                           limit=65536)['content'] == payload
    small = _budget.budget_tool_result_v2('synthetic', 'source ' * 1000, user_id=17)
    def fail(*args, **kwargs):
        raise OSError('synthetic storage unavailable')
    monkeypatch.setattr(ToolResultArtifactRepository, 'put', fail)
    results = _budget.enforce_round_aggregate_budget_v2(
        {'b': (small, 'synthetic', 'b')}, user_id=17)
    evidence = split_tool_result_delivery(results['b'][0]).evidence
    assert evidence['retentionCoverage'] == 'unavailable'


@pytest.mark.parametrize('spilled', [(), (1,), (0, 1, 2)])
def test_batch_order_provenance_and_child_status_ignore_inline_policy(spilled):
    from lib.tools.result_value import (CanonicalReceipt, capture_result,
                                        compose_result, persist_result)
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    repo = ToolResultArtifactRepository()
    # Leaf text must be unique per variant: the module-scoped sidecar shares
    # one CAS store across parametrized variants, and tool-result artifacts are
    # content-addressed (the digest excludes the per-occurrence resultId, so
    # identical content collapses to one retained manifest). Reusing the same
    # text across variants would dedup onto an earlier variant's manifest and
    # surface that occurrence's resultId in provenance — a test-isolation bug,
    # not a provenance bug. Suffixing with the variant keeps each leaf's
    # content distinct so the provenance contract is exercised genuinely.
    tag = '|'.join(str(i) for i in spilled) or 'none'
    leaves = [capture_result(f'{text}#{tag}',
                             execution_status='error' if i == 1 else 'ok',
                             source_coverage='complete')
              for i, text in enumerate(('FIRST|', 'SECOND|', 'THIRD|'))]
    receipts = []
    for i, leaf in enumerate(leaves):
        receipt = CanonicalReceipt(name=f'tool-{i}', call_id=f'call-{i}',
                                   output='LOSSY_PREVIEW')
        if i in spilled:
            receipt['canonicalRef'] = persist_result(leaf, user_id=17)
        else:
            receipt.canonical = leaf
        receipts.append(receipt)
    batch = capture_result(compose_result('display only', receipts))
    ref = persist_result(batch, user_id=17)
    page = repo.read_range(user_id=17, artifact_ref=ref)
    assert page['content'] == ''.join(
        f'{text}#{tag}' for text in ('FIRST|', 'SECOND|', 'THIRD|'))
    assert [entry['path'][0]['callId'] for entry in page['provenance']] == [
        f'call-{i}' for i in range(3)]
    assert [entry['path'][0]['resultId'] for entry in page['provenance']] == [
        leaf.result_id for leaf in leaves]
    assert [entry['path'][0]['executionStatus'] for entry in page['provenance']] == [
        'ok', 'error', 'ok']
    assert not repo.search(user_id=17, artifact_ref=ref, query='LOSSY_PREVIEW')['items']


def test_posthook_redacts_descriptive_metadata_without_changing_media_identity():
    from lib.tools.result_value import (CapturedToolResult, ResultPart,
                                        capture_result, posthook_result, persist_result)
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    original = CapturedToolResult((
        ResultPart('text', 'SECRET', metadata=(('uri', 'test://resource?token=SECRET'),)),
        ResultPart('media', reference='tofu-media:original', mime_type='image/png',
                   metadata=(('caption', 'SECRET'), ('reason', 'SECRET'),
                             ('sha256', 'a' * 64), ('byteSize', 128))),
    ), execution_status='error')
    redacted = capture_result(posthook_result(original, lambda text: text.replace('SECRET', 'REDACTED')))
    assert 'SECRET' not in json.dumps(redacted.manifest())
    assert redacted.execution_status == 'error'
    assert redacted.parts[1].reference == original.parts[1].reference
    assert dict(redacted.parts[1].metadata)['sha256'] == 'a' * 64
    ref = persist_result(redacted, user_id=17)
    page = ToolResultArtifactRepository().read_range(user_id=17, artifact_ref=ref)
    assert 'SECRET' not in json.dumps(page)
    assert page['media'][0]['caption'] == 'REDACTED'


def test_failed_result_reprojection_preserves_error_and_historical_reference(monkeypatch):
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_value import capture_result, TrustedResultText
    from lib.tools.result_envelope import split_tool_result_delivery
    body = 'large diagnostic ' * 16000
    source = TrustedResultText(body, canonical=capture_result(body, execution_status='error'))
    first = _budget.budget_tool_result_v2('synthetic_error', source, user_id=17)
    old = split_tool_result_delivery(first).evidence
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 2000)
    second = _budget.budget_tool_result_v2('synthetic_error', first, user_id=17)
    delivered = split_tool_result_delivery(second)
    assert json.loads(delivered.model_text)['status'] == 'error'
    assert delivered.evidence['artifactRef'] == old['artifactRef']
    assert delivered.evidence['resultId'] == old['resultId']
    assert _budget._result_tokens(delivered.model_text, '') <= 2000


@pytest.mark.parametrize('storage_available', [True, False])
def test_inline_then_smaller_budget_publishes_source_or_reports_loss(monkeypatch, storage_available, caplog):
    from lib.tasks_pkg.compaction import _budget
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    body = 'evidence word ' * 1500 + 'TAIL_NEEDLE'
    first = _budget.budget_tool_result_v2('synthetic', body, user_id=17)
    original = split_tool_result_delivery(first).evidence
    assert not original.get('artifactRef') and not original['truncated']
    if not storage_available:
        def fail(*args, **kwargs):
            raise OSError('SECRET-backend-exception')
        monkeypatch.setattr(ToolResultArtifactRepository, 'put', fail)
    monkeypatch.setattr(_budget, 'TOOL_RESULT_V2_MAX_TOKENS', 2000)
    second = _budget.budget_tool_result_v2('synthetic', first, user_id=17)
    delivery = split_tool_result_delivery(second)
    assert delivery.evidence['resultId'] == original['resultId']
    assert delivery.evidence['truncated']
    assert 'TAIL_NEEDLE' not in delivery.model_text
    assert _budget._result_tokens(delivery.model_text, '') <= 2000
    if storage_available:
        ref = delivery.evidence['artifactRef']
        assert ref and ref in delivery.model_text
        assert delivery.evidence['retentionCoverage'] == 'complete'
        result = ToolResultArtifactRepository().search(
            user_id=17, artifact_ref=ref, query='TAIL_NEEDLE')
        assert len(result['items']) == 1
    else:
        assert not delivery.evidence.get('artifactRef')
        assert delivery.evidence['retentionCoverage'] == 'unavailable'
        assert 'retention unavailable' in delivery.model_text.lower()
        assert 'artifact persistence failed (OSError)' in caplog.text
        assert 'SECRET-backend-exception' not in caplog.text
        assert 'SECRET-backend-exception' not in delivery.model_text


@pytest.mark.parametrize('invalid', [b'\x89PNG\r\n', {'dataUrl': 'private media'}, '\ud800'])
def test_artifact_text_repository_rejects_media_and_invalid_encoding(invalid):
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    with pytest.raises((TypeError, UnicodeError)):
        ToolResultArtifactRepository().put(user_id=17, content=invalid)


def _screenshot_dict():
    return {
        '__screenshot__': True,
        'format': 'jpeg',
        'dataUrl': 'data:image/jpeg;base64,/9j/SYNTHETIC',
        '_text_fallback': 'Page preview — report body',
        '_modelReplaySources': ['tofu-media:shot-1'],
    }


def _capture_sink():
    class Sink:
        value = None

        def capture(self, call_id, value):
            self.value = value

    return Sink()


def test_screenshot_settle_capture_retains_media_reference(monkeypatch):
    """A screenshot's canonical capture keeps its media descriptor.

    Regression (conv mub06hs7u6ygp1): every screenshot settle lane flattened
    the ``__screenshot__`` dict to display text BEFORE ``capture_result``, so
    the artifact tree held only the report text — read_tool_artifact returned
    ``media: []`` while the manifest claimed ``retentionCoverage=complete``.
    The settle seam now captures the dict (with its retained
    ``_modelReplaySources``) as canonical evidence; the model-visible content
    stays the display text on the legacy contract.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, 'run_post_hooks',
                        lambda name, args, text, task: text)

    sink = _capture_sink()
    task = {'id': 'shot-settle', '_userId': 17, 'model': 'vision-model',
            'config': {'tools': {'resultEnvelope': 'legacy'}}}
    content = pipeline._settle_tool_result(
        task, 'browser_preview_page', 'shot-call', {}, 0, {},
        'Page preview — report body',
        idempotent_tools=frozenset(), cache={}, tid='shot-settle',
        round_num=1, internal_result_sink=sink,
        canonical_content=_screenshot_dict())

    assert str(content) == 'Page preview — report body'
    assert sink.value is not None
    manifest = sink.value.manifest()
    kinds = [part['kind'] for part in manifest['parts']]
    assert kinds == ['text', 'media']
    assert manifest['parts'][0]['text'] == 'Page preview — report body'
    assert manifest['parts'][1]['reference'] == 'tofu-media:shot-1'
    assert manifest['retentionCoverage'] == 'complete'


def test_screenshot_settle_v2_envelope_readback_returns_media(monkeypatch):
    """Under v2 the retained reference stays honest AND reads back the media.

    Two-part regression proof (conv mub06hs7u6ygp1):
      1. The manifest the envelope references now carries the retained
         ``tofu-media:`` descriptor, so ``read_tool_artifact`` reports the
         media instead of ``media: []``.
      2. A complete inline view no longer cries ``partial``/``truncated`` —
         the artifactRef is a post-compaction recovery handle, not an
         invitation to an immediate redundant read.
    """
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository

    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, 'run_post_hooks',
                        lambda name, args, text, task: text)

    sink = _capture_sink()
    row = {}
    task = {'id': 'shot-settle-v2', '_userId': 17, 'model': 'vision-model'}
    content = pipeline._settle_tool_result(
        task, 'browser_preview_page', 'shot-call', {}, 0, row,
        'Page preview — report body',
        idempotent_tools=frozenset(), cache={}, tid='shot-settle-v2',
        round_num=1, internal_result_sink=sink,
        canonical_content=_screenshot_dict())

    # The model projection is the complete inline content, not a pointer.
    assert str(content) == 'Page preview — report body'
    evidence = row.get('toolResultEvidence') or {}
    assert evidence.get('artifactRef'), row
    assert evidence.get('status') == 'ok'
    assert evidence.get('truncated') is False
    assert evidence.get('viewCoverage') == 'complete'
    assert evidence.get('retentionCoverage') == 'complete'

    got = ToolResultArtifactRepository().read_range(
        user_id=17, artifact_ref=evidence['artifactRef'],
        offset=0, limit=8192)
    assert got is not None
    assert 'Page preview — report body' in got['content']
    assert [m['reference'] for m in got['media']] == ['tofu-media:shot-1']
    assert got['retentionCoverage'] == 'complete'
    # Read coverage is honestly partial: the media part is not text.
    assert got['coverage'] == 'incomplete'


def test_captured_media_references_walk_inline_children_only():
    from lib.tools.result_value import (
        CanonicalReceipt, capture_result, compose_result)

    child = capture_result({
        '__screenshot__': True,
        '_text_fallback': 'report',
        '_modelReplaySources': ['tofu-media:inline-1'],
    })
    receipt = CanonicalReceipt(
        {'name': 'browser_preview_page', 'call_id': 'gw-1'}, canonical=child)
    spilled_receipt = CanonicalReceipt(
        {'name': 'read_files', 'call_id': 'gw-2',
         'canonicalRef': 'tool-result:' + 'a' * 64})
    composed = compose_result('{"status":"ok"}', [receipt, spilled_receipt])
    value = capture_result(composed)
    # Inline child media surfaces; a spilled child stays behind its artifact
    # ref (resolvable through read_tool_artifact descriptors, never guessed).
    assert value.media_references() == ['tofu-media:inline-1']
    assert capture_result('plain').media_references() == []


def _fake_shot_capture(task, value, **kwargs):
    refs = []
    for ordinal, payload in enumerate(value.image_contents):
        ref = {
            'attachmentId': f'att-{ordinal}',
            'mimeType': payload['mimeType'],
            'sourceTool': kwargs['source_tool'],
            'toolCallId': kwargs['tool_call_id'],
            'sourceOrdinal': ordinal,
        }
        task.setdefault(kwargs['reference_field'], []).append(ref)
        refs.append(ref)
    return len(refs)


def test_gateway_child_receipt_uses_frozen_media_projection(monkeypatch):
    """execute_tools receipts carry tofu-media refs, never inline base64.

    Regression: the child receipt output came from the live nested message
    (inline base64 blocks), so one screenshot made the gateway payload ~275KB
    and the v2 budget spilled it into a bare artifact pointer — the model
    never saw the image at all.
    """
    import threading
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat

    screenshot = {
        '__screenshot__': True,
        'format': 'jpeg',
        'dataUrl': 'data:image/jpeg;base64,' + ('QUJD' * 4096),
        '_text_fallback': 'Page preview — synthetic report',
    }

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        return call_id, screenshot, False

    monkeypatch.setattr(pipeline, '_execute_tool_one', execute)
    monkeypatch.setattr(heartbeat, '_execute_tool_one', execute)
    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, 'model_supports_vision', lambda m: True)
    monkeypatch.setattr(
        'lib.tasks_pkg.mcp_result_media.capture_mcp_result_images',
        _fake_shot_capture)
    tools = [{'type': 'function', 'function': {
        'name': 'synthetic_shot',
        'parameters': {'type': 'object', 'properties': {}}}}]
    task = {'id': 'gateway-shot', 'convId': '', '_userId': 17,
            'status': 'running', 'aborted': False,
            'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
            'model': 'vision-model', '_attended': False,
            'config': {'tools': {'resultEnvelope': 'legacy'}},
            '_executable_tool_catalog': tools}
    calls = [{'id': 'child-shot', 'type': 'function', 'function': {
        'name': 'synthetic_shot', 'arguments': '{}'}}]
    receipts = _execute_call_batch(
        task, calls, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, model='vision-model', llm_round=1)
    assert len(receipts) == 1
    output = receipts[0]['output']
    assert isinstance(output, list)
    assert output[0] == {'type': 'image_url',
                         'image_url': {'url': 'tofu-media:att-0'}}
    assert output[1]['type'] == 'text'
    assert 'Page preview' in output[1]['text']
    assert 'data:image' not in json.dumps(output)
    assert 'QUJD' not in json.dumps(output)
    row = task['toolRounds'][-1]
    assert row['toolModelContent'] == output


def test_pipeline_projects_composed_media_refs_to_vision_model(monkeypatch):
    """A composed gateway result reaches a vision model as real image blocks.

    The canonical tree retains the child's ``tofu-media:`` refs (sink capture
    above), so the appended tool message is multimodal: image blocks plus the
    receipt text. Text-only models keep the plain receipt string, and the v2
    aggregate-budget pass never clobbers the image blocks.
    """
    import threading
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    from lib.tools.result_value import (
        CanonicalReceipt, capture_result, compose_result)
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    child = capture_result({
        '__screenshot__': True,
        '_text_fallback': 'Page preview — synthetic report',
        '_modelReplaySources': ['tofu-media:att-0'],
    })
    receipt = CanonicalReceipt(
        {'name': 'browser_preview_page', 'call_id': 'gw-child'},
        canonical=child)
    composed = compose_result(
        json.dumps({'status': 'ok', 'results': [
            {'call_id': 'gw-child', 'name': 'browser_preview_page',
             'status': 'done'}]}),
        [receipt])

    def execute(task, tc, name, call_id, args, rn, row, *rest, **kwargs):
        return call_id, composed, False

    monkeypatch.setattr(pipeline, '_execute_tool_one', execute)
    monkeypatch.setattr(heartbeat, '_execute_tool_one', execute)
    monkeypatch.setattr(pipeline, 'append_event', lambda *args, **kwargs: None)

    def run(vision, contract='legacy'):
        monkeypatch.setattr(
            pipeline, 'model_supports_vision', lambda m: vision)
        task = {'id': 'gw-projection', 'convId': '', '_userId': 17,
                'status': 'running', 'aborted': False,
                'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
                'model': 'test-model', '_attended': False,
                'config': {'tools': {'resultEnvelope': contract}}}
        args = {'calls': [{'name': 'browser_preview_page', 'arguments': {}}]}
        _, row, _ = _build_tool_round_entry(
            'execute_tools', args, 'gw-call', json.dumps(args), 1, False)
        task['toolRounds'].append(row)
        tc = {'id': 'gw-call', 'type': 'function',
              'function': {'name': 'execute_tools',
                           'arguments': json.dumps(args)}}
        messages = []
        execute_tool_pipeline(
            task, [(tc, 'execute_tools', 'gw-call', args, row['roundNum'],
                    row, None)],
            cfg={'autoApply': True}, project_path=None, project_enabled=False,
            tool_list=[], messages=messages, all_search_results_text=[],
            round_num=0, model='test-model')
        return task, row, messages

    task, row, messages = run(vision=True)
    msg = messages[-1]
    assert msg['role'] == 'tool' and msg['tool_call_id'] == 'gw-call'
    blocks = msg['content']
    assert isinstance(blocks, list)
    assert blocks[0] == {'type': 'image_url',
                         'image_url': {'url': 'tofu-media:att-0'}}
    assert blocks[-1]['type'] == 'text'
    assert json.loads(blocks[-1]['text'])['status'] == 'ok'
    assert msg['_modelImageSources'] == ['tofu-media:att-0']
    assert row['toolModelContent'] == blocks

    task2, row2, messages2 = run(vision=False)
    assert isinstance(messages2[-1]['content'], str)
    assert json.loads(messages2[-1]['content'])['status'] == 'ok'
    assert 'toolModelContent' not in row2

    # v2: the reference-bearing result still retains a durable artifact, but
    # a complete inline view no longer claims partial/truncated — the model
    # holds everything, and the image blocks survive the aggregate-budget
    # rewrite untouched.
    task3, row3, messages3 = run(vision=True, contract='v2')
    blocks3 = messages3[-1]['content']
    assert isinstance(blocks3, list)
    assert blocks3[0] == {'type': 'image_url',
                          'image_url': {'url': 'tofu-media:att-0'}}
    assert blocks3[-1]['type'] == 'text'
    assert json.loads(blocks3[-1]['text'])['status'] == 'ok'
    assert row3['toolModelContent'] == blocks3
    evidence3 = row3.get('toolResultEvidence') or {}
    assert evidence3.get('artifactRef')
    assert evidence3.get('truncated') is False


def test_v2_envelope_reference_retention_is_not_marked_truncated():
    """Retaining a durable reference is not withholding.

    A reference-bearing result whose view is complete must say
    ok/complete — the artifactRef is a post-compaction recovery handle,
    never an invitation to an immediate redundant read. Only a view that
    actually dropped content is partial.
    """
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    from lib.tool_results.tool_result_artifacts import ToolResultArtifactRepository
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tools.result_value import TrustedResultText, capture_result

    captured = capture_result({
        '__screenshot__': True,
        '_text_fallback': 'Page preview — report body',
        '_modelReplaySources': ['tofu-media:shot-9'],
    })
    source = TrustedResultText('Page preview — report body',
                               canonical=captured)
    envelope = budget_tool_result_v2('browser_preview_page', source,
                                     user_id=17)
    value = json.loads(envelope)
    assert value['status'] == 'ok'
    assert value['truncated'] is False
    assert value['viewCoverage'] == 'complete'
    assert value['retentionCoverage'] == 'complete'
    assert value['artifactRef'].startswith('tool-result:')
    delivery = split_tool_result_delivery(envelope)
    assert delivery.model_text == 'Page preview — report body'
    got = ToolResultArtifactRepository().read_range(
        user_id=17, artifact_ref=value['artifactRef'], offset=0, limit=8192)
    assert [m['reference'] for m in got['media']] == ['tofu-media:shot-9']

    big_text = 'REPORT body ' * 6000
    big = capture_result({
        '__screenshot__': True,
        '_text_fallback': big_text,
        '_modelReplaySources': ['tofu-media:shot-9'],
    })
    big_source = TrustedResultText(big_text, canonical=big)
    big_value = json.loads(budget_tool_result_v2(
        'browser_preview_page', big_source, user_id=17))
    assert big_value['truncated'] is True
    assert big_value['status'] == 'partial'
    assert big_value['viewCoverage'] == 'partial'
    assert big_value['artifactRef'].startswith('tool-result:')


def test_read_tool_artifact_replays_retained_media(monkeypatch):
    """A read of a media-bearing artifact replays it to model and frontend.

    The handler's content carries canonical media parts (settle stamp →
    post-phase image blocks for a vision model) and the row meta carries
    resolvable imageDataUris for frontend tiles — previously both were
    text-only and the media silently stayed behind the digest.
    """
    from lib.tasks_pkg.handlers import tool_result_artifacts as handler
    from lib.tools.result_value import capture_result, persist_result

    captured = capture_result({
        '__screenshot__': True,
        '_text_fallback': 'Page preview — report body',
        '_modelReplaySources': ['tofu-media:shot-77'],
    })
    ref = persist_result(captured, user_id=17)

    finalized = {}
    monkeypatch.setattr(
        handler, '_finalize_tool_round',
        lambda task, rn, row, metas, **kw: finalized.setdefault(
            'metas', metas))
    task = {'id': 'read-media', '_userId': 17, 'model': 'vision-model'}
    tc_id, content, _ = handler._handle_tool_result_artifact(
        task, {}, 'read_tool_artifact', 'read-1',
        {'artifact_ref': ref}, 0, {}, {}, None, False)

    assert tc_id == 'read-1'
    canonical = getattr(content, 'canonical', None)
    assert canonical is not None
    assert canonical.media_references() == ['tofu-media:shot-77']
    assert canonical.retention_coverage == 'complete'
    projected = json.loads(str(content))
    assert projected['media'][0]['reference'] == 'tofu-media:shot-77'
    meta = finalized['metas'][0]
    uris = meta.get('imageDataUris') or []
    assert [u['uri'] for u in uris] == [
        '/api/v1/media/attachments/shot-77']
    assert uris[0]['format'] == 'img'
    assert uris[0]['filename'] == 'artifact-media-1.img'
