"""Recover a validated, previously sent model view across task boundaries.

Turn rows remain the sole transcript authority. Validated completed chat
continuations are durable derived state, one bounded checkpoint per lane.
Edits and incompatible permissions/sources force an observable cold rebuild;
process restarts and disposable cache eviction do not erase compaction work.
Preparation happens before Turn settlement; Sidecar publishes in its transaction.
"""
from __future__ import annotations

import hashlib
import json

from lib.log import get_logger
from lib.storage.model_continuation import ModelContinuationStore, settled_witness

logger = get_logger(__name__)
VERSION = 1
MAX_TURN_WITNESSES = 4096
_POLICY_FIELDS = (
    'model', 'provider_id', 'providerId', 'preset', 'thinkingDepth',

    'modelRef', 'preferredProviderId', 'routing', 'autopilot',
    'systemPrompt', 'systemPromptMode', 'systemPromptBlocks', 'responseFormat',
    'projectPath', 'projectEnabled', 'memoryEnabled', 'searchEnabled',
    'grantedToolPermissions', 'tools', 'enabledTools', 'disabledTools',
    'mcpServers', 'programmaticTools', 'multiAgent', 'contextExperimentFlags',
    'chatMode', 'interactionMode', 'thinkingEnabled', 'defaultThinkingDepth',
    'searchMode', 'fetchEnabled', 'codeExecEnabled', 'browserEnabled',
    'desktopEnabled', 'imageGenEnabled', 'humanGuidanceEnabled', 'schedulerEnabled',
    '_explicitToolSchemas', '_principalContext',
)



def _digest(value):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256()
    for part in encoder.iterencode(value):
        digest.update(part.encode('utf-8'))
    return digest.hexdigest()


def turn_witness(row):
    return settled_witness(row)


def policy_witness(config):
    from lib.context_experiment_flags import normalize_context_experiment_flags
    from lib.tool_round_replay import TOOL_REPLAY_PROJECTION_VERSION
    return _digest([VERSION, TOOL_REPLAY_PROJECTION_VERSION,
                    {key: config.get(key) for key in _POLICY_FIELDS},
                    normalize_context_experiment_flags(config)])


_ROUTE_FIELDS = frozenset({
    'model', 'provider_id', 'providerId', 'preset', 'thinkingDepth',
    'modelRef', 'preferredProviderId', 'routing',
})


def portable_policy_witness(config):
    """Route identity may change; policy, permissions and source scope may not."""
    return policy_witness({key: value for key, value in config.items()
                           if key not in _ROUTE_FIELDS})


def effort_policy_witness(config):
    """Same adaptive Opus and requested policy; only enabled effort may differ.

    Effort changes upstream cache rendering, not validity of prior thinking.
    Keep the full continuation rather than treating this as cross-model
    migration. Off/mode changes, other families and old checkpoints retain
    the conservative rebuild path. One fixed-size witness adds no payload copy.
    """
    from lib.model_info import is_claude_opus_47
    if (not is_claude_opus_47(config.get('model') or '')
            or config.get('thinkingEnabled') is False
            or (config.get('thinkingDepth') or config.get('defaultThinkingDepth')) == 'off'):
        return ''
    return policy_witness({key: value for key, value in config.items()
                           if key not in ('thinkingDepth', 'defaultThinkingDepth')})


