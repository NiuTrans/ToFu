"""Reuse an interrupted Turn's persisted working view on Continue.

The owner-scoped compaction archive is the only persisted snapshot. It holds
accepted compactions and, for every root Turn that does not complete, the
suspended working view rewound to its last protocol-valid boundary
(``preserve_suspended_working_view``). Turn history remains authoritative.
Policy/history/tool-prefix mismatch, missing archives, unreproducible media,
unconsumed inbox carriers and cancellation fall back to full canonical replay.
Each resume reports its outcome so the Turn timeline can mark earlier
compactions it did not reuse (``report_compaction_resume``).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

from lib.log import get_logger
from lib.tasks_pkg.inbox_delivery import UNVERIFIED_INBOX_DELIVERY
from lib.tasks_pkg.model_replay import policy_witness
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

logger = get_logger(__name__)
_MAX_ARCHIVE_CANDIDATES = 8
# Receipt implementation of a suspended-working-view checkpoint, derived from an
# accepted compaction or standing alone; never a compaction event itself.
CHECKPOINT_IMPLEMENTATION = 'suspended_working_context'


def _digest(value):
    digest = hashlib.sha256()
    for part in json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':'), allow_nan=False).iterencode(value):
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def _history_digest(config):
    proof = config.get('_modelReplayHistoryProof')
    base = config.get('_modelReplayBase')
    if not isinstance(proof, dict) and not isinstance(base, list):
        return None
    return _digest({'proof': proof} if isinstance(proof, dict) else {'turns': base})


def resume_background_digest(receipts):
    """Guard authoritative late background deliveries outside the tool prefix."""
    return _digest(receipts or [])


def resume_execution_digest(content, thinking):
    """Bind suspended prose to the source Turn's canonical text lanes."""
    return _digest({'content': content or '', 'thinking': thinking or ''})


def compaction_resume_witness(task, *, rounds=None, round_count=None):
    """Describe the exact source prefix at a complete boundary of the view.

    ``round_count`` names how many of ``rounds`` the view already contains;
    a rewound suspension omits the unfinished batch and every round after it.

    Consumed steers stay in the witnessed view: the Turn keeps only their
    display preview, so a canonical rebuild would lose them. A carrier the
    provider has not consumed is salvaged to the queue or redelivered from its
    durable row, so restoring it would deliver it twice.
    """
    if (not isinstance(task, dict) or task.get('aborted') or not task.get('_turnId')
            or task.get(UNVERIFIED_INBOX_DELIVERY) or task.get('_turnVisibleRunTurnIds')):
        return None
    config = task.get('config') or {}
    if any(config.get(key) for key in ('flowId', 'flowBuiltin', 'flowDefinition')):
        return None
    history = _history_digest(config)
    if history is None:
        return None
    if rounds is None:
        from lib.tasks_pkg.manager._persist import _merge_tool_rounds
        rounds = _merge_tool_rounds(task)
    count = len(rounds) if round_count is None else round_count
    # Media needs no witness field: restore proves each retained image against
    # current authority before the snapshot can replace fresh history.
    return {'version': 1, 'turnId': task['_turnId'],
            'policyDigest': policy_witness(config), 'historyDigest': history,
            'backgroundDigest': resume_background_digest(task.get('_bgCommandInjects')),
            'toolRoundCount': count,
            'toolPrefixDigest': _digest(
                _reconstruct_tool_call_messages(list(rounds[:count])) or [])}


def _is_compaction_receipt(receipt):
    """A standalone checkpoint carries no summary; everything else compacted."""
    return 'summary' in receipt or receipt.get('implementation') != CHECKPOINT_IMPLEMENTATION


