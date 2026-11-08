"""Channel media: downloadable originals of channel events (docs/CHANNELS.md C9).

Responsibility: keep provider file names safe, and resolve the pending media
of an event that is about to be routed, once. Each original is downloaded
through the adapter's ``MediaPort`` into a private scratch directory, stored
as an owner-scoped retained attachment (``lib.media_attachments``), and a
voice message is transcribed through the existing speech-recognition chain
(``lib.video_analysis.transcribe_track``). The outcome is recorded on the
event, so the replay of an unrouted event reuses it instead of downloading
again. A failed download is an outcome (``failed`` with its reason, shown in
the turn), never a silent drop. Files are stored, never executed or opened;
no media URL or file token reaches a log, a record or a rendered turn.

Entry points: ``safe_media_name``, ``MediaPort``, ``resolve_event_media``.
Dependencies: ``lib.channels.model``; ``lib.media_attachments``,
``lib.knowledge`` and ``lib.video_analysis`` are imported lazily.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from typing import Any, Protocol

from lib.channels.model import ChannelEvent, ChannelMedia
from lib.log import get_logger

logger = get_logger(__name__)

# The attachment contract's name bound (TurnMediaAttachment.name maxLength).
_NAME_LIMIT = 240


def safe_media_name(name: Any, fallback: str) -> str:
    """The original file name reduced to one plain path component.

    Directories (either separator), control characters and leading dots are
    removed, so no name can address another path or hide as a dotfile.
    """
    text = str(name or '').replace('\\', '/').rsplit('/', 1)[-1]
    text = ''.join(char for char in text if char >= ' ' and char != '\x7f').strip().lstrip('.')
    return text[:_NAME_LIMIT].strip() or fallback


class MediaPort(Protocol):
    def download_media(self, item: ChannelMedia, path: str) -> dict[str, Any]:
        """Write ``item``'s original to ``path``.

        Returns ``{'ok': True, 'contentType': …}`` or ``{'ok': False, 'detail': …}``
        (a reason safe to show) for this item. Raises only when the transport
        itself is gone, which halts ingest before the watermark moves (C1).
        """


def resolve_event_media(store, event: ChannelEvent, recorded: dict[str, Any] | None,
                        port: MediaPort | None) -> ChannelEvent:
    """``event`` with resolved media, taken from its record when already resolved."""
    if not event.media:
        return event
    previous = tuple(ChannelMedia.from_record(item) for item in (recorded or {}).get('media') or ())
    if previous and all(item.status != 'pending' for item in previous):
        return replace(event, media=previous)
    with tempfile.TemporaryDirectory(prefix='tofu-channel-media-') as scratch:
        media = tuple(_resolve(store.owner, event, item, port, os.path.join(scratch, str(index)))
                      for index, item in enumerate(event.media))
    store.record_event_media(event, [item.to_record() for item in media])
    return replace(event, media=media)


def _resolve(owner: int, event: ChannelEvent, item: ChannelMedia, port: MediaPort | None,
             scratch: str) -> ChannelMedia:
    if item.status != 'pending':
        return item
    if port is None:
        return replace(item, status='failed', detail='this channel cannot download media')
    os.makedirs(scratch)
    path = os.path.join(scratch, 'original')
    fetched = port.download_media(item, path)
    if not fetched.get('ok'):
        detail = str(fetched.get('detail') or 'download failed')
        logger.info('[Channels] media not downloaded event=%s kind=%s: %s',
                    event.event_id, item.kind, detail)
        return replace(item, status='failed', detail=detail)
    from lib.knowledge.ingest import KnowledgeIngestError
    from lib.media_attachments import ingest_received_file
    try:
        attachment = ingest_received_file(
            path, item.name, item.mime_type or str(fetched.get('contentType') or ''), user_id=owner,
            origin={'channel': event.channel, 'eventId': event.event_id})
    except KnowledgeIngestError as error:
        return replace(item, status='failed', detail=f'could not be stored: {error}')
    resolved = replace(item, status='attached', detail='', attachment=attachment,
                       mime_type=str(attachment.get('mimeType') or item.mime_type),
                       size_bytes=int(attachment.get('sizeBytes') or item.size_bytes))
    if item.kind != 'voice':
        return resolved
    from lib.video_analysis import transcribe_track
    outcome = transcribe_track(path, scratch, float(item.duration_s), owner_user_id=owner)
    return replace(resolved, transcript=str(outcome.get('text') or '').strip(),
                   transcript_status=str(outcome.get('status') or 'failed'),
                   transcript_detail=str(outcome.get('detail') or ''))


__all__ = ['MediaPort', 'resolve_event_media', 'safe_media_name']
