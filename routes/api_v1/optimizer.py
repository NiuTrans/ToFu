"""routes/api_v1/optimizer.py — Daily Optimizer REST surface.

Routes:
  GET    /api/v1/optimizer/proposals              — list (read-only)
  GET    /api/v1/optimizer/proposals/<id>         — single proposal + action log
  POST   /api/v1/optimizer/proposals/<id>/approve — apply pending proposal (admin)
  POST   /api/v1/optimizer/proposals/<id>/reject  — mark rejected (admin)
  POST   /api/v1/optimizer/proposals/<id>/revert  — revert applied proposal (admin)
  POST   /api/v1/optimizer/run-now                — synchronous pipeline run (admin)

Reads stay accessible to any authenticated key so dashboards can show
historical proposals; mutations require ``admin`` scope (cookie-auth
UI calls implicitly have admin, so the in-app panel works as before).
"""

from __future__ import annotations

import json

from flask import Blueprint, request

from lib.api_response import (
    api_bad_request, api_internal_error, api_not_found, api_ok, safe_route,
)
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.optimizer import run_once as _run_once
from lib.optimizer import storage as _storage
from lib.optimizer.actions import ACTION_REGISTRY
from lib.request_parser import optional_bool, optional_int, optional_str, parse_body

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_optimizer_bp = Blueprint('api_v1_optimizer', __name__)


def _disabled_response():
    """Return a forbidden response when the optimizer feature flag is off."""
    import lib as _lib
    if getattr(_lib, 'OPTIMIZER_ENABLED', True):
        return None
    return api_bad_request(
        'Daily Optimizer is disabled. Enable it in Settings → '
        '功能模块 → 每日优化器.',
    ), 403


def _decode_json_columns(row, *cols):
    for col in cols:
        raw = row.get(col)
        if isinstance(raw, str) and raw:
            try:
                row[col] = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Optimizer.v1] decode %s failed: %s', col, e)


# ── Reads ────────────────────────────────────────────────────────────

@api_v1_optimizer_bp.route('/api/v1/optimizer/proposals', methods=['GET'])
@require_auth
@api_meta(
    summary='List optimizer proposals',
    description=(
        'Returns recent optimizer proposals (descending by creation time). '
        'Filter by ``status`` (pending_review / applied / rejected / '
        'expired / reverted) and limit with ``limit`` (1..500, default 50).'
    ),
    tags=['optimizer'],
)
@safe_route
def list_proposals():
    """List recent optimizer proposals (filterable by status).

    Wrapped in @safe_route: any uncaught exception (e.g. DB blip in
    _storage.list_proposals) is converted to a 500 envelope with the
    function qualname as source, replacing the ad-hoc try/except that
    duplicated api_internal_error's own logging.
    """
    status = (request.args.get('status') or '').strip() or None
    try:
        limit = max(1, min(500, int(request.args.get('limit') or 50)))
    except (TypeError, ValueError) as e:
        logger.debug('[Optimizer.v1] bad limit arg, defaulting to 50: %s', e)
        limit = 50
    rows = _storage.list_proposals(status=status, limit=limit)
    for r in rows:
        _decode_json_columns(r, 'action_args', 'evidence')
    return api_ok({'proposals': rows})


@api_v1_optimizer_bp.route('/api/v1/optimizer/proposals/<proposal_id>',
                            methods=['GET'])
@require_auth
@api_meta(
    summary='Get an optimizer proposal',
    description='Returns the proposal plus its action log entry (if any).',
    tags=['optimizer'],
)
@safe_route
def get_proposal(proposal_id):
    """Return one proposal plus its action-log row, or 404.

    @safe_route wraps the primary proposal lookup — any DB failure
    surfaces as a 500 with the correct source. The action_log lookup
    below keeps its own local try/except because it is DESIGNED to soft-
    fail (warning + None fallback + return the proposal anyway); that
    non-fatal path cannot be expressed via @safe_route.
    """
    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return api_not_found('Proposal not found')

    _decode_json_columns(prop, 'action_args', 'evidence')

    try:
        action_log = _storage.get_action_log_for_proposal(proposal_id)
    except Exception as e:
        logger.warning('[Optimizer.v1] action_log lookup failed: %s', e)
        action_log = None
    if action_log:
        _decode_json_columns(action_log, 'pre_metric', 'outcome_metric')

    return api_ok({'proposal': prop, 'action_log': action_log})


# ── Mutations ────────────────────────────────────────────────────────

