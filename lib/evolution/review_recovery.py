"""Frozen read-only review recovery after process loss or format refusal.

Runs own CAS, admission and task execution. This policy never replays an
executor. Terminal format refusals have known archived costs and their own finite
retry allowance. A registry miss alone is insufficient: the recorded process must
have disappeared in the same boot/PID namespace. Unsupported or inaccessible
platform evidence fails closed, as does an owner on another host/namespace.
"""
from .contracts import EvolutionStateError
from .stage_launch import read_launch

from lib.process_owner import process_owner as process_owner, owner_state as owner_state


def episode_complete(run, episode=None):
    losses = run.get('stage_losses') or []
    incomplete = [row for row in run['receipts'] if row.get('actuals_complete') is False]
    if incomplete and episode is None:
        return False
    if episode and any(row.get('actuals_complete') is False for row in episode_receipts(run, episode)):
        return False
    if not losses and not incomplete:
        return run.get('actuals_complete') is not False
    return not any(row['episode_id'] == run['episode_id'] for row in losses)


def episode_receipts(run, episode):
    start = next((index for index, row in enumerate(run['receipts'])
                  if row['task_id'] == episode['task_id']), None)
    return run['receipts'][start:] if start is not None else []


def validate_frozen_review(run, user_id):
    from .episodes import get_episode
    intent = read_launch(run.get('stage_launch'), run_id=run['run_id'], user_id=user_id)
    executed = next((row for row in reversed(run['receipts']) if row['stage'] == 'executing'), None)
    episode = get_episode(run['episode_id'], user_id=user_id)
    if (intent['stage'] != 'reviewing' or not executed
            or executed['task_id'] != intent['source_task_id'] or not run['result']
            or not episode or episode['status'] != 'running'
            or not episode_receipts(run, episode)
            or executed not in episode_receipts(run, episode)
            or executed.get('submission_voided')):
        raise EvolutionStateError('saved review has no matching consumed execution evidence')
    from .source_tools import scientific_review_binding_matches
    if not scientific_review_binding_matches(run, owner=user_id):
        raise EvolutionStateError('saved scientific review no longer matches the sealed experiment')
    return intent


def preparing_recovery(run, intent):
    """A lost-review marker, same episode and frozen request authorize one CAS."""
    losses = run.get('stage_losses') or []
    if not losses:
        return False
    latest = losses[-1]
    return (latest['admitted_recovery'] and latest['task_id'] == run['task_id']
            and latest['stage'] == 'reviewing'
            and latest['episode_id'] == run['episode_id']
            and latest['intent_id'] == run['stage_launch']['intent_id']
            and intent['stage'] == 'reviewing')


def format_retry_decision(run, user_id, *, exhausted, error='invalid_json'):
    """Format refusal reuses consumed execution evidence, never a new verdict."""
    if error not in ('invalid_json', 'text_limit', 'originals_uninspected'):
        raise EvolutionStateError('unsupported review format refusal')
    reference = run.get('stage_launch') or {}
    decision = {'error': error, 'admitted': False,
                'intent_id': reference.get('intent_id', ''), 'refusal': ''}
    episode = None
    try:
        validate_frozen_review(run, user_id)
        from .episodes import get_episode
        episode = get_episode(run['episode_id'], user_id=user_id)
    except (EvolutionStateError, ValueError):
        decision['refusal'] = 'frozen_evidence_unavailable'
        return decision
    prior = sum(bool(row.get('review_format_retry', {}).get('admitted'))
                for row in episode_receipts(run, episode))
    if prior >= run.get('max_review_format_retries', 0):
        decision['refusal'] = 'retry_budget'
    elif exhausted:
        decision['refusal'] = 'execution_budget'
    else:
        decision['admitted'] = True
    return decision


def preparing_format_retry(run, intent):
    """A committed consumed format failure authorizes its frozen review only."""
    latest = run['receipts'][-1] if run['receipts'] else {}
    marker = latest.get('review_format_retry') or {}
    return (marker.get('error') in ('invalid_json', 'text_limit', 'originals_uninspected') and marker.get('admitted') is True
            and not marker.get('refusal') and latest.get('task_id') == run['task_id']
            and latest.get('stage') == 'reviewing'
            and marker.get('intent_id') == run['stage_launch']['intent_id']
            and intent['stage'] == 'reviewing')
