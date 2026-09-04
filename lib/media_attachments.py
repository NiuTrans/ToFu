"""Owner-scoped chat attachment facade over the durable knowledge authority.

Conversation turns store only the bounded references returned here. Originals,
parsed chunks, video transcripts and visual evidence keep one lifecycle in
``lib.knowledge``; model projection resolves them just in time under the
authenticated owner instead of replaying megabytes through every turn.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)

_MAX_ATTACHMENTS_PER_TURN = 20
# Fallback per-attachment text budget for models whose context window is
# unknown; known windows scale up from this floor (document_text_budget).
_DOCUMENT_TEXT_CAP = 48_000
_MAX_DOCUMENT_TEXT_CAP = 240_000
_VIDEO_TEXT_CAP = 40_000
_DOCUMENT_IMAGE_CAP = 4
# Fallback whole-request attachment-text budget (unknown window); the
# model-aware request cap is 2x the per-attachment budget.
MODEL_TEXT_REQUEST_CAP = 96_000


def document_text_budget(model: str) -> int:
    """Per-attachment extracted-text budget scaled to the model's window.

    12% of the context window (chars ≈ tokens × 4), clamped to
    [_DOCUMENT_TEXT_CAP, _MAX_DOCUMENT_TEXT_CAP]. Unknown models keep the
    48k floor — the sliding-window read_files path covers the rest.
    """
    try:
        from lib.model_info import resolved_context_profile
        window = int(
            (resolved_context_profile(model or '') or {}).get('window') or 0)
    except Exception as exc:
        logger.debug('[Media] context profile lookup failed: %s', exc)
        window = 0
    if window <= 0:
        return _DOCUMENT_TEXT_CAP
    scaled = int(window * 0.12 * 4)
    return max(_DOCUMENT_TEXT_CAP, min(scaled, _MAX_DOCUMENT_TEXT_CAP))


def document_text_request_cap(model: str) -> int:
    """Whole-request attachment-text budget (all attachments combined)."""
    return 2 * document_text_budget(model)


@dataclass(slots=True)
class MediaProjectionBudget:
    """Mutable request-local text allowance shared across all turn messages."""

    text_chars_remaining: int = MODEL_TEXT_REQUEST_CAP
    attachments_remaining: int = 1

    def available(self, per_attachment_cap: int) -> int:
        planned = max(1, int(self.attachments_remaining))
        fair_share = (
            max(0, int(self.text_chars_remaining)) + planned - 1
        ) // planned
        self.attachments_remaining = max(0, planned - 1)
        return max(0, min(int(per_attachment_cap), fair_share))

    def skip_attachment(self) -> None:
        self.attachments_remaining = max(
            0, int(self.attachments_remaining) - 1)

    def consume(self, count: int) -> None:
        self.text_chars_remaining = max(
            0, int(self.text_chars_remaining) - max(0, int(count)))


def attachment_ref(document: dict) -> dict:
    """Project canonical storage metadata into the conversation contract."""
    metadata = document.get('media_metadata')
    if not isinstance(metadata, dict):
        metadata = {}
    attachment_id = str(document.get('id') or '')
    media_kind = str(metadata.get('media_kind') or 'document')
    if media_kind not in {'document', 'video'}:
        media_kind = 'document'
    status = str(metadata.get('status') or 'ready')
    if status not in {'processing', 'ready', 'failed', 'unavailable'}:
        status = 'unavailable'
    mime_type = str(metadata.get('mime_type') or '')
    if not mime_type:
        mime_type = mimetypes.guess_type(
            str(document.get('name') or ''))[0] or ''
    result = {
        'attachmentId': attachment_id,
        'kind': media_kind,
        'name': str(document.get('name') or 'attachment')[:240],
        'status': status,
        'mimeType': mime_type[:255],
        'sizeBytes': int(document.get('size_bytes') or 0),
        'sourceUrl': f'/api/v1/media/attachments/{attachment_id}/source',
    }
    if media_kind == 'document':
        result.update({
            'pages': int(document.get('pages') or 0),
            'textChars': int(document.get('text_chars') or 0),
            'method': str(document.get('method') or '')[:255],
        })
    else:
        poster_asset_id = str(metadata.get('poster_asset_id') or '')
        result.update({
            'durationSeconds': float(metadata.get('duration_s') or 0),
            'width': int(metadata.get('width') or 0),
            'height': int(metadata.get('height') or 0),
            'frameCount': int(metadata.get('frame_count') or 0),
            'avgFrameBytes': int(metadata.get('avg_frame_bytes') or 0),
            'transcriptStatus': str(
                metadata.get('transcript_status') or 'none')[:32],
        })
        if poster_asset_id:
            result['previewUrl'] = (
                f'/api/v1/knowledge/assets/{poster_asset_id}?thumbnail=1')
    error = str(metadata.get('error') or '')[:2000]
    if error:
        result['error'] = error
    return result


def get_attachment(attachment_id: str, *, user_id: int) -> dict | None:
    from lib.knowledge import get_document_metadata

    document = get_document_metadata(
        str(attachment_id or ''), user_id=require_user_id(
            user_id, context='media attachment lookup'))
    return attachment_ref(document) if document is not None else None


def ingest_document(
    raw: bytes, filename: str, *, user_id: int,
    command_id: str | None = None,
) -> dict:
    from lib.knowledge import add_document

    document = add_document(
        raw, filename, user_id=user_id, scope='draft',
        command_id=command_id)
    return attachment_ref(document)



def ingest_mcp_image(
    raw: bytes,
    mime_type: str,
    *,
    user_id: int,
    source_tool: str,
    tool_call_id: str,
    ordinal: int,
) -> dict:
    """Persist one MCP ImageContent original and return a Turn image ref."""
    from lib.knowledge import add_document, patch_media_metadata
    from lib.mcp.result_content import MAX_MCP_IMAGE_BYTES

    owner_user_id = require_user_id(user_id, context='MCP image ingest owner')
    if not raw:
        raise ValueError('MCP image is empty')
    if len(raw) > MAX_MCP_IMAGE_BYTES:
        raise ValueError('MCP image exceeds the per-image budget')
    normalized_mime = str(mime_type or '').lower().strip()
    if normalized_mime == 'image/jpg':
        normalized_mime = 'image/jpeg'
    extension_by_mime = {
        'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif',
        'image/webp': '.webp', 'image/bmp': '.bmp',
    }
    extension = extension_by_mime.get(normalized_mime)
    if extension is None:
        raise ValueError(f'Unsupported MCP image MIME type: {normalized_mime}')

    content_digest = hashlib.sha256(raw).hexdigest()
    identity = hashlib.sha256(
        f'{source_tool}:{tool_call_id}:{ordinal}:{content_digest}'.encode(
            'utf-8', errors='replace'
        )
    ).hexdigest()[:20]
    filename = f'mcp-image-{identity}{extension}'
    command_id = f'mcp.image.persist:{owner_user_id}:{identity}'
    document = add_document(
        raw,
        filename,
        user_id=owner_user_id,
        scope='attachment',
        command_id=command_id,
    )
    actual_mime = {
        '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    }.get(str(document.get('kind') or '').lower())
    if actual_mime is None:
        raise ValueError('MCP image bytes did not decode as a supported image')
    attachment_id = str(document.get('id') or '')
    updated = patch_media_metadata(
        attachment_id,
        {
            'media_kind': 'document',
            'status': 'ready',
            'mime_type': actual_mime,
            'origin': 'mcp_tool_result',
            'source_tool': str(source_tool or '')[:240],
            'tool_call_id': str(tool_call_id or '')[:240],
        },
        user_id=owner_user_id,
        command_id=f'mcp.image.metadata:{owner_user_id}:{identity}',
    )
    if updated is not None:
        document = updated
    name = str(document.get('name') or filename)[:240]
    return {
        'attachmentId': attachment_id,
        'preview': f'/api/v1/media/attachments/{attachment_id}/source',
        'caption': f'Image from {str(source_tool or "MCP tool")[:160]}',
        'sizeKB': round(len(raw) / 1024, 1),
        'name': name,
        'mimeType': actual_mime,
        'sourceTool': str(source_tool or '')[:240],
        'toolCallId': str(tool_call_id or '')[:240],
    }


def create_video(
    source_path: str, filename: str, *, user_id: int,
    size_bytes: int = 0, command_id: str | None = None,
) -> tuple[dict, bool]:
    from lib.knowledge import create_media_source

    mime_type = mimetypes.guess_type(filename or '')[0] or 'video/mp4'
    document = create_media_source(
        source_path, filename, user_id=user_id,
        command_id=command_id,
        scope='draft',
        media_metadata={
            'media_kind': 'video',
            'status': 'processing',
            'phase': 'probe',
            'mime_type': mime_type,
            'received_size_bytes': max(0, int(size_bytes)),
        })
    if document.get('duplicate') and (
            document.get('media_metadata') or {}).get('status') != 'ready':
        from lib.knowledge import patch_media_metadata
        restarted = patch_media_metadata(
            str(document.get('id') or ''), {
                'status': 'processing', 'phase': 'probe', 'error': '',
            }, user_id=user_id,
            command_id=(f'{command_id}:restart'[:200] if command_id else None))
        if restarted is not None:
            document = {**restarted, 'duplicate': True}
    return attachment_ref(document), bool(document.get('duplicate'))


def complete_video(
    attachment_id: str, *, frames: list[dict], transcript: str,
    transcript_status: str, transcript_model: str, storyboard: str,
    storyboard_status: str, storyboard_model: str, probe: dict,
    user_id: int, command_id: str | None = None,
) -> dict | None:
    """Commit video frames, searchable narration and final status atomically."""
    from lib.knowledge import replace_media_evidence
    from lib.knowledge.chunking import chunk_document

    chunks: list[dict] = []

    def append_text(text: str, section: str) -> None:
        if not str(text or '').strip():
            return
        for part in chunk_document(str(text)):
            chunks.append({
                'section': section,
                'location': 'Video',
                'content': str(part.get('content') or ''),
            })

    append_text(storyboard, 'Visual storyboard')
    append_text(transcript, 'Audio transcript')
    if chunks and frames:
        # Search hits on the storyboard can recover the corresponding visual
        # evidence without duplicating frame bytes in the text index.
        chunks[0]['asset_ordinals'] = list(range(len(frames)))

    evidence = [{
        'path': str(frame.get('path') or ''),
        'kind': 'video_frame',
        'mime_type': 'image/jpeg',
        'suffix': '.jpg',
        'caption': f"Video frame at {float(frame.get('t') or 0):.2f}s",
        'metadata': {'timestamp_s': float(frame.get('t') or 0)},
    } for frame in frames]
    avg_frame_bytes = int(sum(
        Path(str(frame.get('path') or '')).stat().st_size for frame in frames
    ) / max(1, len(frames))) if frames else 0
    metadata = {
        'media_kind': 'video',
        'status': 'ready',
        'phase': 'done',
        'mime_type': mimetypes.guess_type(
            str(probe.get('filename') or 'video.mp4'))[0] or 'video/mp4',
        'duration_s': round(float(probe.get('duration') or 0), 2),
        'width': int(probe.get('width') or 0),
        'height': int(probe.get('height') or 0),
        'fps': float(probe.get('fps') or 0),
        'frame_count': len(frames),
        'avg_frame_bytes': avg_frame_bytes,
        'transcript_status': str(transcript_status or 'none'),
        'transcript_model': str(transcript_model or ''),
        'storyboard_status': str(storyboard_status or 'none'),
        'storyboard_model': str(storyboard_model or ''),
    }
    updated = replace_media_evidence(
        str(attachment_id or ''), chunks=chunks, assets=evidence,
        media_metadata=metadata, user_id=user_id, command_id=command_id)
    return attachment_ref(updated) if updated is not None else None


def mark_failed(
    attachment_id: str, error: str, *, user_id: int,
    command_id: str | None = None,
) -> dict | None:
    from lib.knowledge import patch_media_metadata

    document = patch_media_metadata(
        str(attachment_id or ''), {
            'status': 'failed', 'phase': 'failed',
            'error': str(error or 'processing failed')[:2000],
        }, user_id=user_id, command_id=command_id)
    return attachment_ref(document) if document is not None else None


def set_phase(
    attachment_id: str, phase: str, *, user_id: int,
) -> dict | None:
    from lib.knowledge import patch_media_metadata

    document = patch_media_metadata(
        str(attachment_id or ''), {'phase': str(phase or '')[:32]},
        user_id=user_id)
    return attachment_ref(document) if document is not None else None


def delete_attachment(attachment_id: str, *, user_id: int) -> bool:
    from lib.knowledge import (
        delete_document, get_document_metadata, set_document_scope,
    )

    document = get_document_metadata(str(attachment_id or ''), user_id=user_id)
    if document is None or document.get('scope') == 'library':
        return False
    if document.get('scope') == 'shared':
        return set_document_scope(
            str(attachment_id), 'library', user_id=user_id) is not None
    return delete_document(str(attachment_id), user_id=user_id)


def discard_draft(attachment_id: str, *, user_id: int) -> bool:
    """Delete only an unsent upload; retained/library content fails closed."""
    from lib.knowledge import delete_document, get_document_metadata

    document = get_document_metadata(str(attachment_id or ''), user_id=user_id)
    if document is None or document.get('scope') != 'draft':
        return False
    return delete_document(str(attachment_id), user_id=user_id)


def _retain_attachment(attachment_id: str, *, user_id: int) -> dict | None:
    from lib.knowledge import get_document_metadata, set_document_scope

    document = get_document_metadata(attachment_id, user_id=user_id)
    if document is None:
        return None
    scope = str(document.get('scope') or 'library')
    desired_scope = {
        'draft': 'attachment',
        'library': 'shared',
    }.get(scope)
    if desired_scope is not None:
        document = set_document_scope(
            attachment_id, desired_scope, user_id=user_id) or document
    return attachment_ref(document)


def resolve_client_refs(
    items: Any, *, user_id: int, retain: bool = False,
) -> list[dict]:
    """Replace untrusted client metadata with canonical owner-scoped refs."""
    if not isinstance(items, list):
        return []
    resolved: list[dict] = []
    seen: set[str] = set()
    for candidate in items[:_MAX_ATTACHMENTS_PER_TURN]:
        if not isinstance(candidate, dict):
            continue
        attachment_id = str(candidate.get('attachmentId') or '').strip()
        if not attachment_id or len(attachment_id) > 128 or attachment_id in seen:
            continue
        canonical = (_retain_attachment(attachment_id, user_id=user_id)
                     if retain else
                     get_attachment(attachment_id, user_id=user_id))
        if canonical is None:
            logger.warning('[Media] ignored missing attachment %s', attachment_id)
            continue
        seen.add(attachment_id)
        resolved.append(canonical)
    return resolved


def _bounded_chunks(
    attachment_id: str, *, query: str, user_id: int, char_cap: int,
) -> tuple[list[dict], list[str], str, int, int]:
    """Select chunks under *char_cap*; report the projection mode.

    Returns ``(selected, asset_ids, mode, injected_chars, total_text_chars)``
    where *mode* is:
      - ``'full'``   — the whole extracted text fits in the budget;
      - ``'search'`` — a subset, chosen by relevance search against *query*;
      - ``'head'``   — a subset, the document head, because relevance search
        matched nothing (or there was no query).
    """
    from lib.knowledge import (
        get_document_content,
        get_document_metadata,
        search_document_candidates,
    )

    metadata = get_document_metadata(attachment_id, user_id=user_id)
    if metadata is None:
        return [], [], 'full', 0, 0
    total_text_chars = int(metadata.get('text_chars') or 0)
    rows: list[dict] = []
    from_search = False
    if total_text_chars > char_cap and query.strip():
        rows = search_document_candidates(
            attachment_id, query, user_id=user_id, limit=12)
        from_search = bool(rows)
    if not rows:
        page = get_document_content(
            attachment_id, user_id=user_id, offset=0, limit=80)
        rows = list((page or {}).get('chunks') or [])
    selected: list[dict] = []
    asset_ids: list[str] = []
    consumed = 0
    for row in rows:
        content = str(row.get('content') or '')
        remaining = char_cap - consumed
        if remaining <= 0:
            break
        if len(content) > remaining:
            marker = '\n[attachment excerpt truncated]'
            if remaining <= len(marker):
                content = marker[:remaining]
            else:
                content = content[:remaining - len(marker)] + marker
        selected.append({**row, 'content': content})
        consumed += len(content)
        for asset in row.get('assets') or []:
            asset_id = str(asset.get('id') or '')
            if asset_id and asset_id not in asset_ids:
                asset_ids.append(asset_id)
    if consumed >= total_text_chars:
        mode = 'full'
    elif from_search:
        mode = 'search'
    else:
        mode = 'head'
    return selected, asset_ids, mode, consumed, total_text_chars


def _image_block(asset_id: str, *, user_id: int) -> dict | None:
    from lib.knowledge import read_asset

    loaded = read_asset(asset_id, user_id=user_id)
    if loaded is None:
        return None
    row, raw = loaded
    mime_type = str(row.get('mime_type') or '')
    if not mime_type.startswith('image/'):
        return None
    encoded = base64.b64encode(raw).decode('ascii')
    return {
        'type': 'image_url',
        'image_url': {'url': f'data:{mime_type};base64,{encoded}'},
    }


def project_for_model(
    items: Any, *, user_id: int, query: str, model: str,
    image_budget: int | None = None,
    projection_budget: MediaProjectionBudget | None = None,
) -> dict:
    """Resolve attachment refs into bounded text/image blocks for one request."""
    owner_user_id = require_user_id(user_id, context='media model projection')
    blocks: list[dict] = []
    used_images = 0
    used_text_chars = 0
    canonical = resolve_client_refs(items, user_id=owner_user_id)
    for index, ref in enumerate(canonical, 1):
        attachment_id = ref['attachmentId']
        stable_ref = f'att_media_{attachment_id}'
        if ref.get('status') != 'ready':
            if projection_budget is not None:
                projection_budget.skip_attachment()
            detail = ref.get('error') or 'processing has not completed'
            blocks.append({'type': 'text', 'text': (
                f'[Attachment {index}: {ref["name"]} — {stable_ref}] '\
                f'is {ref.get("status")}: {detail}.')})
            continue

        is_video = ref.get('kind') == 'video'
        per_attachment_cap = (
            _VIDEO_TEXT_CAP if is_video else document_text_budget(model))
        char_cap = (per_attachment_cap if projection_budget is None else
                    projection_budget.available(per_attachment_cap))
        if char_cap > 0:
            (chunks, linked_asset_ids, projection_mode, injected_chars,
             store_text_chars) = _bounded_chunks(
                attachment_id, query=query, user_id=owner_user_id,
                char_cap=char_cap)
        else:
            (chunks, linked_asset_ids, projection_mode, injected_chars,
             store_text_chars) = ([], [], 'full', 0, 0)
        body_parts = []
        for chunk in chunks:
            section = str(chunk.get('section') or '')
            content = str(chunk.get('content') or '')
            if section:
                body_parts.append(f'[{section}]\n{content}')
            elif content:
                body_parts.append(content)
            used_text_chars += len(content)
            if projection_budget is not None:
                projection_budget.consume(len(content))

        from lib.knowledge import list_document_assets
        all_assets = list_document_assets(
            attachment_id, user_id=owner_user_id, limit=200) or []
        asset_by_id = {str(asset.get('id') or ''): asset for asset in all_assets}
        candidate_assets = [asset_by_id[asset_id] for asset_id in linked_asset_ids
                            if asset_id in asset_by_id]
        if not candidate_assets:
            candidate_assets = all_assets

        remaining = None if image_budget is None else max(
            0, int(image_budget) - used_images)
        if is_video:
            from lib.model_info import video_frame_budget
            from lib.video_analysis._frames import _thin_frames

            frame_cap = video_frame_budget(
                model, avg_frame_bytes=int(
                    ref.get('avgFrameBytes') or 0))
            if remaining is not None:
                frame_cap = min(frame_cap, remaining)
            chosen_assets = _thin_frames(candidate_assets, frame_cap)
        else:
            document_cap = _DOCUMENT_IMAGE_CAP
            if remaining is not None:
                document_cap = min(document_cap, remaining)
            chosen_assets = candidate_assets[:document_cap]

        # Exact-name registry: the tool-display layer renders the read_files
        # round title as 'uploaded attachment — "<name>"' without an owner id.
        from lib.attachments import register_attachment_name
        register_attachment_name(stable_ref, ref['name'])

        total_text_chars = int(ref.get('textChars') or 0) or store_text_chars
        pages = int(ref.get('pages') or 0)
        facts = [f'{ref["name"]} ({ref["kind"]}']
        if pages:
            facts[-1] += f', {pages} pages'
        if total_text_chars:
            facts[-1] += f', {total_text_chars:,} chars'
        facts[-1] += ')'
        facts.append(f'attachment ref: {stable_ref}')
        header = f'[Attachment {index}: {"; ".join(facts)}]'
        if projection_mode == 'search':
            header += (
                f'\n[Showing {injected_chars:,} of {total_text_chars:,} chars: '
                f'excerpts selected by relevance to your message. To read more '
                f'of this file, call read_files with path="{stable_ref}" '
                f'(optionally start_line/end_line to page).]')
        elif projection_mode == 'head':
            header += (
                f'\n[Relevance search matched nothing in this file for your '
                f'message, so only the first {injected_chars:,} of '
                f'{total_text_chars:,} chars (from the head of the document) '
                f'are shown. To read more, call read_files with '
                f'path="{stable_ref}" (optionally start_line/end_line to '
                f'page).]')
        if body_parts:
            blocks.append({'type': 'text', 'text': (
                header + '\n' + '\n\n'.join(body_parts))})
        elif char_cap <= 0:
            blocks.append({'type': 'text', 'text': (
                header + '\n[attachment text omitted: request budget exhausted]')})
        else:
            blocks.append({'type': 'text', 'text': header})
        for asset in chosen_assets:
            block = _image_block(str(asset.get('id') or ''), user_id=owner_user_id)
            if block is None:
                continue
            blocks.append(block)
            metadata = asset.get('metadata') or {}
            if is_video:
                blocks.append({'type': 'text', 'text': (
                    '[video frame at '
                    f'{float(metadata.get("timestamp_s") or 0):.2f}s]')})
            elif int(asset.get('page') or 0):
                blocks.append({'type': 'text', 'text': (
                    f'[document page {int(asset.get("page") or 0)}]')})
            used_images += 1
    return {
        'blocks': blocks,
        'image_count': used_images,
        'text_chars': used_text_chars,
    }


__all__ = [
    'MODEL_TEXT_REQUEST_CAP', 'MediaProjectionBudget', 'attachment_ref',
    'complete_video', 'create_video', 'delete_attachment', 'discard_draft',
    'document_text_budget', 'document_text_request_cap', 'get_attachment',
    'ingest_document', 'mark_failed', 'project_for_model',
    'resolve_client_refs', 'set_phase',
]