def _portable_messages(messages):
    """Detach a text-only continuation; opaque/provider-bound state cannot migrate.

    Refuse multimodal history rather than silently losing evidence. Composer
    carriers are rebuilt for the destination model; user policy remains guarded
    by portable_policy_witness. Ordinary call/result text keeps its exact bytes.
    """
    import copy
    projected = []
    for message in messages:
        if message.get('_contextComposer'):
            continue
        if not isinstance(message.get('content', ''), (str, type(None))):
            return None
        if any(message.get(key) for key in (
                'reasoning_content', 'reasoning', 'thinking',
                '_responses_items', '_responsesOutputItems', '_modelImageSources')):
            return None
        # Unknown private provider state is not portable. These flags describe
        # local text provenance (including L2 history-carrier metadata).
        allowed_private = {
            '_isMeta', '_isCompactionSummary', '_compactionArchiveId',
            '_estimatedPromptTokens', '_attachmentTextChars',
            '_retainedUserHistory', '_retainedUserHistoryVersion', '_isObjectiveAnchor',
            '_isVuDirective', '_isVirtualUser', '_isInboxInject',
            '_containsHumanSteer',

            '_authoredUserText',
            '_compactionHandoff', '_turnId', '_sourceActor', '_initiator',
        }
        if any(str(key).startswith('_') and key not in allowed_private for key in message):
            return None
        projected.append(copy.deepcopy(message))
    return projected if projected and projected[-1].get('role') == 'assistant' else None


def _publication_status(task, reason):
    task['_modelReplayPublishStatus'] = reason
    logger.info('[ModelReplay] conv=%s checkpoint=%s',
                str(task.get('convId') or '')[:8], reason)


def _has_resume_data(config):
    return any(config.get(key) for key in (
        'checkpointToolRounds', 'contentPrefix', 'resumePrefill', '_resumeSource',
    ))


def _supports_completed_publication(config):
    return not any(config.get(key) for key in (
        'flowId', 'flowBuiltin', 'flowDefinition', 'toolHistory',
    ))


def _supports_prefix_restore(config):
    # Recovering the current Turn is not restoring a completed historical
    # prefix. After validated recovery actually completes, publication is a
    # different admission decision and must not inherit this exclusion.
    return _supports_completed_publication(config) and not _has_resume_data(config)


