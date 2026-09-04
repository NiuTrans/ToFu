"""Unified authenticated chat-attachment upload and lifecycle API."""

from __future__ import annotations

import mimetypes

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload_too_large,
    api_service_unavailable,
)
from lib.file_serving import send_file_conditional
from lib.log import get_logger
from lib.openapi import api_meta
from lib.quart_sync import request_files

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
        from lib.pdf_parser.admission import PdfParseCapacityExceeded

        if isinstance(exc, PdfParseCapacityExceeded):
            return api_service_unavailable(
                str(exc),
                retry_after=1,
                kind='server_busy',
                retryable=True,
            )
        if isinstance(exc, KnowledgeIngestError):
            return api_bad_request(str(exc), field='file')
        logger.error('[Media.v1] document ingest failed: %s', exc, exc_info=True)
        return api_internal_error('Attachment indexing failed')
    return api_ok({'attachment': attachment})


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
@api_meta(summary='Stream an attachment original', tags=['media'])
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
