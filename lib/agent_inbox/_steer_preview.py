"""Projection of a human message's image attachments for injection previews.

A mid-turn human steer carries its full user message in the inbox item's
``_user_msg`` (``lib/conversation_sync/command_service.py``). The injection
chip and its display-only ``_userSteerInjects`` sidecar historically kept only
the text, so the operator's images silently vanished from the rendered chip
even though the model received them. This helper projects ``images`` into the
bounded ``{preview, caption, sizeKB}`` shape the frontend renders
(``TurnImageAttachment``), shared by the durable pending record
(``lib/storage_sidecar/operations_pkg/_turns_write.py``) and the delivered
flush (``lib/tasks_pkg/orchestrator/_deferred_inbox_flush.py``).

Bounded and never raises: image entries are capped, and only already-resolved
display sources (``preview`` URL) are carried — no binary/base64 payloads.
"""

from __future__ import annotations

from typing import Any, Mapping

MAX_PREVIEW_IMAGES = 9
_CAPTION_UNITS = 200


def steer_preview_images(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Project ``item['_user_msg'].images`` into bounded display dicts.

    Returns [] when the steer carried no usable image previews.
    """
    user_msg = item.get('_user_msg')
    if not isinstance(user_msg, Mapping):
        return []
    images = user_msg.get('images')
    if not isinstance(images, list):
        return []
    out: list[dict[str, Any]] = []
    for image in images:
        if not isinstance(image, Mapping):
            continue
        preview = image.get('preview')
        if not isinstance(preview, str) or not preview.strip():
            continue
        entry: dict[str, Any] = {'preview': preview.strip()}
        caption = image.get('caption')
        if isinstance(caption, str) and caption.strip():
            entry['caption'] = caption.strip()[:_CAPTION_UNITS]
        size_kb = image.get('sizeKB')
        if isinstance(size_kb, (int, float)) and not isinstance(size_kb, bool):
            entry['sizeKB'] = size_kb
        elif isinstance(size_kb, str) and size_kb.strip():
            entry['sizeKB'] = size_kb.strip()
        out.append(entry)
        if len(out) >= MAX_PREVIEW_IMAGES:
            break
    return out
