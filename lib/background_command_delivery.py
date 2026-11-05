"""Bounded retries for publishing an already-finished command's result.

The caller freezes payload and identity once; queue.enqueue's durable receipt
makes a lost-ACK retry safe. completion_delivery_config projects recorded source
roots for a fresh task; it never transfers a previous workspace registration.
This does not claim recovery before first commit. owner_absence classifies a
refusal: only an owner-scoped read confirming the conversation absent makes it
CompletionOwnerAbsent; the command registry decides whether that is final.
"""
from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.project_dispatch.admission import KEY as DISPATCH_ADMISSION_KEY

logger = get_logger(__name__)


class CompletionOwnerAbsent(Exception):
    """queue.enqueue refused a receipt because its owner conversation is absent.

    Both storage backends commit a receipt only into an active conversation
    owned by the completion's user, so retrying the same request cannot succeed
    unless that conversation is created.
    """


def owner_absence(error: BaseException, conv_id: str, *, user_id: int) -> str | None:
    """Return why ``error`` proves the owner absent, or None while it is retryable.

    Only a ``database_not_found`` refusal confirmed by the owner-scoped
    ``conversation.get`` returning null counts. A present owner means the
    refusal concerned something else; an unanswered read is not absence.
    """
    if not isinstance(error, StorageError) or error.code != 'database_not_found':
        return None
    from lib.conversations.repository import get_conversation
    try:
        owner = get_conversation(conv_id, user_id=user_id, include_messages=False)
    except Exception as probe_error:
        logger.debug('[BackgroundDelivery] owner probe for %s failed; the refusal stays '
                     'retryable: %s: %s', conv_id, type(probe_error).__name__, probe_error)
        return None
    if owner is not None:
        return None
    return (f'owner conversation {conv_id!r} is absent for user {user_id} '
            f'({error.code}: {error.message})')


def publish_completion(enqueue: Callable[..., dict[str, Any]], *args: Any,
                       **kwargs: Any) -> dict[str, Any]:
    """Retry transient storage failures three times using identical arguments."""
    for attempt in range(3):
        try:
            return enqueue(*args, **kwargs)
        except StorageError as error:
            transient = error.retryable or error.code in {
                'database_busy', 'database_unavailable', 'database_timeout'}
            if not transient or attempt == 2:
                raise
            time.sleep(min(1.0, max(0.1 * (2 ** attempt),
                                   (error.retry_after_ms or 0) / 1000)))
    raise AssertionError('unreachable completion retry state')


# A completion starts a fresh delivery turn. These fields resume the old
# attempt and can hold multi-MiB buffers; they are not execution preferences.
_COMPLETION_ATTEMPT_FIELDS = frozenset({
    'assistantMsgId', 'excludeLast', 'resumePrefill', 'contentPrefix',
})


def completion_delivery_config(config: dict | None) -> dict:
    """Keep execution policy/preferences without copying a settled attempt."""
    original = config or {}
    cleaned = {key: value for key, value in original.items()
               if (key == DISPATCH_ADMISSION_KEY
                   or not str(key).startswith(('_', 'checkpoint')))
               and key not in _COMPLETION_ATTEMPT_FIELDS
               and key not in {'canonicalProjectPath', 'canonicalProjectPaths',
                               'workspaceRuntimeRoots'}}
    # A new task cannot inherit the settled task's registered checkout. Use
    # only the canonical roots recorded by admission, never inspect/adopt peers.
    mappings = []
    primary, canonical = original.get('projectPath'), original.get('canonicalProjectPath')
    if isinstance(primary, str) and isinstance(canonical, str) and canonical:
        mappings.append((primary, canonical))
    extras, canonical_extras = original.get('projectPaths'), original.get('canonicalProjectPaths')
    if (isinstance(extras, list) and isinstance(canonical_extras, list)
            and len(extras) == len(canonical_extras)):
        mappings.extend((source, target) for source, target in zip(extras, canonical_extras)
                        if isinstance(source, str) and isinstance(target, str) and target)
    mappings.sort(key=lambda pair: -len(pair[0]))

    def canonical_path(value):
        if not isinstance(value, str) or not Path(value).is_absolute():
            return value
        for source, target in mappings:
            if not Path(source).is_absolute() or not Path(target).is_absolute():
                continue
            try:
                suffix = Path(value).relative_to(source)
            except ValueError:
                continue
            return str(Path(target) / suffix)
        return value

    if 'projectPath' in cleaned:
        cleaned['projectPath'] = canonical_path(cleaned['projectPath'])
    for key in ('projectPaths', 'readOnlyPaths'):
        if isinstance(cleaned.get(key), list):
            cleaned[key] = [canonical_path(path) for path in cleaned[key]]
    return cleaned


