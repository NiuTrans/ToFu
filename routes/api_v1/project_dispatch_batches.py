"""Explicit-human conversation fan-out over the existing project dispatch owner.

Registered on the project coordination blueprint by api_v1 assembly. This module
owns HTTP adaptation only: no model, scheduler, store, grant or retry policy.
"""
from quart import request

from lib.api_response import api_ok
from lib.openapi import api_meta
from lib.project_dispatch.contract import parse, request_body, query_parameters
from lib.project_dispatch.repository import _validate_schema
from lib.request_parser import decode_proxy_path_arg

from .auth import require_scope
from .project_brain import (
    api_v1_project_brain_bp, _dispatch_service, _safe_route_error_response,
)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/batches', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Authorize one bounded parallel conversation batch before independent dispatch',
          tags=['project'], scope='chat', request_body=request_body('batchRequest'))
def project_batch_create():
    try:
        data = parse('batchRequest')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.authorize_batch_and_dispatch(data, principal=principal,
                      command_id=request.headers.get('Idempotency-Key') or ''))
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/batches', methods=['GET'])
@require_scope('chat')
@api_meta(summary='Read an exact batch command receipt without authorizing or dispatching',
          tags=['project'], scope='chat', parameters=query_parameters('batchQuery'))
def project_batch_receipt():
    try:
        query = {'path': decode_proxy_path_arg('path') or '',
                 'commandId': request.args.get('commandId') or ''}
        _validate_schema(query, 'batchQuery')
        service, principal = _dispatch_service(query['path'])
        return api_ok({'batch': service.batch_receipt(
            principal=principal, command_id=query['commandId'])})
    except Exception as exc:
        return _safe_route_error_response(exc)


@api_v1_project_brain_bp.route('/api/v1/project/coordination/batches/dispatch', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Retry only the original approved batch through existing conversation commands',
          tags=['project'], scope='chat', request_body=request_body('batchRetry'))
def project_batch_dispatch():
    try:
        data = parse('batchRetry')
        service, principal = _dispatch_service(data['projectPath'])
        return api_ok(service.dispatch_batch(data['batchId'], principal=principal))
    except Exception as exc:
        return _safe_route_error_response(exc)
