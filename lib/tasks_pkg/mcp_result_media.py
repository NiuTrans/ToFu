"""Persist MCP image result blocks into the owner-scoped media authority.

This is the only task-side bridge from a string-compatible MCP result to Turn
media. It decodes under strict per-image/aggregate budgets, persists originals
through ``lib.media_attachments``, and stores only bounded references on the
task for the authoritative Turn projection.
"""

from __future__ import annotations

import base64
import binascii
import threading
from typing import Any

from lib.identity import require_user_id
from lib.log import get_logger
from lib.mcp.result_content import (
    MAX_MCP_IMAGE_BYTES,
    MAX_MCP_IMAGE_TOTAL_BYTES,
    MAX_MCP_IMAGES_PER_TURN,
)

logger = get_logger(__name__)


def capture_mcp_result_images(
    task: dict[str, Any],
    result: object,
    *,
    source_tool: str,
    tool_call_id: str,
) -> int:
    """Persist supported images from *result* and attach their Turn refs.

    Failures are per-image and non-fatal: the MCP text result still settles.
    Returns the number of new references appended to the task.
    """
    payloads = getattr(result, "image_contents", ())
    if not isinstance(payloads, (list, tuple)) or not payloads:
        return 0
    try:
        owner_user_id = require_user_id(
            task.get("_userId"), context="MCP result image owner"
        )
    except (TypeError, ValueError) as exc:
        logger.warning("[MCP:Media] skipped images without owner: %s", exc)
        return 0

    media_lock = task.setdefault("_mcpMediaLock", threading.Lock())
    appended = 0
    with media_lock:
        images = task.setdefault("_mcpImages", [])
        if not isinstance(images, list):
            images = []
            task["_mcpImages"] = images
        total_bytes = int(task.get("_mcpImageBytes") or 0)
        existing_ids = {
            str(item.get("attachmentId") or "")
            for item in images
            if isinstance(item, dict)
        }
        for ordinal, payload in enumerate(payloads):
            if len(images) >= MAX_MCP_IMAGES_PER_TURN:
                logger.warning("[MCP:Media] turn image count budget reached")
                break
            if not isinstance(payload, dict):
                continue
            encoded = payload.get("data")
            mime_type = str(payload.get("mimeType") or "")
            if not isinstance(encoded, str) or not encoded:
                continue
            compact = "".join(encoded.split())
            estimated_bytes = (len(compact) * 3) // 4
            if estimated_bytes > MAX_MCP_IMAGE_BYTES:
                logger.warning(
                    "[MCP:Media] skipped oversized image from %s (%d bytes estimated)",
                    source_tool,
                    estimated_bytes,
                )
                continue
            if total_bytes + estimated_bytes > MAX_MCP_IMAGE_TOTAL_BYTES:
                logger.warning("[MCP:Media] turn image byte budget reached")
                break
            try:
                raw = base64.b64decode(compact, validate=True)
                if not raw or len(raw) > MAX_MCP_IMAGE_BYTES:
                    raise ValueError("decoded image exceeds the per-image budget")
                if total_bytes + len(raw) > MAX_MCP_IMAGE_TOTAL_BYTES:
                    logger.warning("[MCP:Media] turn image byte budget reached")
                    break
                from lib.media_attachments import ingest_mcp_image

                image_ref = ingest_mcp_image(
                    raw,
                    mime_type,
                    user_id=owner_user_id,
                    source_tool=source_tool,
                    tool_call_id=tool_call_id,
                    ordinal=ordinal,
                )
            except (binascii.Error, ValueError, TypeError) as exc:
                logger.warning(
                    "[MCP:Media] rejected image %d from %s: %s",
                    ordinal,
                    source_tool,
                    exc,
                )
                continue
            except Exception as exc:  # storage/ingest failure must not fail tool text
                logger.warning(
                    "[MCP:Media] failed to persist image %d from %s: %s",
                    ordinal,
                    source_tool,
                    exc,
                    exc_info=True,
                )
                continue
            attachment_id = str(image_ref.get("attachmentId") or "")
            if not attachment_id or attachment_id in existing_ids:
                continue
            images.append(image_ref)
            existing_ids.add(attachment_id)
            total_bytes += len(raw)
            appended += 1
        task["_mcpImageBytes"] = total_bytes
    return appended


__all__ = ["capture_mcp_result_images"]
