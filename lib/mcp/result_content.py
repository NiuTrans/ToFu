"""Runtime-only MCP carrier: display is never the canonical document or media.

Native bytes travel only to the existing owner-scoped media ingestion boundary.
Ordered descriptors and retention receipts contain no base64.
"""
from __future__ import annotations

from typing import TypedDict

MAX_MCP_CONTENT_BLOCKS = 1024


def safe_field_text(value, default=''):
    """Do not invoke SDK __str__/repr on untrusted metadata."""
    return value[:2048] if isinstance(value, str) else default


MAX_MCP_IMAGES_PER_TURN = 20
MAX_MCP_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MCP_IMAGE_TOTAL_BYTES = 40 * 1024 * 1024
_ALLOWED_IMAGE_MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/bmp'}


class MCPImageContent(TypedDict, total=False):
    data: str
    mimeType: str
    sourceOrdinal: int
    status: str
    reason: str
    caption: str


class MCPToolResult(str):
    """String serialization is display-only; attributes are runtime payloads."""

    def __new__(cls, text, *, image_contents=(), structured_json=None,
                document_text=None, document_error=None, is_error=False,
                content_blocks=(), retention_error=None):
        value = super().__new__(cls, text)
        value.image_contents = tuple(image_contents)
        value.structured_json = structured_json
        value.document_text = document_text
        value.document_error = document_error
        value.retention_error = retention_error
        value.is_error = is_error
        value.content_blocks = tuple(content_blocks)
        value.media_refs = ()
        return value


def block_field(block, name, default=None):
    return block.get(name, default) if isinstance(block, dict) else getattr(block, name, default)


def bounded_content_blocks(result, *, max_bytes):
    """Bound aggregate text before joins/copies; return one loss receipt."""
    blocks, used, loss = [], 0, None
    content = block_field(result, 'content', ()) or ()
    for ordinal, block in enumerate(content):
        if ordinal >= MAX_MCP_CONTENT_BLOCKS:
            loss = 'content_block_limit'
            blocks.append({'type': 'unavailable', 'reason': loss,
                           'status': 'unavailable', 'sourceOrdinal': ordinal,
                           'omittedCount': len(content) - ordinal if isinstance(content, (list, tuple)) else None})
            break
        kind = safe_field_text(block_field(block, 'type'), 'unknown')
        entry = {'type': kind}
        if kind == 'image':
            for key in ('data', 'mimeType', 'mime_type', 'caption'):
                value = block_field(block, key)
                if isinstance(value, str):
                    entry[key] = value if key == 'data' else value[:2048]
        resource = block_field(block, 'resource', {}) if kind == 'resource' else block
        text = block_field(resource, 'text')
        if isinstance(text, str):
            # Four bytes per character is only an allocation upper bound;
            # measure exactly after a cheap character bound check.
            if len(text) > max_bytes - used or len(text.encode('utf-8', errors='replace')) > max_bytes - used:
                loss = 'document_retention_limit'
                entry.update(status='unavailable', reason=loss)
                text = '[content unavailable: document retention limit]'
            else:
                used += len(text.encode('utf-8', errors='replace')) + 1
            entry['text'] = text
        uri = safe_field_text(block_field(resource, 'uri'))
        if uri:
            entry['uri'] = uri
        if kind == 'resource':
            resource_entry = {key: entry.pop(key) for key in ('text', 'uri') if key in entry}
            if block_field(resource, 'blob') is not None:
                resource_entry['blob'] = True  # Presence only, never the bytes.
            entry['resource'] = resource_entry
        blocks.append(entry)
    return blocks, loss


def extract_mcp_image_contents(result):
    """Bound native data before decoding; rejected blocks remain explicit."""
    images = []
    total = 0
    for ordinal, block in enumerate(block_field(result, 'content', ()) or ()):
        if ordinal >= MAX_MCP_CONTENT_BLOCKS:
            images.append({'status': 'unavailable', 'reason': 'content_block_limit',
                           'sourceOrdinal': ordinal, 'mimeType': ''})
            break
        if safe_field_text(block_field(block, 'type', '')).lower() != 'image':
            continue
        data = block_field(block, 'data')
        mime = safe_field_text(block_field(block, 'mimeType') or block_field(block, 'mime_type')).lower().strip()
        if mime == 'image/jpg':
            mime = 'image/jpeg'
        item = {'mimeType': mime, 'sourceOrdinal': ordinal}
        caption = block_field(block, 'caption')
        if isinstance(caption, str):
            item['caption'] = caption[:2048]
        reason = None
        if len(images) >= MAX_MCP_IMAGES_PER_TURN:
            item.update(status='unavailable', reason='image_count_limit', aggregate=True)
            images.append(item)
            break
        elif mime not in _ALLOWED_IMAGE_MIME_TYPES:
            reason = 'unsupported_mime'
        elif not isinstance(data, str) or not data:
            reason = 'invalid_base64'
        elif len(data) > 4 * ((MAX_MCP_IMAGE_BYTES + 2) // 3):
            reason = 'image_byte_limit'
        elif total + len(data) > 4 * ((MAX_MCP_IMAGE_TOTAL_BYTES + 2) // 3):
            reason = 'image_total_byte_limit'
        if reason:
            item.update(status='unavailable', reason=reason)
        else:
            item['data'] = data
            total += len(data)
        images.append(item)
    return tuple(images)


__all__ = ['MAX_MCP_IMAGES_PER_TURN', 'MAX_MCP_IMAGE_BYTES',
           'MAX_MCP_IMAGE_TOTAL_BYTES', 'MCPImageContent', 'MCPToolResult',
           'extract_mcp_image_contents', 'block_field']
