"""Normalized model-catalog HTTP boundary.

Routes:
  GET /api/v1/model-catalog — read persisted authority or an in-memory legacy
                              provider projection
  PUT /api/v1/model-catalog — atomically compare-and-swap the full catalog

The domain in :mod:`lib.model_catalog` is pure. This module alone owns config
file persistence, request identity, dispatcher reload, and response envelopes.
"""

from __future__ import annotations

import json
import time
from typing import Any

from quart import Blueprint, request

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_forbidden,
    api_internal_error,
    api_ok,
    api_payload_too_large,
    safe_route,
)
from lib.log import audit_log, get_logger
from lib.model_catalog import (
    CONTRACT_VERSION,
    ModelCatalogError,
    normalize_catalog,
    project_providers,
    provider_shells,
    public_provider_metadata,
    resolve_catalog,
)
from lib.openapi import api_meta
from lib.request_parser import async_parse_body, require_dict, require_int

from .auth import request_principal, require_scope


logger = get_logger(__name__)
api_v1_model_catalog_bp = Blueprint('api_v1_model_catalog', __name__)

_MAX_BODY_BYTES = 1024 * 1024


class _RevisionConflict(RuntimeError):
    def __init__(self, expected: int, current: int):
        super().__init__(
            f'model catalog revision conflict: expected {expected}, current {current}')
        self.expected = expected
        self.current = current


def _request_owner() -> int:
    return request_principal().require_owner(context='model catalog')


def _offering_health(catalog: dict) -> dict[str, dict]:
    """Best-effort provider-scoped slot health keyed by offering identity."""
    try:
        from lib.llm_dispatch import get_dispatcher
        slots = get_dispatcher().get_slots_info()
    except Exception as exc:
        logger.debug('[ModelCatalog] runtime health unavailable: %s', exc)
        slots = []
    if not isinstance(slots, list):
        slots = []

    now = time.time()
    health: dict[str, dict] = {}

    def _is_available(slot: dict) -> bool:
        try:
            cooldown_until = float(slot.get('cooldown_until') or 0)
        except (TypeError, ValueError):
            return False
        return bool(slot.get('available', True)) and cooldown_until <= now

    for offering_id, offering in catalog['offerings'].items():
        configuration = offering.get('configuration') or {}
        wire_ids = configuration.get('request_ids') or []
        if not wire_ids:
            wire_ids = configuration.get('aliases') or []
        if not wire_ids:
            wire_ids = [offering['model_id']]
        matching = [
            slot for slot in slots
            if slot.get('provider_id') == offering['provider_id']
            and slot.get('model') in wire_ids
        ]
        available = [
            slot for slot in matching
            if _is_available(slot)
        ]
        if not matching:
            health[offering_id] = {'healthy': False, 'status': 'unknown'}
        elif available and len(available) == len(matching):
            health[offering_id] = {
                'healthy': True,
                'status': 'healthy',
                'available_slots': len(available),
                'slots': len(matching),
            }
        elif available:
            health[offering_id] = {
                'healthy': True,
                'status': 'degraded',
                'available_slots': len(available),
                'slots': len(matching),
            }
        else:
            health[offering_id] = {
                'healthy': False,
                'status': 'down',
                'available_slots': 0,
                'slots': len(matching),
            }
    return health


def _response_payload(config: dict) -> dict[str, Any]:
    catalog = resolve_catalog(config)
    return {
        'contract_version': CONTRACT_VERSION,
        'revision': catalog['revision'],
        'catalog': catalog,
        'providers': public_provider_metadata(config),
        'health': _offering_health(catalog),
    }


@api_v1_model_catalog_bp.route('/api/v1/model-catalog', methods=['GET'])
@require_scope('admin')
@safe_route
@api_meta(
    summary='Read the normalized model catalog',
    description=(
        'Returns persisted catalog authority, or a read-only in-memory '
        'projection of legacy provider rows when no catalog is persisted.'),
    tags=['providers'],
    scope='admin',
)
async def get_model_catalog():
    try:
        _request_owner()
    except PermissionError as exc:
        return api_forbidden(str(exc))
    from routes.config import _read_server_config

    try:
        payload = _response_payload(_read_server_config())
    except ModelCatalogError as exc:
        logger.error('[ModelCatalog] persisted catalog is invalid: %s', exc)
        return api_internal_error(
            'Persisted model catalog is invalid',
            context='model_catalog.get',
        )
    return api_ok(payload)


