"""Unified authenticated chat-attachment upload and lifecycle API."""

from __future__ import annotations

import mimetypes

from quart import Blueprint, current_app, request

from lib.api_v4 import raw_success_response
from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_internal_error,
    api_not_found,
    api_ok,
    api_payload,
)
from lib.file_serving import send_file_conditional
from lib.log import get_logger
from lib.openapi import media_responses, api_meta
from lib.quart_sync import owned_sync_command, request_files

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_media_bp = Blueprint('api_v1_media', __name__)


@api_v1_media_bp.route('/api/v1/media/attachments', methods=['POST'])
@require_auth
@api_meta(
    summary='Upload and index one chat document attachment', tags=['media'])
@owned_sync_command
def upload_media_attachment_v1():
    files = request_files()
    upload = files.get('file')
    if upload is None or not getattr(upload, 'filename', ''):
        return api_bad_request('No file provided', field='file')
    from lib.knowledge.html_application import application_html_projector
    from lib.knowledge.ingest import KnowledgeIngestError
    from lib.media_attachments import ingest_document

    # The parser owns its full byte input; HTTP reception already spills
    # into the request-owned file rather than a second receive buffer.
    raw = upload.stream.read()
    owner_user_id = int(request_user_id())
    try:
        attachment = ingest_document(
            raw, str(upload.filename), user_id=owner_user_id,
            html_projector=application_html_projector(current_app))
    except KnowledgeIngestError as exc:
        return api_bad_request(str(exc), field='file')
    return api_ok({'attachment': attachment})


@api_v1_media_bp.route('/api/v1/media/attachments/archive', methods=['POST'])
@require_auth
@api_meta(
    summary='Store a dropped chat archive verbatim in a managed workspace',
    description=(
        "multipart/form-data: ``file`` (a .zip/.tar/.tar.gz/.tgz/.tar.bz2/"
        ".tar.xz archive). The archive is NOT extracted: its bytes are copied "
        "into a fresh ``uploads/workspaces/<stem>/`` directory (auto-suffixed; "
        "existing files never overwritten) and the response names that "
        "directory for the client to mount as a project root via "
        "/api/v1/project/paths. The model unpacks it with its own workspace "
        "tools when a task needs the contents. Unlike the project-browser "
        "archive drop, this needs no already-attached root."),
    tags=['media'])
def store_archive_attachment_v1():
    from lib.chat_workspace import ArchiveRejected, store_archive

    files = request_files()
    upload = files.get('file')
    if upload is None or not getattr(upload, 'filename', ''):
        return api_bad_request('No file provided', field='file')
    try:
        stored = store_archive(upload.stream, str(upload.filename))
    except ArchiveRejected as exc:
        return api_payload(
            {'ok': False, 'errorKind': exc.kind, 'error': str(exc)}, 400)
    except OSError as exc:
        logger.warning('[Media.v1] archive store failed: %s', exc, exc_info=True)
        return api_internal_error('Cannot store the archive')
    logger.info('[Media.v1] chat archive stored %s (%d bytes)',
                stored.archive, stored.bytes_written)
    return api_ok({
        'ok': True,
        'action': 'store_archive',
        'directory': stored.directory.name,
        'path': str(stored.directory),
        'archiveName': stored.archive.name,
        'archivePath': str(stored.archive),
        'bytes': stored.bytes_written,
    })


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