def settle_revoked_dispatch_completion(config, receipts, *, conv_id, user_id):
    """Attach a revoked Worker's late results without starting another model turn.

    The cancelled execution remains the evidence owner. Attach before retiring
    durable delivery intent so an uncertain commit can only replay the same
    deterministic block; an unrelated human wake cannot race the attachment.
    """
    binding = (config or {}).get(DISPATCH_ADMISSION_KEY)
    if not isinstance(binding, dict):
        return False
    from lib.project_dispatch.repository import DispatchGrantRevoked, ProjectDispatchRepository
    repository = ProjectDispatchRepository(binding['owner'], binding['projectPath'])
    admission = repository.admission(binding['assignmentId'])
    if any(admission[key] != binding.get(key)
           for key in ('version', 'owner', 'tenant', 'projectPath', 'assignmentId', 'recipientId')):
        return False
    assignment = repository.read(binding['assignmentId'])
    execution = assignment.get('execution')
    if execution is None:
        return False
    try:
        repository.validate_own(binding)
    except DispatchGrantRevoked:
        pass
    else:
        return False
    from lib.turn_lifecycle import LifecycleConflict, update_turn_projection
    from lib.background_command_projection import background_command_injection_record
    turn = repository.turn_get(
        conv_id, execution['turnId'], user_id=user_id)
    attempt = repository.get_attempt(execution['attemptId'])
    if not isinstance(turn, dict) or turn.get('status') in {'pending', 'running'}:
        return True
    if not isinstance(attempt, dict) or attempt.get('status') in {'pending', 'running'}:
        return True
    deliveries = [{
        'queueId': row['queueId'],
        'commandId': (row.get('payload') or {}).get('_backgroundCommand'),
        'value': (row.get('payload') or {}).get('text') or '',
        'backgroundCommand': row.get('backgroundCommand'),
    } for row in receipts if isinstance(row.get('queueId'), str)
            and isinstance((row.get('payload') or {}).get('_backgroundCommand'), str)
            and (row.get('payload') or {}).get('text')]
    if not deliveries:
        return True
    receipt = background_command_injection_record({
        'roundNum': 0,
        'previews': [{
            'queueId': item['queueId'], 'commandId': item['commandId'],
            'text': item['value'][:1200], 'textTruncated': len(item['value']) > 1200,
            **({'backgroundCommand': item['backgroundCommand']}
               if isinstance(item.get('backgroundCommand'), dict) else {}),
        } for item in deliveries],
    }, attempt_id=execution['attemptId'])
    projection = dict(turn.get('projection') or {})
    previous = projection.get('_bgCommandInjects') or []
    if any(isinstance(row, dict) and row.get('blockId') == receipt['blockId']
           for row in previous):
        return True
    projection['_bgCommandInjects'] = [*previous, receipt]
    try:
        update_turn_projection(
            conv_id, execution['turnId'], projection=projection,
            expected_projection_revision=turn['projectionRevision'], user_id=user_id,
            preserve_activity=True)
    except LifecycleConflict as error:
        if error.code in {'turn_in_progress', 'stale_projection', 'database_conflict'}:
            return True
        raise
    from lib import agent_inbox
    try:
        agent_inbox.consume_peer(conv_id, [item['queueId'] for item in deliveries])
    except Exception as error:
        logger.debug('[BackgroundDelivery] cancelled Worker inbox fence failed: %s', error)
    # Late evidence must not claim model consumption. It is retained on the
    # cancelled Turn; only the automatic synthetic wake is retired.
    return True


def publish_completion_envelope(enqueue, conv_id, envelope, *, kind, user_id):
    """Replay frozen bytes first; repair legacy checkpoints only after refusal.

    The original envelope stays intact for lost-ACK recovery. A deterministic
    reduced config is attempted only after a permanent size/protocol refusal,
    under the SAME command/queue identity. The authority's payload fingerprint
    refuses any conflicting committed receipt; no second completion is minted.
    """
    config = envelope['config']
    try:
        return publish_completion(enqueue, conv_id, envelope['message'], config,
                                  kind=kind, user_id=user_id)
    except StorageError as error:
        cleaned = completion_delivery_config(config)
        if (error.retryable or error.code not in {
                'storage_payload_too_large', 'database_protocol_error'}
                or cleaned == config):
            raise
        return publish_completion(enqueue, conv_id, envelope['message'], cleaned,
                                  kind=kind, user_id=user_id)
