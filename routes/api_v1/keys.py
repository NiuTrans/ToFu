"""routes/api_v1/keys.py — Admin-scoped API key CRUD.

Routes:
  GET    /api/v1/keys              — list (no secrets)
  POST   /api/v1/keys              — create; returns plaintext token ONCE
  GET    /api/v1/keys/{id}         — fetch one
  PATCH  /api/v1/keys/{id}         — update (name, scopes, limits, disabled)
  DELETE /api/v1/keys/{id}         — revoke
  GET    /api/v1/keys/whoami       — return the current AuthContext

The Settings UI hits these to manage keys without dropping to the CLI.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_keys import (
    ALL_SCOPES, create_key, get_key_by_id, list_keys, revoke_key, update_key,
)
from lib.api_response import api_bad_request, api_created, api_not_found, api_ok
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import (
    optional_bool, optional_int, optional_list, optional_str, parse_body,
    require_str,
)

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_keys_bp = Blueprint('api_v1_keys', __name__)


@api_v1_keys_bp.route('/api/v1/keys/whoami', methods=['GET'])
@api_meta(summary='Current authentication context',
          description='Returns the resolved AuthContext for the caller. '
                       'Useful for clients verifying their token works.',
          tags=['keys'])
def whoami():
    ctx = current_auth()
    if ctx is None or not ctx.is_authenticated:
        return api_ok({'authenticated': False})
    return api_ok({
        'authenticated': True,
        'key_id': ctx.key_id,
        'name': ctx.name,
        'scopes': sorted(ctx.scopes),
        'rate_limit_rpm': ctx.rate_limit_rpm,
        'rate_limit_tpd': ctx.rate_limit_tpd,
        'via_tunnel_token': ctx.via_tunnel_token,
    })


@api_v1_keys_bp.route('/api/v1/keys', methods=['GET'])
@require_scope('admin')
@api_meta(summary='List API keys', tags=['keys'], scope='admin',
          responses={
              '200': {'description': 'OK',
                       'content': {'application/json': {
                           'schema': {'type': 'object',
                                      'properties': {
                                          'ok': {'type': 'boolean'},
                                          'keys': {'type': 'array',
                                                    'items': {'$ref': '#/components/schemas/ApiKey'}}}}}}},
          })
def list_keys_route():
    return api_ok(keys=list_keys())


@api_v1_keys_bp.route('/api/v1/keys', methods=['POST'])
@require_scope('admin')
@api_meta(summary='Create an API key',
          description='Returns the plaintext bearer token ONCE in the '
                       '`token` field. Store it — it cannot be recovered.',
          tags=['keys'], scope='admin',
          request_body={'required': True, 'content': {'application/json': {
              'schema': {'type': 'object',
                          'required': ['name', 'scopes'],
                          'properties': {
                              'name': {'type': 'string'},
                              'scopes': {'type': 'array',
                                          'items': {'type': 'string'}},
                              'rate_limit_rpm': {'type': 'integer'},
                              'rate_limit_tpd': {'type': 'integer'},
                              'expires_at': {'type': 'number',
                                              'description': 'Unix timestamp; null = never'},
                              'admin': {'type': 'boolean'},
                              'metadata': {'type': 'object'},
                          }}}}})
def create_key_route():
    body = parse_body()
    name = require_str(body, 'name', max_len=80)
    scopes = optional_list(body, 'scopes', item_type=str, default=[]) or []
    rpm = optional_int(body, 'rate_limit_rpm', default=60, min=0,
                        max=100_000) or 0
    tpd = optional_int(body, 'rate_limit_tpd', default=0, min=0,
                        max=10_000_000_000) or 0
    expires = body.get('expires_at')
    admin = optional_bool(body, 'admin', default=False)
    metadata = body.get('metadata') or {}
    if not isinstance(metadata, dict):
        return api_bad_request('metadata must be an object',
                                field='metadata')
    if scopes:
        unknown = [s for s in scopes if s not in ALL_SCOPES]
        if unknown:
            return api_bad_request(
                f'Unknown scopes: {", ".join(unknown)}', field='scopes',
                allowed=sorted(ALL_SCOPES))
    if not scopes and not admin:
        return api_bad_request(
            'At least one scope is required (or admin=true)', field='scopes')
    try:
        row, plaintext = create_key(
            name=name, scopes=scopes, rate_limit_rpm=rpm,
            rate_limit_tpd=tpd, expires_at=expires,
            metadata=metadata, admin=admin)
    except ValueError as e:
        return api_bad_request(str(e))
    return api_created(key=row, token=plaintext)


@api_v1_keys_bp.route('/api/v1/keys/<key_id>', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Get an API key', tags=['keys'], scope='admin')
def get_key_route(key_id):
    row = get_key_by_id(key_id)
    if row is None:
        return api_not_found('Key not found')
    return api_ok(key=row)


@api_v1_keys_bp.route('/api/v1/keys/<key_id>', methods=['PATCH'])
@require_scope('admin')
@api_meta(summary='Update an API key', tags=['keys'], scope='admin')
def update_key_route(key_id):
    body = parse_body()
    fields = {}
    if 'name' in body:
        fields['name'] = optional_str(body, 'name', default='', max_len=80)
    if 'scopes' in body:
        scopes = optional_list(body, 'scopes', item_type=str, default=[]) or []
        unknown = [s for s in scopes if s not in ALL_SCOPES]
        if unknown:
            return api_bad_request(
                f'Unknown scopes: {", ".join(unknown)}', field='scopes',
                allowed=sorted(ALL_SCOPES))
        fields['scopes'] = scopes
    if 'rate_limit_rpm' in body:
        fields['rate_limit_rpm'] = optional_int(
            body, 'rate_limit_rpm', default=0, min=0, max=100_000) or 0
    if 'rate_limit_tpd' in body:
        fields['rate_limit_tpd'] = optional_int(
            body, 'rate_limit_tpd', default=0, min=0,
            max=10_000_000_000) or 0
    if 'expires_at' in body:
        fields['expires_at'] = body.get('expires_at')
    if 'disabled' in body:
        fields['disabled'] = optional_bool(body, 'disabled', default=False)
    if 'metadata' in body:
        md = body.get('metadata') or {}
        if not isinstance(md, dict):
            return api_bad_request('metadata must be an object',
                                    field='metadata')
        fields['metadata'] = md
    if not fields:
        return api_bad_request('No updatable fields provided')
    if not update_key(key_id, **fields):
        return api_not_found('Key not found')
    return api_ok(key=get_key_by_id(key_id))


@api_v1_keys_bp.route('/api/v1/keys/<key_id>', methods=['DELETE'])
@require_scope('admin')
@api_meta(summary='Revoke an API key', tags=['keys'], scope='admin')
def delete_key_route(key_id):
    if not revoke_key(key_id):
        return api_not_found('Key not found')
    audit_log('api_key_revoked_route', key_id=key_id,
              by=(current_auth().key_id if current_auth() else ''))
    return api_ok({'revoked': key_id})


__all__ = ['api_v1_keys_bp']
