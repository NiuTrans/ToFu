"""Local knowledge-base management API."""

from __future__ import annotations

import io

from quart import Blueprint, request

from lib.quart_sync import request_files, send_file

from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_knowledge_bp = Blueprint('api_v1_knowledge', __name__)

_MAX_FILE_BYTES = 50 * 1024 * 1024
_MAX_BATCH_BYTES = 200 * 1024 * 1024
_MAX_BATCH_FILES = 20
_DOCUMENT_CATEGORIES = {
    'all', 'pdf', 'document', 'spreadsheet', 'presentation', 'image',
    'email', 'ebook', 'text', 'other',
}
_DOCUMENT_SORTS = {'updated_desc', 'created_desc', 'name_asc', 'size_desc'}


def _status_payload(
    *, user_id: int, page: int = 1, page_size: int = 30, query: str = '',
    category: str = 'all', sort: str = 'updated_desc',
) -> dict:
    from lib.knowledge import get_status
    from lib.knowledge.ingest import SUPPORTED_EXTENSIONS
    status = get_status(
        user_id=user_id,
        page=page, page_size=page_size, query=query,
        category=category, sort=sort)
    return {
        **status,
        'limits': {
            'max_file_bytes': _MAX_FILE_BYTES,
            'max_batch_bytes': _MAX_BATCH_BYTES,
            'max_batch_files': _MAX_BATCH_FILES,
        },
        'supported_extensions': list(SUPPORTED_EXTENSIONS),
        'privacy': (
            'local_with_opt_in_visual_provider'
            if status.get('visual_enrichment') else 'local_only'),
        'visual_enrichment_sends_images_to_configured_provider': True,
    }


@api_v1_knowledge_bp.route('/api/v1/knowledge', methods=['GET'])
@require_auth
@api_meta(summary='Local knowledge-base status', tags=['knowledge'])
def knowledge_status_v1():
    owner_user_id = int(request_user_id())
    query = str(request.args.get('query') or '').strip()
    category = str(request.args.get('category') or 'all').lower()
    sort = str(request.args.get('sort') or 'updated_desc').lower()
    if len(query) > 200:
        return api_bad_request(
            'query is too long (max 200 characters)', field='query')
    if category not in _DOCUMENT_CATEGORIES:
        return api_bad_request('unsupported document category', field='category')
    if sort not in _DOCUMENT_SORTS:
        return api_bad_request('unsupported document sort', field='sort')
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('page_size', 30))
    except (TypeError, ValueError):
        return api_bad_request(
            'page and page_size must be integers', field='page')
    if page < 1:
        return api_bad_request('page must be at least 1', field='page')
    if not 1 <= page_size <= 100:
        return api_bad_request(
            'page_size must be from 1 to 100', field='page_size')
    return api_ok(_status_payload(
        user_id=owner_user_id,
        page=page, page_size=page_size, query=query,
        category=category, sort=sort))


@api_v1_knowledge_bp.route('/api/v1/knowledge/activity', methods=['GET'])
@require_auth
@api_meta(summary='Local knowledge background activity', tags=['knowledge'])
def knowledge_activity_v1():
    from lib.knowledge import get_activity
    return api_ok(get_activity(user_id=int(request_user_id())))


@api_v1_knowledge_bp.route('/api/v1/knowledge/settings', methods=['POST'])
@require_auth
@api_meta(summary='Enable or disable local knowledge retrieval', tags=['knowledge'])
def knowledge_settings_v1():
    owner_user_id = int(request_user_id())
    body = parse_body()
    has_enabled = 'enabled' in body
    has_visual = 'visual_enrichment' in body
    if not has_enabled and not has_visual:
        return api_bad_request(
            'enabled or visual_enrichment is required')
    if has_enabled and not isinstance(body.get('enabled'), bool):
        return api_bad_request('enabled must be a boolean', field='enabled')
    if has_visual and not isinstance(body.get('visual_enrichment'), bool):
        return api_bad_request(
            'visual_enrichment must be a boolean',
            field='visual_enrichment')
    from lib.knowledge import set_enabled, set_visual_enrichment
    if has_enabled:
        set_enabled(body['enabled'], user_id=owner_user_id)
    if has_visual:
        set_visual_enrichment(
            body['visual_enrichment'], user_id=owner_user_id)
    return api_ok(_status_payload(user_id=owner_user_id))


@api_v1_knowledge_bp.route('/api/v1/knowledge/search', methods=['POST'])
@require_auth
@api_meta(summary='Preview the local knowledge index', tags=['knowledge'])
def knowledge_search_v1():
    owner_user_id = int(request_user_id())
    body = parse_body()
    query = str(body.get('query') or '').strip()
    if not query:
        return api_bad_request('query is required', field='query')
    if len(query) > 1000:
        return api_bad_request('query is too long (max 1000 characters)', field='query')
    raw_limit = body.get('limit', 6)
    if isinstance(raw_limit, bool):
        return api_bad_request('limit must be an integer from 1 to 10', field='limit')
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return api_bad_request('limit must be an integer from 1 to 10', field='limit')
    if not 1 <= limit <= 10:
        return api_bad_request('limit must be an integer from 1 to 10', field='limit')
    from lib.knowledge.search import search
    results = search(
        query, limit=limit, require_enabled=False, user_id=owner_user_id)
    return api_ok({'query': query, 'count': len(results), 'results': results})


@api_v1_knowledge_bp.route(
    '/api/v1/knowledge/assets/<asset_id>', methods=['GET'])