def restore_prefix(rows, config, *, conversation_id, user_id, validated_context=None):
    """Return (retained model messages, number of covered Turn rows)."""
    config.pop('_goalWorkerState', None)
    config.pop('_modelReplayBase', None)
    config.pop('_modelReplayHistoryProof', None)
    config.pop('_modelReplayProofEligible', None)
    config.pop('_modelReplayToolLatch', None)
    config['_modelReplayStatus'] = 'cold'
    if not rows:
        config['_modelReplayStatus'] = 'empty_history'
        return [], 0
    if validated_context is None and len(rows) > MAX_TURN_WITNESSES:
        config['_modelReplayStatus'] = 'over_budget'
        return [], 0
    witnesses = ([turn_witness(row) for row in rows[:-1]]
                 if validated_context is None else [])
    config['_modelReplayBase'] = witnesses
    if validated_context is not None:
        config['_modelReplayHistoryProof'] = validated_context['historyProof']
        config['_modelReplayProofEligible'] = bool(validated_context.get('proofEligible'))
    # Resume cannot reuse a completed-turn prefix blindly, but it still needs
    # fresh history witnesses to validate an accepted compaction archive.
    if not _supports_prefix_restore(config):
        config['_modelReplayStatus'] = 'unsupported_mode'
        return [], 0
    target = rows[-1]
    if target.get('projection', {}).get('content') or target.get('projection', {}).get('toolRounds'):
        config['_modelReplayStatus'] = 'resume_target'
        return [], 0
    config['_modelReplayPolicy'] = policy_witness(config)
    config['_modelReplayPortablePolicy'] = portable_policy_witness(config)
    config['_modelReplayEffortPolicy'] = effort_policy_witness(config)
    try:
        cached = (validated_context.get('continuation') if validated_context is not None
                  else ModelContinuationStore().get(user_id=user_id, conversation_id=conversation_id,
                                                   lane_id=target.get('laneId') or 'main'))
        if not isinstance(cached, dict):
            reason = (validated_context or {}).get('continuationStatus')
            config['_modelReplayStatus'] = (reason if reason in {
                'history_changed', 'terminal_changed', 'legacy_proof',
                'checkpoint_not_before_target', 'replay_disabled',
            } else 'checkpoint_missing')
            return [], 0
        if 'historyProof' in cached:
            proof = cached['historyProof']
            if (not isinstance(proof, dict) or type(proof.get('version')) is not int
                    or proof['version'] != 2):
                config['_modelReplayStatus'] = 'legacy_proof'
                return [], 0
            if validated_context is None:
                # One terminal witness cannot prove all earlier Turns.
                config['_modelReplayStatus'] = 'proof_requires_authority'
                return [], 0
        same_policy = cached.get('policy') == policy_witness(config)
        effort_policy = config['_modelReplayEffortPolicy']
        same_effort_policy = bool(effort_policy and cached.get('effortPolicy') == effort_policy)
        same_prefix_policy = same_policy or same_effort_policy
        portable = (not same_prefix_policy and cached.get('portablePolicy')
                    == portable_policy_witness(config))
        if not same_prefix_policy and not portable:
            config['_modelReplayStatus'] = 'policy_changed'
            return [], 0
        prior = cached.get('turns') or []
        legacy_prefix = validated_context is not None and 'historyProof' not in cached
        if legacy_prefix:
            witnesses = [turn_witness(row) for row in rows[:len(prior)]]
        if (validated_context is None or legacy_prefix) and (not prior or witnesses[:len(prior)] != prior):
            config['_modelReplayStatus'] = 'authority_changed'
            return [], 0
        messages = cached.get('messages')
        if not isinstance(messages, list) or not messages or messages[-1].get('role') != 'assistant':
            config['_modelReplayStatus'] = 'invalid_checkpoint'
            return [], 0
        if portable:
            messages = _portable_messages(messages)
            if messages is None:
                config['_modelReplayStatus'] = 'nonportable_checkpoint'
                return [], 0
        from lib.knowledge import read_source_path
        for message in messages:
            for source in message.get('_modelImageSources') or []:
                if source.startswith('tofu-media:') and read_source_path(
                        source[len('tofu-media:'):], user_id=user_id) is None:
                    config['_modelReplayStatus'] = 'media_unavailable'
                    return [], 0
        # Attachment ownership/readiness is rechecked even though the text
        # projection remains pinned. A removed source must never be recovered
        # solely from an old cache file.
        from lib.media_attachments import resolve_client_refs
        for row in rows[:len(prior)]:
            refs = (row.get('projection') or {}).get('attachments') or []
            if refs and resolve_client_refs(refs, user_id=user_id) != refs:
                config['_modelReplayStatus'] = 'media_changed'
                return [], 0
        # MCP declarations have their own conversation-settings authority.
        # A replay body (including legacy mcpWire) cannot overwrite or resurrect
        # them after reset; registry assembly reads them even on cold replay.
        if same_prefix_policy and isinstance(cached.get('toolLatch'), dict):
            config['_modelReplayToolLatch'] = cached['toolLatch']
        if config.get('autopilot') and same_prefix_policy and isinstance(cached.get('goalState'), dict):
            from copy import deepcopy
            config['_goalWorkerState'] = deepcopy(cached['goalState'])
        config['_modelReplayStatus'] = (
            'restored_portable' if portable else
            'restored_effort' if not same_policy and same_effort_policy else 'restored')
        return messages, len(prior) if validated_context is None or legacy_prefix else 0
    except Exception as exc:
        config['_modelReplayStatus'] = 'unavailable'
        logger.warning('[ModelReplay] cold rebuild: %s', type(exc).__name__)
        return [], 0


