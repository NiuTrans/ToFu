"""lib/optimizer/applier.py — Whitelist-gated action application.

This is the ONLY place in the codebase that is allowed to call a
handler's ``apply()``.  Any ``action_type`` that is not marked
``auto_apply=True`` in ``ACTION_REGISTRY`` is stored as
``status='pending_review'`` and skipped — no exceptions.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

from . import storage
from .actions import ACTION_REGISTRY

logger = get_logger(__name__)


def apply_proposal(proposal: dict, *, dry_run: bool = False) -> dict:
    """Apply one validated proposal.

    Returns a dict describing the outcome:
        {proposal_id, action_type, status, detail?, error?}
    """
    action_type = proposal['action_type']
    entry = ACTION_REGISTRY.get(action_type)
    action_args = proposal.get('action_args') or {}
    ttl_days = int(proposal.get('ttl_days') or action_args.get('ttl_days') or 7)

    # Every proposal is persisted first (so humans can see what was suggested,
    # even if auto-apply is off).
    initial_status = 'applied' if (entry and entry.get('auto_apply')
                                    and not dry_run) else 'pending_review'
    status_reason = ''
    if initial_status == 'pending_review':
        if dry_run:
            status_reason = 'dry_run'
        elif not entry:
            status_reason = f'unknown action_type: {action_type}'
        else:
            status_reason = 'not in auto-apply whitelist'

    proposal_id = storage.create_proposal(
        title=proposal.get('title', ''),
        rationale=proposal.get('rationale', ''),
        action_type=action_type,
        action_args=action_args,
        severity=proposal.get('severity', 'low'),
        confidence=proposal.get('confidence', 0.5),
        evidence=proposal.get('evidence_ids', []),
        status='applied' if (initial_status == 'applied') else 'pending_review',
        status_reason=status_reason,
    )

    if initial_status == 'pending_review':
        return {
            'proposal_id': proposal_id,
            'action_type': action_type,
            'status': 'pending_review',
            'detail': status_reason,
        }

    # Auto-apply path
    try:
        apply_fn = entry['apply']
        if not callable(apply_fn):
            raise RuntimeError(f'action {action_type} has no apply handler')
        detail = apply_fn(action_args) or {}
    except Exception as e:
        logger.error('[Optimizer.applier] apply failed for proposal=%s type=%s: %s',
                     proposal_id, action_type, e, exc_info=True)
        storage.update_proposal_status(
            proposal_id, 'rejected',
            reason=f'apply failed: {type(e).__name__}: {str(e)[:200]}')
        audit_log('optimizer_action_failed',
                  proposal_id=proposal_id, action_type=action_type,
                  error=str(e)[:200])
        return {
            'proposal_id': proposal_id,
            'action_type': action_type,
            'status': 'rejected',
            'error': str(e)[:200],
        }

    # Record the apply in the action log for the learning loop
    storage.record_applied(
        proposal_id=proposal_id,
        ttl_days=ttl_days,
        pre_metric=proposal.get('pre_metric') or {},
    )

    return {
        'proposal_id': proposal_id,
        'action_type': action_type,
        'status': 'applied',
        'detail': detail,
    }


def revert_expired_actions() -> list[dict]:
    """Scan ``optimizer_action_log`` for rows past their ``expires_at`` and
    revert them.  Returns a list of revert descriptors."""
    reverts: list[dict] = []
    expired = storage.list_expired_applied_actions()
    for row in expired:
        action_type = row.get('p_action_type') or ''
        entry = ACTION_REGISTRY.get(action_type)
        import json
        try:
            args = json.loads(row.get('p_action_args') or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Optimizer.applier] bad action_args on expired row %s: %s',
                           row.get('id'), e)
            args = {}

        reason = f'ttl expired at {row.get("expires_at")}'
        if not entry or not callable(entry.get('revert')):
            logger.warning('[Optimizer.applier] no revert handler for expired '
                           'action=%s log_id=%s — marking expired without revert',
                           action_type, row.get('id'))
            storage.mark_reverted(row['id'], reason + ' (no revert handler)')
            storage.update_proposal_status(row['proposal_id'], 'expired', reason)
            reverts.append({'log_id': row['id'], 'status': 'expired',
                            'note': 'no revert handler'})
            continue

        try:
            entry['revert'](args)
        except Exception as e:
            logger.error('[Optimizer.applier] revert handler failed for %s: %s',
                         row.get('id'), e, exc_info=True)
            storage.mark_reverted(row['id'],
                                  f'revert errored: {type(e).__name__}: {str(e)[:120]}')
            storage.update_proposal_status(row['proposal_id'], 'expired',
                                           reason + ' (revert errored)')
            reverts.append({'log_id': row['id'], 'status': 'expired',
                            'error': str(e)[:200]})
            continue

        storage.mark_reverted(row['id'], reason)
        storage.update_proposal_status(row['proposal_id'], 'expired', reason)
        reverts.append({'log_id': row['id'], 'status': 'expired',
                        'action_type': action_type, 'args': args})

    if reverts:
        logger.info('[Optimizer.applier] auto-reverted %d expired action(s)',
                    len(reverts))
    return reverts