@require_auth
@api_meta(summary='Read an authenticated knowledge image asset', tags=['knowledge'])
def knowledge_asset_v1(asset_id: str):
    from lib.knowledge import read_asset
    loaded = read_asset(asset_id, user_id=int(request_user_id()))
    if loaded is None:
        return api_not_found('Knowledge asset not found')
    row, raw = loaded
    mime = str(row.get('mime_type') or 'application/octet-stream')
    if request.args.get('thumbnail', '').lower() in ('1', 'true', 'yes'):
        try:
            from PIL import Image
            source = io.BytesIO(raw)
            with Image.open(source) as image:
                image.seek(0)
                image.thumbnail((480, 480))
                output = io.BytesIO()
                if image.mode in ('RGBA', 'LA'):
                    image.save(output, format='PNG', optimize=True)
                    mime = 'image/png'
                else:
                    image.convert('RGB').save(
                        output, format='JPEG', quality=82, optimize=True)
                    mime = 'image/jpeg'
                raw = output.getvalue()
        except Exception as exc:
            logger.debug('[Knowledge.v1] thumbnail fallback to original: %s', exc)
    response = send_file(
        io.BytesIO(raw), mimetype=mime,
        download_name=f'{asset_id}.{mime.split("/")[-1]}',
        as_attachment=False)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def _uploaded_files() -> list:
    uploaded = request_files()
    files = []
    try:
        files.extend(uploaded.getlist('files'))
        files.extend(uploaded.getlist('file'))
    except AttributeError:
        for key in ('files', 'file'):
            item = uploaded.get(key)
            if item is not None:
                files.append(item)
    out = []
    seen = set()
    for item in files:
        marker = id(item)
        if marker not in seen and item and getattr(item, 'filename', ''):
            seen.add(marker)
            out.append(item)
    return out


@api_v1_knowledge_bp.route('/api/v1/knowledge/documents', methods=['POST'])
@require_auth
@api_meta(
    summary='Upload and index local knowledge documents',
    description='Multipart fields ``files`` or ``file``; up to 20 documents.',
    tags=['knowledge'],
)
def knowledge_upload_v1():
    owner_user_id = int(request_user_id())
    uploads = _uploaded_files()
    if not uploads:
        return api_bad_request('No files provided', field='files')
    if len(uploads) > _MAX_BATCH_FILES:
        return api_bad_request(f'Too many files (max {_MAX_BATCH_FILES})', field='files')
    if request.content_length and request.content_length > _MAX_BATCH_BYTES:
        return api_bad_request(
            f'Upload batch too large (max {_MAX_BATCH_BYTES // 1048576} MB)',
            field='files')

    from lib.knowledge import add_document
    from lib.knowledge.ingest import KnowledgeIngestError

    indexed = []
    errors = []
    total = 0
    for upload in uploads:
        name = str(upload.filename or 'document')
        raw = upload.stream.read(_MAX_FILE_BYTES + 1)
        total += len(raw)
        if len(raw) > _MAX_FILE_BYTES:
            errors.append({'name': name, 'error': 'File too large (max 50 MB)'})
            continue
        if total > _MAX_BATCH_BYTES:
            errors.append({'name': name, 'error': 'Upload batch exceeded 200 MB'})
            continue
        try:
            indexed.append(add_document(
                raw, name, user_id=owner_user_id))
        except KnowledgeIngestError as exc:
            errors.append({'name': name, 'error': str(exc)})
        except Exception as exc:
            logger.error('[Knowledge.v1] failed to index %s: %s',
                         name, exc, exc_info=True)
            errors.append({'name': name, 'error': 'Unexpected indexing failure'})

    return api_ok({
        'indexed': indexed,
        'errors': errors,
        **_status_payload(user_id=owner_user_id),
    })


@api_v1_knowledge_bp.route(
    '/api/v1/knowledge/documents/<document_id>/content', methods=['GET'])
@require_auth
@api_meta(summary='Inspect parsed local knowledge content', tags=['knowledge'])
def knowledge_content_v1(document_id: str):
    owner_user_id = int(request_user_id())
    from lib.knowledge import get_document_content
    try:
        offset = int(request.args.get('offset', 0))
        limit = int(request.args.get('limit', 80))
    except (TypeError, ValueError):
        return api_bad_request(
            'offset and limit must be integers', field='offset')
    if offset < 0:
        return api_bad_request('offset must be at least 0', field='offset')
    if not 1 <= limit <= 200:
        return api_bad_request('limit must be from 1 to 200', field='limit')
    content = get_document_content(
        document_id, user_id=owner_user_id, offset=offset, limit=limit)
    if content is None:
        return api_not_found('Knowledge document not found')
    return api_ok(content)


@api_v1_knowledge_bp.route(
    '/api/v1/knowledge/documents/<document_id>/reindex', methods=['POST'])
@require_auth
@api_meta(summary='Re-parse and reindex one local document', tags=['knowledge'])
def knowledge_reindex_v1(document_id: str):
    owner_user_id = int(request_user_id())
    from lib.knowledge import reindex_document
    from lib.knowledge.ingest import KnowledgeIngestError
    try:
        document = reindex_document(document_id, user_id=owner_user_id)
    except KnowledgeIngestError as exc:
        return api_bad_request(str(exc))
    if document is None:
        return api_not_found('Knowledge document not found')
    return api_ok({
        'reindexed': document,
        **_status_payload(user_id=owner_user_id),
    })


@api_v1_knowledge_bp.route(
    '/api/v1/knowledge/documents/<document_id>', methods=['DELETE'])
@require_auth
@api_meta(summary='Delete one local knowledge document', tags=['knowledge'])
def knowledge_delete_v1(document_id: str):
    from lib.knowledge import delete_document
    owner_user_id = int(request_user_id())
    if not delete_document(document_id, user_id=owner_user_id):
        return api_not_found('Knowledge document not found')
    return api_ok(_status_payload(user_id=owner_user_id))


__all__ = ['api_v1_knowledge_bp']