def _witness_mismatch(witness, receipt, config, rounds, history):
    """Name the first fresh source this Turn's archive no longer matches."""
    count = witness.get('toolRoundCount')
    if witness.get('policyDigest') != policy_witness(config):
        return 'settings_changed'
    if witness.get('historyDigest') != history:
        return 'history_changed'
    summary = receipt.get('summary') or {}
    if _is_compaction_receipt(receipt) and (
            summary.get('accepted') is not True or summary.get('complete') is not True):
        return 'unavailable'
    if (witness.get('backgroundDigest') != config.get(
            '_resumeBackgroundDigest', resume_background_digest([]))
            or (witness.get('executionDigest') is not None
                and witness['executionDigest'] != config.get('_resumeExecutionDigest'))
            or type(count) is not int or not 0 <= count <= len(rounds)
            or witness.get('toolPrefixDigest') != _digest(
                _reconstruct_tool_call_messages(list(rounds[:count])) or [])):
        return 'progress_changed'
    return None


def restore_compacted_resume(task, config, rounds, *, fresh_messages=(), current_messages=()):
    """Return detached archived context plus uncovered rounds, or None.

    At most eight metadata entries and one accepted bounded post snapshot are
    used. Every read passes the authenticated owner to the selected repository.
    The caller keeps the full rounds for display/accounting and side-effect
    recovery; only the model projection is replaced. Captured image bytes in
    the snapshot must be reproducible from ``current_messages`` (the fresh
    authoritative projection) or from their recorded sources. The outcome is
    kept in ``task['_compactionResumeOutcome']`` for the resume report.
    """
    projected, reason, archive = _restore_compacted_resume(
        task, config, rounds, fresh_messages, current_messages)
    task['_compactionResumeOutcome'] = {'reason': reason, 'archiveId': archive}
    return projected


def _restore_compacted_resume(task, config, rounds, fresh_messages, current_messages):
    if (task.get('aborted') or config.get('toolHistory') or fresh_messages is None
            or any(not isinstance(message, dict) for message in fresh_messages)):
        return None, 'ineligible', None
    history = _history_digest(config)
    if history is None:
        return None, 'ineligible', None
    reason = None
    try:
        from lib.agent_core.store import get_conversation_store
        from lib.tasks_pkg.manager import task_user_id
        from lib.agent_core.model_context_limits import MAX_ENTRY_BYTES, MAX_MESSAGES
        owner, conversation = task_user_id(task), task.get('convId')
        if not conversation:
            return None, 'ineligible', None
        store = get_conversation_store()
        archives = store.list_compaction_archives(
            conversation, user_id=owner, limit=_MAX_ARCHIVE_CANDIDATES)
        for metadata in archives:
            if task.get('aborted'):
                return None, 'ineligible', None
            if metadata.get('resultStatus') != 'completed':
                continue
            item = store.get_compaction_archive(
                conversation, metadata['id'], user_id=owner,
                include_messages=False, include_post_messages=False)
            receipt = ((item or {}).get('archive') or {}).get('receipt') or {}
            witness = receipt.get('resumeWitness') or {}
            if witness.get('version') != 1 or witness.get('turnId') != task.get('_turnId'):
                continue
            mismatch = _witness_mismatch(witness, receipt, config, rounds, history)
            if mismatch:
                # The newest witnessed archive of this Turn names the cause.
                reason = reason or mismatch
                continue
            item = store.get_compaction_archive(
                conversation, metadata['id'], user_id=owner,
                include_messages=False, include_post_messages=True)
            if ((item or {}).get('archive') or {}).get('receipt') != receipt:
                return None, 'unavailable', None
            post = (item or {}).get('postMessages')
            if (not isinstance(post, list) or not 0 < len(post) <= MAX_MESSAGES
                    or any(not isinstance(message, dict) for message in post)):
                return None, 'unavailable', None
            # The existing continuation size bound also caps restore work.
            encoded_bytes = 0
            for part in json.JSONEncoder(ensure_ascii=False).iterencode(post):
                encoded_bytes += len(part.encode('utf-8'))
                if encoded_bytes > MAX_ENTRY_BYTES:
                    break
            if encoded_bytes > MAX_ENTRY_BYTES:
                return None, 'unavailable', None
            from lib.tasks_pkg.compaction._handoff import is_compaction_handoff
            if (_is_compaction_receipt(receipt)
                    and not any(is_compaction_handoff(message) for message in post)):
                return None, 'unavailable', None
            from lib.tasks_pkg.compaction._continuity import read_continuity
            read_continuity(post, task)  # Corrupt state cannot be an accepted replay.
            from lib.tasks_pkg.model_images import retained_images_reproducible
            if not retained_images_reproducible(
                    post, current_messages=current_messages, user_id=owner):
                return None, 'media_unavailable', None
            from lib.tasks_pkg.message_input_receipts import historical_swarm_context
            count = witness['toolRoundCount']
            projected = historical_swarm_context(
                post, binding=task.get('_swarmMessageBinding'))
            projected.extend(_reconstruct_tool_call_messages(list(rounds[count:])) or [])

            # Hydration proves this tail follows unchanged current input.
            # Tool-prefix evidence alone cannot authorize dropping fresh
            # guidance, Composer carriers or non-tool prose.
            for message in fresh_messages:
                # Only Composer-owned content-addressed carriers may already
                # be present. Equal human/assistant occurrences stay distinct.
                if message.get('_contextComposer') and message in projected:
                    continue
                projected.append(deepcopy(message))
            if task.get('aborted'):
                return None, 'ineligible', None
            if _is_compaction_receipt(receipt):
                task['_acceptedCompactionReceipt'] = deepcopy(receipt)
            if receipt.get('implementation') == CHECKPOINT_IMPLEMENTATION:
                # A later interruption supersedes this checkpoint and the
                # Turn's completion retires it.
                task['_resumedCheckpointArchiveId'] = metadata['id']
            task['_compactionResumeStatus'] = 'restored'
            logger.info('[CompactionResume] conv=%s archive=%s covered=%d suffix=%d messages=%d',
                        conversation, metadata['id'], count, len(rounds) - count, len(projected))
            return projected, 'restored', metadata['id']
    except Exception as exc:
        logger.debug('[CompactionResume] unavailable: %s', type(exc).__name__)
        return None, 'unavailable', None
    return None, reason or 'no_witness', None


