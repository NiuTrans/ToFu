"""Authenticated binary delivery; never resolves caller-controlled filesystem paths."""
from urllib.parse import quote

import asyncio
from quart import Blueprint, Response, request
from lib.api_response import api_not_found

from lib.api_v4 import raw_success_response
from lib.artifacts.core import ArtifactNotFoundError
from lib.openapi import api_meta, media_responses
from lib.response_assets import load_asset
from routes.api_v1.auth import request_user_id, require_auth

from lib.log import get_logger

logger = get_logger(__name__)

response_assets_bp = Blueprint('response_assets', __name__)


@response_assets_bp.route('/api/response-assets/<asset_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get an owned response image', tags=['artifacts'],
          responses=media_responses('image/png', 'image/jpeg', 'image/webp', 'image/gif'))
@raw_success_response
async def response_asset(asset_id):
    try:
        owner_user_id = int(request_user_id())
        data, info = await asyncio.to_thread(load_asset, asset_id, user_id=owner_user_id)
    except ArtifactNotFoundError:
        return api_not_found('not_found')
    response = Response(data, content_type=info['mime'])
    response.headers.update({'X-Content-Type-Options': 'nosniff',
                             'Cache-Control': 'private, no-store',
                             'Referrer-Policy': 'no-referrer'})
    if request.args.get('download') is not None:
        ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}[info['mime']]
        supplied = request.args.get('download') or ''
        name = ''.join(c for c in supplied if c.isprintable() and c not in '/\\";')[:120]
        if not name or name in ('1', 'true'):
            name = 'response-image.' + ext
        response.headers['Content-Disposition'] = (
            'attachment; filename="response-image.' + ext + '"; filename*=UTF-8\'\'' + quote(name, safe=''))
    return response


@response_assets_bp.route('/api/response-surfaces/resources', methods=['GET'])
@require_auth
@api_meta(summary='Read live compute resources in an owned response context', tags=['artifacts'],
          responses=media_responses('application/json'))
@raw_success_response
async def response_resources():
    from threading import Event
    import json
    from quart import g
    from lib.response_surfaces import read_resources
    from lib.api_response import api_bad_request, api_error
    allowed = {'provider', 'connection', 'queue', 'runId', 'conversationId', 'turnId'}
    if (set(request.args) - allowed or any(len(request.args.getlist(key)) != 1 for key in request.args)
            or any(len(value) > 256 for value in request.args.values())):
        return api_bad_request('Invalid surface query')
    scope = {key: value for key, value in request.args.items() if key not in ('provider', 'connection', 'queue')}
    cancelled = Event()
    owner = request_user_id()
    tenant = str(getattr(getattr(g, 'auth_ctx', None), 'tenant_id', '') or '')
    # Join the request-owned worker even on disconnect; its provider sees cancel.
    worker = asyncio.create_task(asyncio.to_thread(read_resources, scope, request.args.get('provider'),
        user_id=owner, tenant_id=tenant, cancelled=cancelled.is_set, connection=request.args.get('connection'), queue=request.args.get('queue')))
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        try:
            await asyncio.shield(worker)
        except Exception as diagnostic_error:
            logger.debug('response_resources failure handled: %s', type(diagnostic_error).__name__)
            pass
        raise
    except PermissionError:
        return api_not_found('not_found')
    except (ValueError, KeyError):
        return api_bad_request('Invalid surface context or data')
    except Exception as error:
        from lib.storage.errors import StorageError
        if isinstance(error, StorageError) and error.code == 'not_found':
            return api_not_found('not_found')
        # Provider messages can contain transport details; keep this projection inert.
        return api_error('Live resources are unavailable; retry or check provider configuration', status=503, kind='resource_unavailable')
    response = Response(json.dumps(result, ensure_ascii=False, allow_nan=False), content_type='application/json')
    response.headers.update({'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
    return response


@response_assets_bp.route('/api/response-surfaces/providers', methods=['GET'])
@require_auth
@api_meta(summary='List configured resource providers in an owned response context', tags=['artifacts'],
          responses=media_responses('application/json'))
@raw_success_response
async def response_providers():
    import json
    from lib.response_surfaces import available_providers
    from lib.api_response import api_bad_request, api_error
    if (set(request.args) - {'runId', 'conversationId', 'turnId'}
            or any(len(request.args.getlist(key)) != 1 for key in request.args)
            or any(len(value) > 256 for value in request.args.values())):
        return api_bad_request('Invalid surface query')
    try:
        result = await asyncio.to_thread(available_providers, dict(request.args), user_id=request_user_id())
    except (PermissionError, KeyError):
        return api_not_found('not_found')
    except Exception as error:
        from lib.storage.errors import StorageError
        if isinstance(error, StorageError) and error.code == 'not_found':
            return api_not_found('not_found')
        return api_error('Resource providers are unavailable; retry', status=503, kind='resource_unavailable')
    response = Response(json.dumps(result), content_type='application/json')
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@response_assets_bp.route('/api/response-assets/artifacts/<artifact_id>', methods=['GET'])
@require_auth
@api_meta(summary='Resolve an owned same-conversation rich artifact action', tags=['artifacts'],
          responses=media_responses('application/json', 'text/plain'))
@raw_success_response
async def response_artifact(artifact_id):
    from lib.artifacts.core import get_artifact_meta
    conversation_id = request.args.get('conversationId')
    owner = request_user_id()
    if not conversation_id:
        return api_not_found('not_found')
    if request.args.get('download') is None:
        def resolve():
            row = get_artifact_meta(artifact_id, user_id=owner)
            if row['conv_id'] != conversation_id or row['source'] == 'response_asset':
                raise ArtifactNotFoundError('Unavailable')
            return row
        try:
            row = await asyncio.to_thread(resolve)
        except ArtifactNotFoundError:
            return api_not_found('not_found')
        import json
        response = Response(json.dumps({'artifactId': row['id']}), content_type='application/json')
    else:
        from lib.artifacts.response_files import download_payload
        from routes.artifacts import _content_disposition
        try:
            data, info = await asyncio.to_thread(download_payload, artifact_id, user_id=owner,
                                                 conv_id=conversation_id,
                                                 interactive=request.args.get('interactive') == '1')
        except ArtifactNotFoundError:
            return api_not_found('not_found')
        response = Response(data, content_type=info['mime'])
        response.headers['Content-Disposition'] = _content_disposition('attachment', info['filename'])
        if info['warning']:
            response.headers['Warning'] = '299 tofu "' + info['warning'] + '"'
    response.headers.update({'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                             'Content-Security-Policy': "default-src 'none'; sandbox", 'Referrer-Policy': 'no-referrer'})
    return response