@api_v1_model_catalog_bp.route('/api/v1/model-catalog', methods=['PUT'])
@require_scope('admin')
@safe_route
@api_meta(
    summary='Replace the normalized model catalog',
    description=(
        'Atomic compare-and-swap. expected_revision must match the current '
        'server revision; a stale writer receives HTTP 409 and must re-read.'),
    tags=['providers'],
    scope='admin',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {
            'type': 'object',
            'required': ['expected_revision', 'catalog'],
            'properties': {
                'expected_revision': {'type': 'integer', 'minimum': 0},
                'catalog': {'type': 'object'},
            },
            'additionalProperties': False,
        },
    }}},
)
async def put_model_catalog():
    try:
        owner_user_id = _request_owner()
    except PermissionError as exc:
        return api_forbidden(str(exc))

    if request.content_length is not None \
            and request.content_length > _MAX_BODY_BYTES:
        return api_payload_too_large(_MAX_BODY_BYTES)
    body = await async_parse_body()
    if len(json.dumps(body, separators=(',', ':')).encode()) > _MAX_BODY_BYTES:
        return api_payload_too_large(_MAX_BODY_BYTES)
    expected_revision = require_int(body, 'expected_revision', min=0)
    submitted_catalog = require_dict(body, 'catalog')
    unknown_fields = set(body) - {'expected_revision', 'catalog'}
    if unknown_fields:
        return api_bad_request(
            'unknown request fields: %s' % ', '.join(sorted(unknown_fields)))

    from lib.json_store import update_json_atomic
    from routes.config import _SERVER_CONFIG_PATH

    committed_catalog: dict[str, Any] = {}

    def _commit(current: Any) -> dict:
        if not isinstance(current, dict):
            current = {}
        current_catalog = resolve_catalog(current)
        current_revision = current_catalog['revision']
        if current_revision != expected_revision:
            raise _RevisionConflict(expected_revision, current_revision)
        shells = provider_shells(current)
        provider_ids = [
            str(shell.get('id') or shell.get('key') or shell.get('brand') or '')
            for shell in shells
        ]
        normalized = normalize_catalog(
            submitted_catalog,
            provider_ids=provider_ids,
            revision=current_revision + 1,
        )
        if submitted_catalog.get('revision') != expected_revision:
            raise ModelCatalogError(
                'catalog.revision must equal expected_revision')
        updated = dict(current)
        updated['model_catalog'] = normalized
        updated['providers'] = project_providers(shells, normalized)
        committed_catalog.update(normalized)
        return updated

    try:
        updated_config = update_json_atomic(
            _SERVER_CONFIG_PATH,
            _commit,
            default={},
            strict=True,
        )
    except _RevisionConflict as exc:
        return api_conflict(
            'model_catalog_revision_conflict',
            expected_revision=exc.expected,
            current_revision=exc.current,
        )
    except ModelCatalogError as exc:
        return api_bad_request(str(exc), field='catalog')
    except Exception as exc:
        logger.error('[ModelCatalog] atomic commit failed: %s', exc, exc_info=True)
        return api_internal_error(
            'Failed to persist model catalog', context='model_catalog.put')

    try:
        import lib as _lib
        _lib.reload_config()
    except Exception as exc:
        logger.warning('[ModelCatalog] config reload failed after commit: %s', exc)
    try:
        from lib.llm_dispatch import reset_dispatcher
        reset_dispatcher()
    except Exception as exc:
        logger.warning('[ModelCatalog] dispatcher reset failed after commit: %s', exc)

    audit_log(
        'model_catalog_replaced',
        owner_user_id=owner_user_id,
        revision=committed_catalog['revision'],
        models=len(committed_catalog['models']),
        offerings=len(committed_catalog['offerings']),
    )
    return api_ok(_response_payload(updated_config))


__all__ = ['api_v1_model_catalog_bp']
