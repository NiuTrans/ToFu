"""Projection of a human steer's image attachments for its records and the model.

A mid-turn human steer carries its full user message in the inbox item's
``_user_msg`` (``lib/conversation_sync/command_service.py``). The injection
chip and its display-only ``_userSteerInjects`` sidecar historically kept only
the text, so the operator's images silently vanished. ``steer_preview_images``
projects ``images`` into the bounded ``{preview, url, caption, sizeKB}`` shape
the frontend renders (``TurnImageAttachment``), shared by the durable pending
record (``turn.steer.commit`` on both storage authorities) and the delivered
flush (``lib/tasks_pkg/orchestrator/_deferred_inbox_flush.py``). An uploaded
image is kept as its stored ``/api/images/<f>`` link; only an image whose
upload failed keeps its inline ``data:`` preview, the one copy that exists.

``steer_model_images`` turns those dicts into user-message images the message
builder renders (``conv_message_builder.api.image_content_blocks``), so a
steered screenshot reaches the model the way it would as a new turn: the
round-boundary inbox carrier (``orchestrator/_swarm_inbox.py``) and a steer
requeued as the next Turn (``tasks_pkg/committed_steers.py``).

Bounded and never raises: image entries are capped and a record never carries
a ``base64`` field.
"""

from __future__ import annotations

from typing import Any, Mapping

from lib.attachments import canonical_image_ref
from lib.turn_image_transport import legacy_turn_image_payload

MAX_PREVIEW_IMAGES = 9
_CAPTION_UNITS = 200


def _stored_link(image: Mapping[str, Any]) -> str:
    """The canonical ``/api/images/<f>`` link of an uploaded image, or ''."""
    for key in ('url', 'preview'):
        value = image.get(key)
        if isinstance(value, str) and not value.startswith('data:'):
            link = canonical_image_ref(value.strip())
            if link:
                return link
    return ''


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
        link = _stored_link(image)
        preview = image.get('preview')
        if link:
            entry: dict[str, Any] = {'preview': link, 'url': link}
        elif isinstance(preview, str) and preview.strip():
            entry = {'preview': preview.strip()}
        else:
            continue
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


def steer_model_images(item: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The steer's projected images as user-message images for the model.

    A stored upload keeps its ``url``, which the request body resolves from
    disk. An inline ``data:`` preview is split into the ``base64``/``mediaType``
    pair the message builder reads; a preview that is neither is dropped.
    """
    out: list[dict[str, Any]] = []
    for image in steer_preview_images(item):
        if image.get('url'):
            out.append(image)
            continue
        payload = legacy_turn_image_payload(image)
        if payload is not None:
            out.append({**image, 'base64': payload.base64_data,
                        'mediaType': payload.media_type})
    return out