def report_compaction_resume(task, config, rounds):
    """Publish whether this resumed attempt reused its predecessors' context.

    ``sourceAttemptIds`` names the earlier attempts of this Turn whose model
    view the resume replaces: the accepted resume source plus every producer
    of a retained checkpoint round. The Turn timeline marks their accepted
    compactions that no longer shape the context; the report changes neither
    the model view nor execution recovery. Observability never aborts resume.
    """
    from lib.tool_results.tool_round_identity import execution_identity
    outcome = task.get('_compactionResumeOutcome') or {}
    reason = outcome.get('reason') or 'ineligible'
    current = task.get('_attemptId') or ''
    source = config.get('_resumeSource')
    lineage = dict.fromkeys(
        attempt for attempt, _ in map(execution_identity, [
            source if isinstance(source, dict) else None, *rounds])
        if attempt and attempt != current)
    try:
        from lib.agent_core.events import EventType, build_event
        from lib.tasks_pkg.manager import append_event
        append_event(task, build_event(
            EventType.COMPACTION_RESUME,
            convId=str(task.get('convId') or ''),
            restored=reason == 'restored',
            reason=reason,
            sourceAttemptIds=list(lineage),
            **({'archiveId': str(outcome['archiveId'])} if outcome.get('archiveId') else {}),
            ts=int(time.time()),
        ))
    except Exception as exc:
        logger.warning('[CompactionResume] report unavailable: %s', type(exc).__name__)


