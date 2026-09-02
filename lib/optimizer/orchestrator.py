"""lib/optimizer/orchestrator.py — Glue: run_once() end-to-end.

Steps per run:
  1. Revert any auto-applied actions whose ttl has expired.
  2. Gather evidence (see analyzer.gather_evidence).
  3. Ask the LLM for proposals (proposer.propose).
  4. For each proposal: apply if whitelisted & auto_apply=True, else
     store as pending_review.
  5. Emit ``audit_log('optimizer_run_complete', ...)`` summarising.
"""

from __future__ import annotations

from datetime import datetime

from lib.identity import PrincipalContext
from lib.log import audit_log, get_logger, log_context

from . import analyzer, applier, proposer

logger = get_logger(__name__)


def run_once(*, principal: PrincipalContext, dry_run: bool = False,
             llm_override=None,
             window_hours: int = 24) -> dict:
    """Run the optimiser pipeline once.

    Args:
        principal: Explicit owner-scoped user or system identity. The caller
            must carry ``optimizer:maintain`` (or ``admin``) authority.
        dry_run: If True, never actually apply anything; every proposal
            is stored with ``status='pending_review'`` and
            ``status_reason='dry_run'``.
        llm_override: Optional callable injected by tests — see
            ``proposer.propose``.
        window_hours: Evidence window size.

    Returns:
        Summary dict with keys: ``started_at``, ``finished_at``,
        ``reverts``, ``evidence_summary``, ``proposals``, ``applied``,
        ``pending_review``, ``rejected``.
    """
    if not isinstance(principal, PrincipalContext):
        raise TypeError('optimizer run requires PrincipalContext')
    principal.require_scope('optimizer:maintain')
    owner_user_id = principal.require_owner(context='optimizer run')

    started = datetime.now().isoformat()
    logger.info('[Optimizer] run_once starting dry_run=%s', dry_run)

    # Step 1: expire / revert
    try:
        reverts = applier.revert_expired_actions(
            owner_user_id=owner_user_id)
    except Exception as e:
        logger.error('[Optimizer] revert_expired_actions crashed: %s', e, exc_info=True)
        reverts = []

    # Step 2: evidence
    with log_context('Optimizer.gather_evidence', logger=logger):
        try:
            evidence = analyzer.gather_evidence(
                principal=principal, window_hours=window_hours)
        except Exception as e:
            logger.error('[Optimizer] gather_evidence crashed: %s', e, exc_info=True)
            # Degrade gracefully with empty evidence so the audit record still writes
            evidence = analyzer.EvidenceBundle(
                window_hours=window_hours,
                generated_at=started,
            )

    # Step 3: propose
    try:
        proposals = proposer.propose(evidence, llm_override=llm_override)
    except Exception as e:
        logger.error('[Optimizer] proposer.propose crashed: %s', e, exc_info=True)
        proposals = []

    # Step 4: apply / stage
    applied: list[dict] = []
    pending: list[dict] = []
    rejected: list[dict] = []
    for prop in proposals:
        try:
            outcome = applier.apply_proposal(
                prop, owner_user_id=owner_user_id, dry_run=dry_run)
        except Exception as e:
            logger.error('[Optimizer] apply_proposal crashed: %s', e, exc_info=True)
            rejected.append({'title': prop.get('title', ''),
                             'error': str(e)[:200]})
            continue
        if outcome.get('status') == 'applied':
            applied.append(outcome)
        elif outcome.get('status') == 'rejected':
            rejected.append(outcome)
        else:
            pending.append(outcome)

    finished = datetime.now().isoformat()
    summary = {
        'started_at': started,
        'finished_at': finished,
        'dry_run': dry_run,
        'reverts': reverts,
        'evidence_summary': {
            'window_hours': evidence.window_hours,
            'tool_call_counts_size': len(evidence.tool_call_counts),
            'tool_error_counts_size': len(evidence.tool_error_counts),
            'top_search_domains': len(evidence.top_search_domains),
            'irrelevant_dropped_domains': len(evidence.irrelevant_dropped_domains),
            'audit_event_counts': evidence.audit_event_counts,
            'prior_actions_count': len(evidence.prior_actions),
            'daily_report_snippets': len(evidence.daily_report_snippets),
        },
        'prior_actions': evidence.prior_actions,
        'proposals': proposals,
        'applied': applied,
        'pending_review': pending,
        'rejected': rejected,
    }

    audit_log(
        'optimizer_run_complete',
        user_id=owner_user_id,
        dry_run=dry_run,
        duration_s=(
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds(),
        reverts=len(reverts),
        proposals_produced=len(proposals),
        applied=len(applied),
        pending_review=len(pending),
        rejected=len(rejected),
    )
    logger.info('[Optimizer] run_once done: proposals=%d applied=%d pending=%d '
                'rejected=%d reverts=%d',
                len(proposals), len(applied), len(pending), len(rejected),
                len(reverts))
    return summary
