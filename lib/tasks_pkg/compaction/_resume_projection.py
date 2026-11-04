"""Reuse accepted archive projections on Continue after validating fresh sources.

The existing owner-scoped compaction archive is the only persisted snapshot.
Turn history remains authoritative. Policy/history/tool-prefix mismatch, missing
archives, unreproducible media, unconsumed inbox carriers and cancellation fall
back to full canonical replay. Each resume reports its outcome so the Turn
timeline can mark earlier compactions it did not reuse
(``report_compaction_resume``).
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import time

from lib.log import get_logger
from lib.tasks_pkg.model_replay import UNVERIFIED_INBOX_DELIVERY, policy_witness
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages

logger = get_logger(__name__)
_MAX_ARCHIVE_CANDIDATES = 8


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


def compaction_resume_witness(task):
    """Describe the exact source prefix only at a complete accepted L2 boundary.

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
    from lib.tasks_pkg.manager._persist import _merge_tool_rounds
    rounds = _merge_tool_rounds(task)
    # Media needs no witness field: restore proves each retained image against
    # current authority before the snapshot can replace fresh history.
    return {'version': 1, 'turnId': task['_turnId'],
            'policyDigest': policy_witness(config), 'historyDigest': history,
            'backgroundDigest': resume_background_digest(task.get('_bgCommandInjects')),
            'toolRoundCount': len(rounds),
            'toolPrefixDigest': _digest(_reconstruct_tool_call_messages(rounds) or [])}


def _witness_mismatch(witness, summary, config, rounds, history):
    """Name the first fresh source this Turn's archive no longer matches."""
    count = witness.get('toolRoundCount')
    if witness.get('policyDigest') != policy_witness(config):
        return 'settings_changed'
    if witness.get('historyDigest') != history:
        return 'history_changed'
    if summary.get('accepted') is not True or summary.get('complete') is not True:
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
            mismatch = _witness_mismatch(
                witness, receipt.get('summary') or {}, config, rounds, history)
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
            if not any(is_compaction_handoff(message) for message in post):
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
            task['_acceptedCompactionReceipt'] = deepcopy(receipt)
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


def preserve_suspended_compaction(task):
    """Save a closed suspended working view through the existing archive owner.

    Keep the original compaction audit immutable. This derived checkpoint adds
    no model call and never represents another compaction event. Fresh Turn,
    history, policy, tool and background witnesses still govern every restore.
    """
    if (not task.get('_turnId') or task.get('_suspendedCompactionSaved')
            or task.get('status') not in {'error', 'aborted', 'cancelled'}):
        return
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task) or task.get('_archiveScope'):
        return
    receipt = task.get('_acceptedCompactionReceipt')
    messages = task.get('messages')
    if not isinstance(receipt, dict) or not isinstance(messages, list):
        return
    if not receipt.get('summary', {}).get('accepted'):
        return
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff, has_open_tool_batch
    from lib.tasks_pkg.conv_message_builder._toolcalls import scan_replayable_tool_round_prefix
    from lib.tasks_pkg.manager._persist import _merge_tool_rounds
    from lib.agent_core.model_context_limits import MAX_ENTRY_BYTES, MAX_MESSAGES
    from lib.tasks_pkg.manager import task_user_id
    if has_open_tool_batch(messages) or not any(is_compaction_handoff(message) for message in messages):
        return
    rounds = _merge_tool_rounds(task)
    if scan_replayable_tool_round_prefix(rounds).blocked_position is not None:
        return
    if len(messages) > MAX_MESSAGES:
        return
    size = sum(len(part.encode('utf-8')) for part in json.JSONEncoder(
        ensure_ascii=False).iterencode(messages))
    if size > MAX_ENTRY_BYTES:
        return
    # Suspension is the caller's completed boundary; it does not authorize
    # model dispatch, tool replay or mutation of the retained abort signal.
    witnessed_task = {**task, 'aborted': False}
    witness = compaction_resume_witness(witnessed_task)
    if witness is None:
        return
    from lib.agent_core.store import get_conversation_store
    owner = task_user_id(task)
    store = get_conversation_store()
    checkpoint_receipt = deepcopy(receipt)
    witness['executionDigest'] = resume_execution_digest(
        task.get('content'), task.get('thinking'))
    checkpoint_receipt['resumeWitness'] = witness
    checkpoint_receipt['implementation'] = 'suspended_working_context'
    checkpoint_receipt['trigger'] = 'checkpoint'
    # This snapshot reuses the accepted handoff; no new summary cost is billed.
    archive = task.get('_suspendedCompactionArchiveId')
    if not archive:
        archive = store.archive_transcript(task['convId'], messages, user_id=owner,
            trigger='checkpoint', task_id=task['id'], receipt=checkpoint_receipt)
        if not archive:
            raise RuntimeError('Suspended working context archive unavailable')
        task['_suspendedCompactionArchiveId'] = archive
    store.update_archive_summary(archive, '', 0, len(messages), user_id=owner,
        receipt=checkpoint_receipt, post_messages=deepcopy(messages))
    from lib.tasks_pkg.compaction._constants import archive_retention
    keep = archive_retention()
    if keep:
        store.prune_archives(task['convId'], keep, user_id=owner)
    task['_suspendedCompactionSaved'] = True