def _closed_working_view(messages, rounds, *, compacted=False):
    """Return ``(view, settled_round_count)`` at the last protocol-valid boundary.

    An interruption can stop inside a tool batch. Rewinding to the request that
    produced that batch (the batch and its partial results are dropped) is the
    resume contract: unfinished calls come back only as the settled receipts
    Continue replays after the view. ``None`` means no such boundary exists.
    An uncompacted view must hold every round it witnesses; only an accepted
    compaction may have folded earlier rounds into its handoff.
    """
    from lib.tasks_pkg.compaction._handoff import has_open_tool_batch
    from lib.tool_results.tool_round_replay import scan_replayable_tool_round_prefix
    gap = scan_replayable_tool_round_prefix(rounds).blocked_position
    count = len(rounds) if gap is None else gap
    view = list(messages)
    if has_open_tool_batch(view):
        from lib.tasks_pkg.compaction._layer2._anchor import _apiform_tool_rounds
        spans = _apiform_tool_rounds(view)
        if not spans or spans[-1][1] != len(view):
            return None
        start = spans[-1][0]
        open_ids = {call.get('id') for call in view[start].get('tool_calls') or []
                    if isinstance(call, dict)}
        view = view[:start]
        if has_open_tool_batch(view):
            return None
        count = min(count, next((index for index, row in enumerate(rounds)
                                 if row.get('toolCallId') in open_ids), len(rounds)))
    held = {call.get('id') for message in view if message.get('role') == 'assistant'
            for call in message.get('tool_calls') or [] if isinstance(call, dict)}
    # Rounds after the boundary come back through replay; the view must not
    # already hold one, or Continue would deliver it twice.
    if any(row.get('toolCallId') in held for row in rounds[count:] if row.get('toolCallId')):
        return None
    if not compacted and any(row.get('toolCallId') not in held
                             for row in rounds[:count] if row.get('toolCallId')):
        return None
    return view, count


def _resumable_root_turn(task):
    from lib.tasks_pkg.invocation import is_child_invocation
    return (bool(task.get('_turnId')) and not is_child_invocation(task)
            and not task.get('_archiveScope'))


def _turn_completed(task):
    from lib.turn_verdict import derive_turn_verdict, task_terminal_evidence
    return derive_turn_verdict(task_terminal_evidence(task)).status.value == 'completed'