def prepare_completed_task(task):
    """Prepare a bounded candidate; only Sidecar may mint its settled witness."""
    config = task.get('config') or {}
    base = config.get('_modelReplayBase')
    owner = task.get('_userId')
    conv_id, turn_id = task.get('convId'), task.get('_turnId')
    checks = (
        ('missing_base', not isinstance(base, list)),
        ('missing_identity', not owner or not conv_id or not turn_id),
        ('unsupported_mode', not _supports_completed_publication(config)),
        ('unverified_resume_context', _has_resume_data(config)
         and task.get('_resumeContextHydrated') is not True),
        ('failed_or_aborted', task.get('error') or task.get('aborted')),
        ('steered_history', task.get('_userSteerInjects')),
        ('multi_turn_run', task.get('_turnVisibleRunTurnIds')),
    )
    for reason, blocked in checks:
        if blocked:
            _publication_status(task, reason)
            return
    try:
        messages = task.get('messages') or []
        if not messages or messages[-1].get('role') != 'assistant':
            _publication_status(task, 'missing_final_model_message')
            return
        if (_has_resume_data(config)
                and messages[-1].get('content') != task.get('content', '')):
            # contentPrefix is display bookkeeping, not proof that those
            # tokens reached the model. Never publish a body omitting it.
            _publication_status(task, 'resume_content_unrepresented')
            return
        # A length/incomplete terminal may retain a complete text boundary,
        # but an unfinished tool batch must never become a reusable prefix.
        if messages[-1].get('tool_calls') or messages[-1].get('function_call'):
            _publication_status(task, 'unfinished_tool_batch')
            return
        if config.get('model') and task.get('model') != config['model']:
            _publication_status(task, 'model_changed_during_run')
            return
        from lib.storage.model_continuation import encode_projection
        candidate = {'policy': config.get('_modelReplayPolicy') or policy_witness(config),
                     'portablePolicy': (config.get('_modelReplayPortablePolicy')
                                        or portable_policy_witness(config)),
                     'effortPolicy': config.get('_modelReplayEffortPolicy',
                                              effort_policy_witness(config)),
                     'turns': base + [[turn_id, '0' * 64]], 'messages': messages,
                     'toolLatch': task.get('_toolOrchestrationPromptLatch')}
        history_proof = config.get('_modelReplayHistoryProof')
        if history_proof:
            from lib.tasks_pkg.message_semantics import has_model_media
            if config.get('_modelReplayProofEligible') and not any(
                    has_model_media(message) for message in messages):
                candidate['historyProof'] = history_proof
            else:
                # Media keeps its existing durable replay capability. Obtain
                # full content witnesses once at publication, not on every
                # ordinary text read, and fence edits since initial selection.
                full = ModelContinuationStore().read_context(
                    user_id=owner, conversation_id=conv_id,
                    turn_id=turn_id, replay=False)
                if (not isinstance(full, dict) or not full.get('supported')
                        or full.get('historyProof') != history_proof):
                    _publication_status(task, 'authority_changed')
                    return
                candidate['turns'] = [turn_witness(row) for row in full['turns'][:-1]] + candidate['turns'][-1:]
        encoded = encode_projection(candidate)
        if encoded is None:
            _publication_status(task, 'invalid_or_over_budget')
            return
        _publication_status(task, 'prepared')
        # Detach mutable executor messages before command serialization/retries.
        return {'projection': json.loads(encoded), 'content': task.get('content', '')}
    except Exception as exc:
        logger.warning('[ModelReplay] checkpoint unavailable: %s', type(exc).__name__)
        _publication_status(task, 'unavailable')


def publish_completed_task(task):
    """Explicit settled import compatibility seam; live lifecycle never calls this."""
    candidate = prepare_completed_task(task)
    if candidate is None:
        return
    try:
        from lib.storage import get_storage_client
        target = get_storage_client().query('turn.get', {
            'conversation_id': task['convId'], 'turn_id': task['_turnId'],
            'user_id': task['_userId']}, deadline=2.0)
        if not target or target.get('status') != 'completed':
            _publication_status(task, 'turn_not_completed')
            return
        if (target.get('projection') or {}).get('content') != candidate['content']:
            _publication_status(task, 'settled_content_changed')
            return
        projection = candidate['projection']
        projection['turns'][-1] = turn_witness(target)
        stored = ModelContinuationStore().put(
            user_id=task['_userId'], conversation_id=task['convId'],
            lane_id=target.get('laneId') or 'main', projection=projection)
        _publication_status(task, 'stored' if stored else 'rejected')
    except Exception:
        _publication_status(task, 'unavailable')
