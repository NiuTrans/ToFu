"""routes/api_v1/usage.py — Per-key usage analytics.

Routes:
  GET /api/v1/usage              — usage for the calling key (or, if
                                    admin, optional `?key_id=` to inspect
                                    a different key)
  GET /api/v1/usage/summary      — admin-only: per-key totals across
                                    a window
"""

from __future__ import annotations

from quart import Blueprint, request

from lib.api_response import api_forbidden, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.usage_tracker import (
    all_keys_with_activity, usage_for_key, usage_summary,
)

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_usage_bp = Blueprint('api_v1_usage', __name__)


@api_v1_usage_bp.route('/api/v1/usage', methods=['GET'])
@require_scope('usage')
@api_meta(summary='Per-key usage analytics',
          description='Returns daily request/token counts for the '
                       'authenticated key. Admins can pass `?key_id=` '
                       'to inspect another key.',
          tags=['usage'], scope='usage',
          parameters=[
              {'name': 'days', 'in': 'query',
               'schema': {'type': 'integer', 'default': 30,
                          'minimum': 1, 'maximum': 90}},
              {'name': 'key_id', 'in': 'query',
               'schema': {'type': 'string'},
               'description': 'Admin only: inspect another key.'},
          ])
def get_usage():
    auth = current_auth()
    try:
        days = max(1, min(int(request.args.get('days') or 30), 90))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[usage] get_usage caught %s: %s', type(_e_audit).__name__, _e_audit)
        days = 30
    target_key = (request.args.get('key_id') or '').strip()
    if target_key:
        if not auth or not auth.has_scope('admin'):
            return api_forbidden(
                'admin scope required to inspect another key')
    else:
        target_key = (auth.key_id if auth else '_anon')
    return api_ok(usage_for_key(target_key, days=days))


@api_v1_usage_bp.route('/api/v1/usage/summary', methods=['GET'])
@require_scope('admin')
@api_meta(summary='Aggregate usage summary across all keys (admin)',
          tags=['usage'], scope='admin',
          parameters=[
              {'name': 'days', 'in': 'query',
               'schema': {'type': 'integer', 'default': 7,
                          'minimum': 1, 'maximum': 90}},
          ])
def get_summary():
    try:
        days = max(1, min(int(request.args.get('days') or 7), 90))
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[usage] get_summary caught %s: %s', type(_e_audit).__name__, _e_audit)
        days = 7
    summary = usage_summary(days=days)
    summary['active_keys'] = len(all_keys_with_activity())
    return api_ok(summary)


__all__ = ['api_v1_usage_bp']
