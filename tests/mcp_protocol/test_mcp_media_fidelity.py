"""Synthetic MCP media fidelity, no network or user-data fixtures."""
import asyncio
import base64
import hashlib
import io
from types import SimpleNamespace

import pytest
from PIL import Image
from lib.mcp.client._bridge import MCPBridge
from lib.mcp.result_content import MCPToolResult
from lib.tasks_pkg.mcp_result_media import capture_mcp_result_images

pytestmark = pytest.mark.unit


def png():
    output = io.BytesIO()
    Image.new('RGB', (3, 2), 'blue').save(output, format='PNG')
    return output.getvalue()


def call(blocks, structured=None, error=False):
    async def run(*args, **kwargs):
        return SimpleNamespace(content=blocks, structuredContent=structured, isError=error)
    handle = SimpleNamespace(name='fake', sdk_generation=0, session=SimpleNamespace(call_tool=run))
    return asyncio.run(MCPBridge()._async_call_tool(handle, 'read', {}, None))


def test_mixed_document_original_bytes_order_and_caption(monkeypatch):
    raw = png()
    encoded = base64.b64encode(raw).decode()
    text = 'before ' + '中' * 210000
    result = call([{'type': 'text', 'text': text},
                   {'type': 'image', 'data': encoded, 'mimeType': 'image/png', 'caption': 'original caption'},
                   {'type': 'text', 'text': 'after'}], {'table': [1, 2]})
    seen = []
    def ingest(data, mime, **kwargs):
        seen.append(data)
        return {'attachmentId': 'synthetic', 'toolCallId': 'c', 'sourceTool': 'mcp__x__read'}
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', ingest)
    task = {'_userId': 7}
    assert capture_mcp_result_images(task, result, source_tool='mcp__x__read', tool_call_id='c') == 1
    assert seen == [raw]
    assert result.document_text.startswith(text)
    assert result.document_text.endswith('{"table": [1, 2]}')
    assert result.structured_json == '{"table": [1, 2]}'
    assert '[Truncated:' in str(result) and encoded not in str(result)
    assert [b['type'] for b in result.content_blocks] == ['text', 'image', 'text']
    receipt = result.media_refs[0]
    assert receipt['sourceOrdinal'] == 1
    assert receipt['sha256'] == hashlib.sha256(raw).hexdigest()
    assert receipt['caption'] == 'original caption' and receipt['view'] == 'source'
    assert result._modelReplaySources == ['tofu-media:synthetic']
    assert task['_mcpImages'][0]['caption'] == 'original caption'
    assert capture_mcp_result_images(task, result, source_tool='mcp__x__read', tool_call_id='c') == 0


@pytest.mark.parametrize('data,mime,reason', [('not base64!', 'image/png', 'invalid_base64'),
    (base64.b64encode(b'not an image').decode(), 'image/png', 'invalid_image'),
    (base64.b64encode(png()).decode(), 'image/jpeg', 'mime_mismatch'),
    ('eA==', 'image/svg+xml', 'unsupported_mime')])
def test_invalid_media_is_typed_unavailable(monkeypatch, data, mime, reason):
    result = call([{'type': 'image', 'data': data, 'mimeType': mime}])
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda *a, **k: pytest.fail('invalid input persisted'))
    assert capture_mcp_result_images({'_userId': 1}, result, source_tool='mcp__x__r', tool_call_id='c') == 0
    assert result.media_refs[0]['status'] == 'unavailable'
    assert result.media_refs[0]['reason'] == reason
    assert not result._modelReplaySources


def test_retention_failure_does_not_claim_saved(monkeypatch, caplog):
    result = call([{'type': 'image', 'data': base64.b64encode(png()).decode(), 'mimeType': 'image/png'}])
    def fail(*args, **kwargs):
        raise OSError('SECRET-media-store-detail')
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', fail)
    task = {'_userId': 1}
    assert capture_mcp_result_images(task, result, source_tool='mcp__x__r', tool_call_id='c') == 0
    assert result.media_refs[0]['reason'] == 'retention_failed'
    assert task['_mcpImages'] == [] and not result._modelReplaySources
    assert 'retention failed for image 0' in caplog.text
    assert 'SECRET-media-store-detail' not in caplog.text
    assert 'SECRET-media-store-detail' not in str(result.media_refs)


def test_large_errors_and_body_are_canonical_not_preview():
    full = 'error detail ' * 50000
    result = call([{'type': 'text', 'text': full}], {'error': 'details'}, error=True)
    assert result.is_error
    assert full in result.document_text
    assert len(str(result)) < len(result.document_text)
    assert result.structured_json == '{"error": "details"}'


def test_oversized_native_data_not_retained_as_truncated_base64(monkeypatch):
    monkeypatch.setattr('lib.mcp.result_content.MAX_MCP_IMAGE_BYTES', 3)
    result = call([{'type': 'image', 'data': 'a' * 100, 'mimeType': 'image/png'}])
    assert 'data' not in result.image_contents[0]
    capture_mcp_result_images({'_userId': 1}, result, source_tool='mcp__x__r', tool_call_id='c')
    assert result.media_refs[0]['reason'] == 'image_byte_limit'


def test_embedded_resource_text_is_not_sdk_repr_or_blob():
    result = call([{'type': 'resource', 'resource': {'uri': 'synthetic:body', 'text': 'complete body'}},
                   {'type': 'resource', 'resource': {'uri': 'synthetic:binary', 'blob': 'SECRETBASE64'}}])
    assert 'complete body' in result.document_text
    assert result.content_blocks[0]['text'] == 'complete body'
    assert result.content_blocks[1]['reason'] == 'unsupported_resource_blob'
    assert 'SECRETBASE64' not in str(result)


def test_owner_unavailable_is_explicit_and_no_store(monkeypatch):
    result = call([{'type': 'image', 'data': base64.b64encode(png()).decode(), 'mimeType': 'image/png'}])
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda *a, **k: pytest.fail('no owner'))
    capture_mcp_result_images({}, result, source_tool='mcp__x__r', tool_call_id='c')
    assert result.media_refs[0]['reason'] == 'owner_unavailable'


def test_exact_decoded_byte_limit_accepts_base64_padding(monkeypatch):
    raw = png()
    monkeypatch.setattr('lib.tasks_pkg.mcp_result_media.MAX_MCP_IMAGE_BYTES', len(raw))
    monkeypatch.setattr('lib.media_attachments.ingest_mcp_image', lambda *a, **k: {'attachmentId': 'exact'})
    result = MCPToolResult('x', image_contents=({'data': base64.b64encode(raw).decode(), 'mimeType': 'image/png'},))
    assert capture_mcp_result_images({'_userId': 1}, result, source_tool='mcp__x__r', tool_call_id='c') == 1
    assert result.media_refs[0]['byteSize'] == len(raw)
