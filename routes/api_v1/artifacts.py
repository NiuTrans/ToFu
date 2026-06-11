"""routes/api_v1/artifacts.py — Artifact metadata + lifecycle.

Routes:
  GET    /api/v1/artifacts                  — list (optional ``?conv=<id>`` filter)
  GET    /api/v1/artifacts/<id>             — single artifact metadata
  GET    /api/v1/artifacts/<id>/versions    — version chain
  POST   /api/v1/artifacts/<id>/pin         — toggle pin flag
  DELETE /api/v1/artifacts/<id>             — soft delete
  POST   /api/v1/artifacts/scan             — re-scan a conv's messages
                                              for inline artifacts

The binary / HTML carve-outs stay in :mod:`routes.artifacts`:

  GET /api/artifacts/<id>/raw      — raw bytes with strict CSP
  GET /api/artifacts/<id>/view     — HTML wrapper with KaTeX (CSP-ed)
  GET /api/artifacts/<id>/export   — Playwright-rendered PDF download

These three are not JSON REST verbs (they ship typed binary / sandboxed
HTML with custom Content-Disposition + CSP headers — not v1 envelope shape).
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from lib.api_response import api_bad_request, api_internal_error, api_not_found
from lib.artifacts import (
    ArtifactNotFoundError, delete_artifact, get_artifact_meta,
    list_artifacts, list_pinned_or_recent, list_versions, scan_message,
    set_pinned,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import async_parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_artifacts_bp = Blueprint('api_v1_artifacts', __name__)


def _strip_meta(meta: dict) -> dict:
    """Sanitize an artifact meta row before returning it (placeholder)."""
    return dict(meta)


@api_v1_artifacts_bp.route('/api/v1/artifacts', methods=['GET'])
@require_auth
@api_meta(
    summary='List artifacts',
    description=(
        'Cross-conversation listing. Pass ``?conv=<id>`` to scope to one '
        'conversation (newest first); otherwise returns pinned + recent '
        'across all conversations, capped by ``?limit=`` (1..200, default 50).'
    ),
    tags=['artifacts'],
)
async def list_artifacts_v1():
    conv = (request.args.get('conv') or '').strip()
    if conv:
        items = list_artifacts(conv)
        return jsonify({
            'conv_id': conv,
            'count': len(items),
            'artifacts': [_strip_meta(m) for m in items],
        })

    try:
        limit = int(request.args.get('limit', '50'))
    except (TypeError, ValueError) as e:
        logger.debug('[Artifacts.v1] bad limit, defaulting to 50: %s', e)
        limit = 50
    limit = max(1, min(limit, 200))
    items = list_pinned_or_recent(limit=limit)
    return jsonify({'count': len(items), 'artifacts': items})


@api_v1_artifacts_bp.route('/api/v1/artifacts/<artifact_id>', methods=['GET'])
@require_auth
@api_meta(summary='Get artifact metadata', tags=['artifacts'])
async def get_artifact_v1(artifact_id):
    try:
        meta = get_artifact_meta(artifact_id)
    except ArtifactNotFoundError:
        return api_not_found('not_found')
    return jsonify(_strip_meta(meta))


@api_v1_artifacts_bp.route('/api/v1/artifacts/<artifact_id>/versions',
                            methods=['GET'])
@require_auth
@api_meta(
    summary='Get the version chain for an artifact',
    description='Returns oldest \u2192 newest. 404 if artifact unknown.',
    tags=['artifacts'],
)
async def list_versions_v1(artifact_id):
    chain = list_versions(artifact_id)
    if not chain:
        return api_not_found('not_found')
    return jsonify({'count': len(chain), 'versions': chain})


@api_v1_artifacts_bp.route('/api/v1/artifacts/<artifact_id>/pin',
                            methods=['POST'])
@require_auth
@api_meta(
    summary='Set/clear the pin flag',
    description='Body: ``{pinned: bool}`` (default ``true``).',
    tags=['artifacts'],
)
async def toggle_pin_v1(artifact_id):
    body = await async_parse_body()
    pinned = bool(body.get('pinned', True))
    if not set_pinned(artifact_id, pinned):
        return api_not_found('not_found_or_failed')
    try:
        meta = get_artifact_meta(artifact_id)
    except ArtifactNotFoundError:
        return api_not_found('not_found')
    return jsonify(_strip_meta(meta))


@api_v1_artifacts_bp.route('/api/v1/artifacts/<artifact_id>',
                            methods=['DELETE'])
@require_auth
@api_meta(summary='Soft-delete an artifact', tags=['artifacts'])
async def delete_artifact_v1(artifact_id):
    if not delete_artifact(artifact_id):
        return api_not_found('not_found_or_failed')
    try:
        audit_log('artifact_delete_route', artifact_id=artifact_id,
                  ip=request.remote_addr)
    except Exception as e:
        logger.debug('[Artifacts.v1] audit_log failed: %s', e)
    return jsonify({'deleted': True})


@api_v1_artifacts_bp.route('/api/v1/artifacts/scan', methods=['POST'])
@require_auth
@api_meta(
    summary='Re-scan a conversation\'s messages for inline artifacts',
    description=(
        'Body: ``{conv_id}``. Idempotent \u2014 dedupe via '
        '``content_sha256`` reuses existing rows. Returns '
        '``{conv_id, scanned, created, artifacts}``.'
    ),
    tags=['artifacts'],
)
async def scan_conv_v1():
    import json as _json

    from lib.database import DOMAIN_CHAT, async_fetchone
    from routes.common import DEFAULT_USER_ID

    body = await async_parse_body()
    conv_id = (body.get('conv_id') or '').strip()
    if not conv_id:
        return api_bad_request('conv_id is required', field='conv_id')

    row = await async_fetchone(
        'SELECT messages FROM conversations WHERE id=? AND user_id=?',
        (conv_id, DEFAULT_USER_ID),
        domain=DOMAIN_CHAT,
    )
    if not row:
        return api_not_found('conv_not_found')
    raw = row['messages']
    try:
        messages = _json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError) as e:
        logger.warning('[Artifacts.v1:scan] failed to parse messages '
                       'for conv=%s: %s', conv_id[:8], e)
        return api_internal_error('invalid_messages_blob')

    scanned = 0
    created_meta: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        if m.get('role') != 'assistant':
            continue
        content = m.get('content') or ''
        if not isinstance(content, str) or not content.strip():
            continue
        scanned += 1
        msg_id = m.get('_msgId') or ''
        try:
            created = scan_message(conv_id, content, msg_id=msg_id,
                                    task_id='', task=None)
        except Exception as e:
            logger.warning('[Artifacts.v1:scan] failed for conv=%s msg=%s: %s',
                           conv_id[:8], msg_id[:8], e, exc_info=True)
            continue
        created_meta.extend(created)

    logger.info('[Artifacts.v1:scan] backfill conv=%s scanned=%d created=%d',
                conv_id[:8], scanned, len(created_meta))
    return jsonify({
        'conv_id': conv_id,
        'scanned': scanned,
        'created': len(created_meta),
        'artifacts': created_meta,
    })


__all__ = ['api_v1_artifacts_bp']