def preserve_suspended_working_view(task):
    """Save an interrupted root Turn's working view through the archive owner.

    Continue restores it before composing context, so the resumed request
    keeps every context carrier, steer and tool result the interrupted one
    carried, and only genuinely changed context is appended at the resume
    point. A view derived from an accepted compaction keeps that audit
    immutable; either way this checkpoint adds no model call and never
    represents a compaction event. Fresh Turn, history, policy, tool and
    background witnesses still govern every restore. Each save is a new,
    newest archive (restore scans only the newest few) and retires every
    checkpoint it supersedes, so a conversation holds one live view.
    """
    if (task.get('_suspendedCompactionSaved') or not _resumable_root_turn(task)
            or _turn_completed(task)):
        return
    messages = task.get('messages')
    if not isinstance(messages, list):
        return
    receipt = task.get('_acceptedCompactionReceipt')
    compacted = isinstance(receipt, dict) and bool(receipt.get('summary', {}).get('accepted'))
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff
    if compacted:
        if not any(is_compaction_handoff(message) for message in messages):
            return
    elif not task.get('_messages_include_current_run'):
        # Without the loop's write-back these are the Turn's input messages.
        return
    from lib.tasks_pkg.manager._persist import _merge_tool_rounds
    from lib.agent_core.model_context_limits import MAX_ENTRY_BYTES, MAX_MESSAGES
    from lib.tasks_pkg.manager import task_user_id
    rounds = _merge_tool_rounds(task)
    boundary = _closed_working_view(messages, rounds, compacted=compacted)
    if boundary is None:
        return
    view, count = boundary
    if not view or len(view) > MAX_MESSAGES:
        return
    size = sum(len(part.encode('utf-8')) for part in json.JSONEncoder(
        ensure_ascii=False).iterencode(view))
    if size > MAX_ENTRY_BYTES:
        return
    # Suspension is the caller's completed boundary; it does not authorize
    # model dispatch, tool replay or mutation of the retained abort signal.
    witnessed_task = {**task, 'aborted': False}
    witness = compaction_resume_witness(witnessed_task, rounds=rounds, round_count=count)
    if witness is None:
        return
    from lib.agent_core.store import get_conversation_store
    owner = task_user_id(task)
    store = get_conversation_store()
    # This snapshot reuses an accepted handoff when there is one; no summary
    # cost is billed and a standalone checkpoint carries no summary at all.
    checkpoint_receipt = deepcopy(receipt) if compacted else {'status': 'completed'}
    witness['executionDigest'] = resume_execution_digest(
        task.get('content'), task.get('thinking'))
    checkpoint_receipt['resumeWitness'] = witness
    checkpoint_receipt['implementation'] = CHECKPOINT_IMPLEMENTATION
    checkpoint_receipt['trigger'] = 'checkpoint'
    archive = task.get('_suspendedCompactionArchiveId')
    if not archive:
        # The view is the payload; the archive keeps no second source copy.
        # It stays pending (never restorable) until its view is written.
        archive = store.archive_transcript(task['convId'], [], user_id=owner,
            trigger='checkpoint', task_id=task['id'],
            receipt=_checkpoint_receipt('pending'))
        if not archive:
            raise RuntimeError('Suspended working context archive unavailable')
        task['_suspendedCompactionArchiveId'] = archive
    store.update_archive_summary(archive, '', 0, len(view), user_id=owner,
        receipt=checkpoint_receipt, post_messages=deepcopy(view))
    task['_suspendedCompactionSaved'] = True
    _retire_superseded_checkpoints(store, task, owner, keep=archive)
    from lib.tasks_pkg.compaction._constants import archive_retention
    keep = archive_retention()
    if keep:
        store.prune_archives(task['convId'], keep, user_id=owner)


def _checkpoint_receipt(status):
    return {'status': status, 'implementation': CHECKPOINT_IMPLEMENTATION,
            'trigger': 'checkpoint'}


def _retire(store, archive, owner):
    """Empty one checkpoint; its metadata row follows ordinary retention."""
    store.update_archive_summary(archive, '', 0, 0, user_id=owner,
        receipt=_checkpoint_receipt('retired'), post_messages=[])


def _retire_superseded_checkpoints(store, task, owner, *, keep):
    """Retire every live checkpoint among the restore candidates but ``keep``.

    The newest checkpoint is the only one Continue can still need. Each save
    retires its predecessors while they are still among the newest
    candidates, so at most one view per conversation keeps its bytes.
    """
    stale = {task.get('_resumedCheckpointArchiveId')} - {None, keep}
    for metadata in store.list_compaction_archives(
            task['convId'], user_id=owner, limit=_MAX_ARCHIVE_CANDIDATES):
        if metadata.get('id') == keep or metadata.get('resultStatus') != 'completed':
            continue
        item = store.get_compaction_archive(
            task['convId'], metadata['id'], user_id=owner,
            include_messages=False, include_post_messages=False)
        receipt = ((item or {}).get('archive') or {}).get('receipt') or {}
        if receipt.get('implementation') == CHECKPOINT_IMPLEMENTATION:
            stale.add(metadata['id'])
    for archive in stale:
        _retire(store, archive, owner)


def retire_suspended_working_view(task):
    """Empty the checkpoint a completed Turn restored; nothing can resume it."""
    archive = task.get('_resumedCheckpointArchiveId')
    if (not archive or task.get('_suspendedCompactionRetired')
            or not _resumable_root_turn(task) or not _turn_completed(task)):
        return
    from lib.agent_core.store import get_conversation_store
    from lib.tasks_pkg.manager import task_user_id
    _retire(get_conversation_store(), archive, task_user_id(task))
    task['_suspendedCompactionRetired'] = True
