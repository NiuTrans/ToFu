"""Unified authenticated chat-attachment upload and lifecycle API."""

from __future__ import annotations

import mimetypes
import os

from quart import Blueprint, request

from lib.api_v4 import raw_success_response
from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload,
    api_payload_too_large,
)
from lib.file_serving import send_file_conditional
from lib.log import get_logger
from lib.openapi import media_responses, api_meta
from lib.quart_sync import request_files
from lib.rate_limiter import rate_limit

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_media_bp = Blueprint('api_v1_media', __name__)

_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


@api_v1_media_bp.route('/api/v1/media/attachments', methods=['POST'])
@require_auth
@api_meta(
    summary='Upload and index one chat document attachment', tags=['media'])
def upload_media_attachment_v1():
    files = request_files()
    upload = files.get('file')
    if upload is None or not getattr(upload, 'filename', ''):
        return api_bad_request('No file provided', field='file')
    if request.content_length and request.content_length > _MAX_DOCUMENT_BYTES:
        return api_payload_too_large(_MAX_DOCUMENT_BYTES)
    raw = upload.stream.read(_MAX_DOCUMENT_BYTES + 1)
    if len(raw) > _MAX_DOCUMENT_BYTES:
        return api_payload_too_large(_MAX_DOCUMENT_BYTES)
    try:
        from lib.media_attachments import ingest_document

        attachment = ingest_document(
            raw, str(upload.filename), user_id=int(request_user_id()))
    except Exception as exc:
        from lib.knowledge.ingest import KnowledgeIngestError

        if isinstance(exc, KnowledgeIngestError):
            return api_bad_request(str(exc), field='file')
        logger.error('[Media.v1] document ingest failed: %s', exc, exc_info=True)
        return api_internal_error('Attachment indexing failed')
    return api_ok({'attachment': attachment})


_ARCHIVE_ERROR_KIND = {
    'archive_limit': 'archive-too-large',
    'archive_invalid': 'archive-invalid',
    'archive_empty': 'archive-empty',
    'archive_path': 'archive-invalid',
}


@api_v1_media_bp.route('/api/v1/media/attachments/expand-archive', methods=['POST'])
@require_auth
@rate_limit(limit=10, per=60)
@api_meta(
    summary='Extract a dropped chat archive into a managed workspace',
    description=(
        "multipart/form-data: ``file`` (a .zip/.tar/.tar.gz archive). Spooled "
        "to a bounded temp file and extracted atomically into "
        "``uploads/workspaces/<stem>/`` (fresh auto-suffixed directory; "
        "existing files never overwritten) via lib.paper.archive — zip-slip/link "
        "defense, 512 files / 64 MiB total / 32 MiB per file / 128 MiB "
        "compressed. Unlike the project-browser archive drop, this needs no "
        "already-attached root, so a ChatGPT-style 'drop a zip on the input "
        "box' works with no project set. Returns the extraction receipt, the "
        "extracted path (for the client to mount as a project root via "
        "/api/v1/project/paths), and TeX main-document candidates so a paper "
        "preview can be offered immediately."),
    tags=['media'])
def expand_archive_attachment_v1():
    from lib.chat_workspace import chat_workspaces_root
    from lib.paper.archive import (
        MAX_ARCHIVE_BYTES,
        extract_archive,
        stream_to_temp,
    )
    from lib.paper.builds.errors import BuildError

    files = request_files()
    upload = files.get('file')
    if upload is None or not getattr(upload, 'filename', ''):
        return api_bad_request('No file provided', field='file')
    archive_name = os.path.basename(
        (upload.filename or 'archive').replace('\\', '/'))

    spooled = None
    try:
        spooled = stream_to_temp(upload.stream, max_bytes=MAX_ARCHIVE_BYTES)
        receipt = extract_archive(
            spooled, chat_workspaces_root(), name_hint=archive_name)
    except BuildError as exc:
        kind = _ARCHIVE_ERROR_KIND.get(exc.code, 'archive-invalid')
        return api_payload(
            {'ok': False, 'errorKind': kind, 'error': str(exc)}, 400)
    except OSError as exc:
        logger.warning('[Media.v1] archive extraction failed: %s', exc,
                       exc_info=True)
        return api_internal_error('Cannot store the extracted archive')
    finally:
        if spooled is not None:
            spooled.unlink(missing_ok=True)

    tex_candidates = []
    try:
        for candidate in sorted(receipt.directory.rglob('*.tex'))[:64]:
            try:
                head = candidate.read_text(errors='replace')[:8192]
            except OSError:
                continue
            if '\\documentclass' in head:
                tex_candidates.append(
                    candidate.relative_to(receipt.directory).as_posix())
    except OSError:
        pass
    logger.info(
        '[Media.v1] chat archive extracted %s (%d files, %d bytes, %d tex)',
        receipt.directory, receipt.files, receipt.bytes_written,
        len(tex_candidates))
    result = receipt.as_dict(receipt.directory.parent)
    result.update(
        ok=True,
        action='expand_archive',
        texMainCandidates=tex_candidates[:16],
    )
    return api_ok(result)


@api_v1_media_bp.route(
    '/api/v1/media/attachments/<attachment_id>', methods=['GET'])
@require_auth
@api_meta(summary='Read canonical chat attachment metadata', tags=['media'])
def media_attachment_v1(attachment_id: str):
    from lib.media_attachments import get_attachment

    attachment = get_attachment(
        attachment_id, user_id=int(request_user_id()))
    if attachment is None:
        return api_not_found('Attachment not found')
    return api_ok({'attachment': attachment})


@api_v1_media_bp.route(
    '/api/v1/media/attachments/<attachment_id>/source', methods=['GET'])
@require_auth
@api_meta(
    responses=media_responses('*/*', conditional=True),
    summary='Stream an attachment original', tags=['media'])
@raw_success_response
def media_attachment_source_v1(attachment_id: str):
    from lib.knowledge import get_document_metadata, read_source_path

    owner_user_id = int(request_user_id())
    document = get_document_metadata(attachment_id, user_id=owner_user_id)
    path = read_source_path(attachment_id, user_id=owner_user_id)
    if document is None or path is None:
        return api_not_found('Attachment source not found')
    mime_type = str(
        (document.get('media_metadata') or {}).get('mime_type') or
        mimetypes.guess_type(str(document.get('name') or ''))[0] or
        'application/octet-stream')
    response = send_file_conditional(
        path, mimetype=mime_type,
        download_name=str(document.get('name') or 'attachment'),
        as_attachment=request.args.get('download', '').lower()
        in {'1', 'true', 'yes'},
    )
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@api_v1_media_bp.route(
    '/api/v1/media/attachments/<attachment_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete an attachment original and derived evidence', tags=['media'])
def delete_media_attachment_v1(attachment_id: str):
    from lib.media_attachments import delete_attachment, discard_draft

    owner_user_id = int(request_user_id())
    draft_only = request.args.get('draft', '').lower() in {'1', 'true', 'yes'}
    if draft_only:
        if discard_draft(attachment_id, user_id=owner_user_id):
            return api_ok({'deleted': True, 'attachmentId': attachment_id})
        from lib.media_attachments import get_attachment
        if get_attachment(attachment_id, user_id=owner_user_id) is None:
            return api_not_found('Attachment not found')
        return api_conflict('Attachment is already retained')
    if not delete_attachment(attachment_id, user_id=owner_user_id):
        return api_not_found('Attachment not found')
    return api_ok({'deleted': True, 'attachmentId': attachment_id})


__all__ = ['api_v1_media_bp']
