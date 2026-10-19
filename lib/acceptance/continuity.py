"""Bind owner requirements to an accepted continuation's original review scope.

Only command acceptance captures this identity in private attempt configuration.
Readers revalidate owned current attempts; the descriptor carries no tool, model,
wallet or lifecycle authority. Decisions still bind each current answer version.
"""
from functools import lru_cache

from lib.conversation_sync.execution_identity import REVIEW_SOURCE_CONFIG_KEY, REVIEW_SOURCE_SCHEMA
from lib.turn_lifecycle import get_attempt, get_turn, LifecycleNotFound


@lru_cache(maxsize=1)
def _validator():
    from jsonschema import Draft202012Validator
    return Draft202012Validator(REVIEW_SOURCE_SCHEMA)


def _owned_attempt(turn, principal, *, require_tenant=True):
    owner = principal.require_owner(context='Turn review continuity')
    attempt = get_attempt(turn['currentAttemptId'], user_id=owner, include_execution_config=True)
    if (attempt['turnId'] != turn['turnId'] or attempt['conversationId'] != turn['conversationId']
            or (require_tenant and (attempt.get('config') or {}).get('_turnPrincipalTenantId', '') != str(principal.tenant_id or ''))):
        raise LifecycleNotFound('Turn review execution scope unavailable')
    return attempt


def review_source(turn, principal, *, attempt=None):
    """Return a validated flat source, or None for a Turn's own review records."""
    if not turn.get('currentAttemptId'):
        return None
    attempt = attempt if attempt is not None else _owned_attempt(turn, principal, require_tenant=False)
    config = attempt.get('config') or {}
    if REVIEW_SOURCE_CONFIG_KEY not in config:
        return None
    source = config[REVIEW_SOURCE_CONFIG_KEY]
    if (attempt['attemptId'] != turn['currentAttemptId'] or attempt['turnId'] != turn['turnId']
            or attempt['conversationId'] != turn['conversationId']
            or config.get('_turnPrincipalTenantId', '') != str(principal.tenant_id or '')
            or not _validator().is_valid(source) or source['owner'] != principal.require_owner()
            or source['tenant'] != str(principal.tenant_id or '')
            or source['conversation'] != turn['conversationId'] or source['turn'] == turn['turnId']):
        raise LifecycleNotFound('Turn review source scope unavailable')
    original = get_turn(source['conversation'], source['turn'], user_id=source['owner'])
    if (original['actor'] != 'assistant' or original['laneId'] != 'main'
            or original['currentAttemptId'] != source['attempt']):
        raise LifecycleNotFound('Turn review source execution was superseded')
    origin_attempt = _owned_attempt(original, principal)
    if (origin_attempt['taskId'] != source['task']
            or REVIEW_SOURCE_CONFIG_KEY in (origin_attempt.get('config') or {})):
        raise LifecycleNotFound('Turn review source is not the original bound execution')
    return dict(source)


def capture_review_source(conversation_id, turn_id, attempt_id, task_id, *, principal):
    """Trusted command port; its caller proves the accepted continuation relation."""
    owner = principal.require_owner(context='Turn review source capture')
    turn = get_turn(conversation_id, turn_id, user_id=owner)
    if (turn['actor'] != 'assistant' or turn['laneId'] != 'main'
            or turn['currentAttemptId'] != attempt_id):
        raise LifecycleNotFound('Turn review source execution unavailable')
    attempt = _owned_attempt(turn, principal)
    if not task_id or attempt['taskId'] != task_id:
        raise LifecycleNotFound('Turn review source task unavailable')
    source = review_source(turn, principal, attempt=attempt)
    if source is not None:
        return source
    return dict(version=1, owner=owner, tenant=str(principal.tenant_id or ''),
                conversation=conversation_id, turn=turn_id, attempt=attempt_id, task=task_id)
