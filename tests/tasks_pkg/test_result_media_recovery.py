"""Disposable real storage; only OCR/parser is faked, never persistence."""
import base64
import copy
import hashlib
import io

import pytest
from PIL import Image

from tests.mcp_protocol.test_mcp_media_fidelity import call
from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._knowledge_sidecar',)


@pytest.mark.parametrize('dimensions', [(3, 2), (2600, 1800)])
def test_original_survives_storage_restart_and_model_resize(
        isolated_knowledge, knowledge_storage, monkeypatch, dimensions):
    # Real PNG validation and original writing/indexing/owner checks. Replace
    # only optional OCR extraction to avoid external model/parser dependencies.
    def parser(raw, name):
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
        return {'text': 'Synthetic recovery image evidence', 'kind': '.png',
                'method': 'test-parser-no-ocr', 'pages': 1, 'assets': []}
    monkeypatch.setattr(isolated_knowledge, 'extract', parser)
    output = io.BytesIO()
    Image.new('RGB', dimensions, 'blue').save(output, format='PNG')
    raw = output.getvalue()
    result = call([{'type': 'text', 'text': 'before'},
                   {'type': 'image', 'data': base64.b64encode(raw).decode(),
                    'mimeType': 'image/png', 'caption': 'original caption'},
                   {'type': 'text', 'text': 'after'}])
    task = {'_userId': 1}
    assert capture_mcp_result_images(task, result, source_tool='mcp__test__read', tool_call_id='recover') == 1
    receipt = copy.deepcopy(result.media_refs[0])
    ref = receipt['mediaRef']
    assert receipt['sourceOrdinal'] == 1 and receipt['caption'] == 'original caption'
    assert receipt['sha256'] == hashlib.sha256(raw).hexdigest()
    assert receipt['byteSize'] == len(raw) and receipt['view'] == 'source'
    assert result._modelReplaySources == [ref]
    # Exercise the same canonical/query path used by direct calls and batches.
    from lib.tools.result_value import capture_result, compose_result, persist_result
    from lib.tools.result_envelope import split_tool_result_delivery
    from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
    from lib.tasks_pkg.compaction._builtin_steps._toolresults import compact_tool_results
    from lib.tasks_pkg.compaction._steps import CompactionContext
    from lib.tool_result_artifacts import ToolResultArtifactRepository
    from types import SimpleNamespace
    import json
    canonical = capture_result(result)
    root_ref = persist_result(canonical, user_id=1)
    for depth in range(2):
        child = {'name': 'mcp__test__read', 'call_id': f'image-child-{depth}',
                 'canonicalRef': root_ref, 'output': 'LOSSY_IMAGE_PREVIEW'}
        carrier = compose_result(json.dumps({'results': [child]}), [child])
        view = budget_tool_result_v2('execute_tools', carrier, user_id=1)
        evidence = split_tool_result_delivery(view).evidence
        root_ref = evidence['artifactRef']
        assert evidence['retentionCoverage'] == 'complete'
    visible = 'lossy model preview ' * 100
    row = {'toolCallId': 'image-parent', 'toolName': 'execute_tools',
           'toolContent': visible, 'toolResultEvidence': evidence}
    saved = copy.deepcopy(row)
    context = [{'role': 'tool', 'tool_call_id': 'image-parent',
                'name': 'execute_tools', 'content': visible}]
    compact_tool_results(CompactionContext(context, task={'toolRounds': [row], '_userId': 1},
        constants=SimpleNamespace(MICRO_HOT_TAIL=0, MICRO_HOT_TAIL_TOKENS=0,
                                  MICRO_COMPACT_THRESHOLD=100)))
    assert root_ref in context[0]['content'] and row == saved
    knowledge_storage.stop()

    knowledge_storage.start()
    repository = ToolResultArtifactRepository()
    page = repository.read_range(user_id=1, artifact_ref=root_ref)
    assert len(page['media']) == 1
    media = page['media'][0]
    assert media['reference'] == ref and media['sha256'] == receipt['sha256']
    assert media['byteSize'] == len(raw) and media['sourceOrdinal'] == 1
    assert media['caption'] == 'original caption'
    assert base64.b64encode(raw).decode() not in json.dumps(page)
    assert not repository.search(user_id=1, artifact_ref=root_ref,
                                 query='LOSSY_IMAGE_PREVIEW')['items']
    assert len(repository.search(user_id=1, artifact_ref=root_ref,
                                 query='before')['items']) == 1
    assert repository.read_range(user_id=2, artifact_ref=root_ref) is None
    from lib.knowledge import read_source_path, get_document_metadata
    from lib.media_attachments import get_attachment
    aid = receipt['attachmentId']
    path = read_source_path(aid, user_id=1)
    assert path.read_bytes() == raw
    assert hashlib.sha256(path.read_bytes()).hexdigest() == receipt['sha256']
    assert get_document_metadata(aid, user_id=1)['scope'] == 'attachment'
    assert get_attachment(aid, user_id=1)['sourceUrl'].endswith('/' + aid + '/source')
    assert read_source_path(aid, user_id=2) is None
    assert get_attachment(aid, user_id=2) is None
    from lib.llm.body import _validate_image_blocks, _downscale_oversized_images
    source = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': ref}}]}]
    projected = copy.deepcopy(source)
    _validate_image_blocks(projected, user_id=1)
    hydrated = projected[0]['content'][0]['image_url']['url']
    assert base64.b64decode(hydrated.split(',', 1)[1]) == raw
    _downscale_oversized_images(projected, 'claude-sonnet-4-5', user_id=1)
    view = projected[0]['content'][0]['image_url']['url']
    assert (view != hydrated) == (dimensions[0] > 2000)
    assert source[0]['content'][0]['image_url']['url'] == ref
    assert result.media_refs[0] == receipt and result._modelReplaySources == [ref]
    assert path.read_bytes() == raw
    denied = copy.deepcopy(source)
    _validate_image_blocks(denied, user_id=2)
    assert denied[0]['content'][0]['type'] == 'text'
    assert 'unavailable' in denied[0]['content'][0]['text'].lower()
    # Loss of the disposable source yields a read-only unavailable projection,
    # not a request to re-execute the original (possibly mutating) tool.
    path.unlink()
    missing = copy.deepcopy(source)
    _validate_image_blocks(missing, user_id=1)
    assert missing[0]['content'][0]['type'] == 'text'
    assert 'unavailable' in missing[0]['content'][0]['text'].lower()
    assert result._modelReplaySources == [ref] and result.media_refs[0] == receipt


