"""Single MCP/native-image retention boundary using owner-scoped attachments.

Receipts distinguish immutable source bytes from model-resized views. Invalid,
over-budget and storage-failed inputs remain typed unavailable, never saved.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import io
import threading
import warnings
from typing import Any

from lib.identity import require_user_id
from lib.log import get_logger
from lib.mcp.result_content import (
    MAX_MCP_IMAGE_BYTES, MAX_MCP_IMAGE_TOTAL_BYTES, MAX_MCP_IMAGES_PER_TURN,
)

logger = get_logger(__name__)


def _validate_image(raw):
    # Verify format without changing/re-encoding originals or allocating pixels.
    from PIL import Image
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {'PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'}:
                raise ValueError('unsupported image format')
            mime = Image.MIME.get(image.format)
            image.verify()
    return mime


def capture_mcp_result_images(task: dict[str, Any], result: object, *,
                              source_tool: str, tool_call_id: str,
                              reference_field: str = '_mcpImages') -> int:
    """Persist originals; publish base64-free receipts on the runtime carrier."""
    if reference_field not in {'_mcpImages', '_toolModelImages'}:
        raise ValueError('unsupported media reference lane')
    payloads = getattr(result, 'image_contents', ())
    if not isinstance(payloads, (list, tuple)) or not payloads:
        return 0
    # The same carrier may cross more than one finalization adapter.
    capture_key = (source_tool, tool_call_id, reference_field)
    if getattr(result, '_media_capture_key', None) == capture_key:
        return 0
    try:
        owner = require_user_id(task.get('_userId'), context='MCP result image owner')
    except (ValueError, TypeError):
        owner = None
    lock = task.setdefault('_mcpMediaLock', threading.Lock())
    appended = 0
    receipts = []
    with lock:
        images = task.setdefault(reference_field, [])
        total = int(task.get('_mcpImageBytes') or 0)
        for index, payload in enumerate(payloads):
            if index >= MAX_MCP_IMAGES_PER_TURN:
                receipts.append({'type': 'image', 'status': 'unavailable',
                    'sourceOrdinal': index, 'view': 'source', 'mimeType': '',
                    'reason': 'image_count_limit', 'omittedCount': len(payloads) - index})
                break
            payload = payload if isinstance(payload, dict) else {}
            ordinal = payload.get('sourceOrdinal', index)
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
                ordinal = index
            receipt = {'type': 'image', 'status': 'unavailable',
                       'sourceTool': source_tool, 'toolCallId': tool_call_id,
                       'sourceOrdinal': ordinal, 'view': 'source',
                       'mimeType': payload.get('mimeType', '') if isinstance(payload.get('mimeType', ''), str) else ''}
            if isinstance(payload.get('caption'), str):
                receipt['caption'] = payload['caption'][:2048]
            receipts.append(receipt)
            reason = payload.get('reason') if payload.get('status') == 'unavailable' else None
            if not owner:
                reason = reason or 'owner_unavailable'
            if (len(task.get('_mcpImages') or []) + len(task.get('_toolModelImages') or [])
                    >= MAX_MCP_IMAGES_PER_TURN):
                reason = reason or 'image_count_limit'
            encoded = payload.get('data')
            if not isinstance(encoded, str) or not encoded:
                reason = reason or 'invalid_base64'
            elif len(encoded) > 4 * ((MAX_MCP_IMAGE_BYTES + 2) // 3):
                reason = reason or 'image_byte_limit'
            if reason:
                receipt['reason'] = reason
                continue
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                receipt['reason'] = 'invalid_base64'
                continue
            receipt.update(sha256=hashlib.sha256(raw).hexdigest(), byteSize=len(raw))
            if not raw or len(raw) > MAX_MCP_IMAGE_BYTES:
                receipt['reason'] = 'image_byte_limit'
                continue
            if total + len(raw) > MAX_MCP_IMAGE_TOTAL_BYTES:
                receipt['reason'] = 'image_total_byte_limit'
                continue
            try:
                actual_mime = _validate_image(raw)
            except Exception as exc:
                logger.debug('[MCP:Media] image %s rejected (%s)', ordinal, type(exc).__name__)
                receipt['reason'] = 'invalid_image'
                continue
            declared = receipt['mimeType'].lower().strip()
            if declared == 'image/jpg':
                declared = 'image/jpeg'
            if actual_mime != declared:
                receipt['reason'] = 'mime_mismatch'
                continue
            try:
                from lib.media_attachments import ingest_mcp_image
                image_ref = ingest_mcp_image(raw, actual_mime, user_id=owner,
                    source_tool=source_tool, tool_call_id=tool_call_id, ordinal=ordinal)
                attachment_id = str(image_ref.get('attachmentId') or '')
                if not attachment_id:
                    raise ValueError('media authority returned no attachment ID')
            except Exception:
                # Log neither binary payloads nor backend exception text/secrets.
                logger.warning('[MCP:Media] retention failed for image %s', ordinal)
                receipt['reason'] = 'retention_failed'
                continue
            receipt.update(status='available', attachmentId=attachment_id,
                           mediaRef='tofu-media:' + attachment_id, mimeType=actual_mime)
            image_ref.update(sourceOrdinal=ordinal, sha256=receipt['sha256'],
                             byteSize=len(raw), view='source')
            if 'caption' in receipt:
                image_ref['caption'] = receipt['caption']
            images.append(image_ref)
            total += len(raw)
            appended += 1
        task['_mcpImageBytes'] = total
        # Runtime-only refs, not a second media store. Canonical capture reads
        # these attributes before reducing the carrier to its display string.
        result.media_refs = tuple(receipts)
        result._modelReplaySources = [r['mediaRef'] for r in receipts if r['status'] == 'available']
        result._media_capture_key = capture_key
        result.image_contents = ()  # Durable refs replace runtime-only encoded bytes.
    return appended


__all__ = ['capture_mcp_result_images']