@api_v1_optimizer_bp.route(
    '/api/v1/optimizer/proposals/<proposal_id>/approve', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Approve and apply a proposal',
    description=(
        'Synchronously applies the proposal\'s action. Only ``action_type``s '
        'whitelisted for auto-apply (see ``lib/optimizer/actions/``) can be '
        'approved this way; non-whitelisted action_types are auto-rejected '
        'with a reason.'
    ),
    tags=['optimizer'], scope='admin',
)
def approve_proposal(proposal_id):
    """Apply a pending proposal synchronously (admin scope)."""
    blocked = _disabled_response()
    if blocked is not None:
        return blocked

    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return api_not_found('Proposal not found')
    if prop.get('status') == 'applied':
        return api_ok({'status': 'applied', 'detail': 'already applied'})
    action_type = prop['action_type']
    entry = ACTION_REGISTRY.get(action_type)
    if not entry or not entry.get('auto_apply') or not callable(entry.get('apply')):
        logger.warning('[Optimizer.v1] approve rejected — %s not in '
                       'auto-apply whitelist', action_type)
        _storage.update_proposal_status(
            proposal_id, 'rejected',
            reason='manual approve blocked: action_type not in auto-apply whitelist')
        return api_bad_request(
            f'action_type {action_type} is not in the auto-apply whitelist',
            field='action_type')

    try:
        args = json.loads(prop.get('action_args') or '{}')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Optimizer.v1] bad action_args JSON for %s: %s',
                       proposal_id, e)
        args = {}
    ttl_days = int(args.get('ttl_days') or 7)

    try:
        detail = entry['apply'](args) or {}
    except Exception as e:
        logger.error('[Optimizer.v1] approve apply failed: %s', e,
                     exc_info=True)
        _storage.update_proposal_status(
            proposal_id, 'rejected',
            reason=f'manual approve failed: {type(e).__name__}: {str(e)[:120]}')
        audit_log('optimizer_action_failed',
                  proposal_id=proposal_id, action_type=action_type,
                  error=str(e)[:200], path='manual_approve')
        return api_internal_error(e, context='approve.apply',
                                  source='api_v1.optimizer.approve')

    _storage.update_proposal_status(proposal_id, 'applied',
                                     reason='manual approve')
    _storage.record_applied(proposal_id=proposal_id, ttl_days=ttl_days,
                             pre_metric={})
    audit_log('optimizer_action_manual_approve',
              proposal_id=proposal_id, action_type=action_type,
              ttl_days=ttl_days, detail=detail)
    return api_ok({'status': 'applied', 'detail': detail})


@api_v1_optimizer_bp.route(
    '/api/v1/optimizer/proposals/<proposal_id>/reject', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Reject a proposal',
    description='Marks the proposal as rejected with an optional reason.',
    tags=['optimizer'], scope='admin',
)
def reject_proposal(proposal_id):
    """Mark a proposal as rejected with an optional reason (admin scope)."""
    blocked = _disabled_response()
    if blocked is not None:
        return blocked

    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return api_not_found('Proposal not found')
    body = parse_body()
    reason = optional_str(body, 'reason', default='', max_len=400)
    _storage.update_proposal_status(proposal_id, 'rejected',
                                     reason=reason or 'manual reject')
    audit_log('optimizer_proposal_reject',
              proposal_id=proposal_id, reason=reason[:200])
    return api_ok({'status': 'rejected'})


@api_v1_optimizer_bp.route(
    '/api/v1/optimizer/proposals/<proposal_id>/revert', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Revert an applied proposal',
    description='Calls the action\'s ``revert`` handler; only valid in '
                '``applied`` state.',
    tags=['optimizer'], scope='admin',
)
def revert_proposal(proposal_id):
    """Revert an applied proposal via the action's revert handler (admin scope)."""
    blocked = _disabled_response()
    if blocked is not None:
        return blocked

    prop = _storage.get_proposal(proposal_id)
    if not prop:
        return api_not_found('Proposal not found')
    if prop.get('status') != 'applied':
        return api_bad_request(
            f'can only revert applied proposals '
            f'(current status={prop.get("status")})',
            field='status')

    action_type = prop['action_type']
    entry = ACTION_REGISTRY.get(action_type)
    if not entry or not callable(entry.get('revert')):
        return api_bad_request(f'no revert handler for {action_type}',
                               field='action_type')

    try:
        args = json.loads(prop.get('action_args') or '{}')
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Optimizer.v1] bad action_args on revert %s: %s',
                       proposal_id, e)
        args = {}
    try:
        detail = entry['revert'](args) or {}
    except Exception as e:
        logger.error('[Optimizer.v1] revert failed: %s', e, exc_info=True)
        return api_internal_error(e, context='revert.apply',
                                  source='api_v1.optimizer.revert')

    action_log = _storage.get_action_log_for_proposal(proposal_id)
    if action_log and not action_log.get('reverted_at'):
        _storage.mark_reverted(action_log['id'], reason='manual revert')
    _storage.update_proposal_status(proposal_id, 'reverted',
                                     reason='manual revert')
    audit_log('optimizer_revert_manual',
              proposal_id=proposal_id, action_type=action_type, detail=detail)
    return api_ok({'status': 'reverted', 'detail': detail})


@api_v1_optimizer_bp.route('/api/v1/optimizer/run-now', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Run the optimizer pipeline now',
    description=(
        'Synchronously runs analyser → proposer → applier. Body fields: '
        '``dry_run`` (bool, default false), ``window_hours`` (int 1..336, '
        'default 24).'
    ),
    tags=['optimizer'], scope='admin',
    request_body={'required': False, 'content': {'application/json': {
        'schema': {'type': 'object', 'properties': {
            'dry_run': {'type': 'boolean', 'default': False},
            'window_hours': {'type': 'integer',
                              'minimum': 1, 'maximum': 336,
                              'default': 24}}}}}},
)
@safe_route
def run_now():
    """Run the optimizer pipeline (analyser → proposer → applier) now (admin scope).

    @safe_route: a crash in _run_once (analyser/proposer/applier) becomes
    a 500 envelope; the ad-hoc try/except replicated api_internal_error's
    logging so the decorator is a pure simplification.
    """
    blocked = _disabled_response()
    if blocked is not None:
        return blocked

    body = parse_body()
    dry_run = optional_bool(body, 'dry_run', default=False)
    window_hours = optional_int(body, 'window_hours', default=24,
                                 min=1, max=24 * 14)

    summary = _run_once(dry_run=dry_run, window_hours=window_hours)
    return api_ok({'summary': summary})


__all__ = ['api_v1_optimizer_bp']