def test_content_count_and_rejection_receipts_are_bounded():
    from lib.mcp.result_content import MAX_MCP_CONTENT_BLOCKS, MAX_MCP_IMAGES_PER_TURN
    result = call([{'type': 'image', 'mimeType': 'image/svg+xml', 'data': 'SECRET'}] * 10000)
    assert len(result.image_contents) <= MAX_MCP_IMAGES_PER_TURN + 1
    assert len(result.content_blocks) <= MAX_MCP_CONTENT_BLOCKS + 1
    assert result.retention_error == 'content_block_limit'
    assert result.content_blocks[-1]['reason'] == 'content_block_limit'
    assert 'SECRET' not in str(result)


def test_oversized_text_and_aggregate_structured_do_not_stay_runtime(monkeypatch):
    monkeypatch.setattr('lib.mcp.types.MCP_MAX_RETAINED_DOCUMENT_BYTES', 1024)
    result = call([{'type': 'text', 'text': 'a' * 2000}], {'other': 'b' * 2000})
    assert result.retention_error
    assert result.document_text is None and result.structured_json is None
    assert all('text' not in block for block in result.content_blocks)


def test_unknown_sdk_metadata_never_calls_repr():
    class Unsafe:
        def __str__(self):
            raise AssertionError('SDK metadata repr leaked')
        __repr__ = __str__
    result = call([{'type': Unsafe(), 'uri': Unsafe()},
                   {'type': 'resource', 'resource': {'uri': Unsafe(), 'blob': Unsafe()}}])
    assert 'unknown content' in str(result)
    assert result.content_blocks[1]['reason'] == 'unsupported_resource_blob'
