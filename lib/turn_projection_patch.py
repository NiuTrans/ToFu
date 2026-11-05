"""Compact, replay-safe patches for v2 turn projections.

Responsibility: derive the transport delta between two JSON-compatible turn
projections.  Storage remains authoritative for the full document; attempt
events carry these patches so a growing tool timeline is not retransmitted on
every frame.  The wire contract is mirrored by
``frontend/src/core/projection-patch.ts`` and documented in
``docs/contracts/API_CONTRACT.md`` §2.5.

It also owns the per-turn ``contextSnapshot`` block: normalization of the
legacy ``_ctx`` overlay and derivation of the block from the task config for
inputs created without a browser (``with_turn_context_snapshot``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from lib.conversation_sync.settled_rows import (
    SettledDict,
    SettledList,
    memo_get,
    memo_put,
    settle_unless_same,
)
from lib.plan_contract import plan_execution_document, proposed_plan_document
from lib.turn_activity_timeline import normalize_activity_timeline


PROJECTION_PATCH_VERSION = 1


class ProjectionPatchError(ValueError):
    """A projection patch is malformed or incompatible with its base."""

# Identity belongs to the turn/attempt columns and the client runtime, never
# inside ``projection`` itself.  Keep this list beside the projection wire
# helpers so every persistence path (lifecycle edits, manual compaction,
# future imports) cleans the exact same keys.
_PROJECTION_IDENTITY_KEYS = frozenset({
    'turnId', 'attemptId', '_turnId', '_attemptId', '_msgId', '_taskId',
    'activeTaskId', 'role', '_turnActor', '_turnKind', '_turnLaneId',
    '_turnStatus', '_turnSettlement', '_projectionRevision',
    '_commandPending', 'branches',
})

# Public projection vocabulary.  Anything outside this set either belongs to
# turn identity/runtime state (handled above) or is a legacy message overlay
# that must be mapped into one of the typed documents below.  Keeping this
# allow-list beside ``normalize_projection_document`` gives every authority
# writer and public reader one executable definition of the backend/frontend
# ownership boundary.
PUBLIC_PROJECTION_FIELDS = frozenset({
    'content', 'thinking', 'toolRounds', 'segments', 'usage', 'usageComplete', 'apiRounds',
    'cost',
    'lastRoundUsage', 'model', 'preset', 'providerId', 'routeSnapshot',
    'thinkingDepth',
    'modifiedFiles', 'modifiedFileList', 'modifiedFilesTruncated', 'fileChanges', 'todoState',
    'waitingOn',
    'fallbackModel', 'fallbackFrom', 'fallbackReason', 'fallbackKind',
    'translatedContent', 'originalContent', 'translation', 'timestamp',
    'images', 'attachments', 'videos', 'pdfTexts', 'convRefs', 'replyQuotes',
    '_branchLanes',
    'orchestration', 'provenance', '_inboxInjects', '_peerInjects',
    '_userSteerInjects', '_bgCommandInjects', 'backgroundCommand',
    '_stallNudges', 'origin', 'contextSnapshot',
    'compaction', 'imageGeneration', 'proposedPlan', 'planExecution', 'usageRecovery',
    'activityTimeline', 'timingTrace', 'rolledBack',
})

# Rewind history lane: each entry preserves one interrupted attempt's
# discarded terminal text so a resume never silently erases rendered
# history. Bounded: only the newest entries survive a resume chain.
_ROLLED_BACK_FIELDS = frozenset({
    'blockId', 'attemptId', 'at', 'content', 'thinking',
})
_ROLLED_BACK_MAX_ENTRIES = 4

# One vocabulary with the writers (lib/turn_initiation.py): a stamped
# initiator missing here used to be rewritten to ``human`` on persistence.
from lib.turn_initiation import VALID_INITIATORS as _VALID_INITIATORS  # noqa: E402
_CHANNEL_ORIGIN_FIELDS = frozenset({'channel', 'thread', 'eventIds', 'title'})
_BRAIN_FIELDS = frozenset({
    'epicId', 'epicTitle', 'originatorConv', 'originatorTitle', 'route',
    'method', 'answered',
})
_TRANSLATION_FIELDS = frozenset({
    'status', 'model', 'error', 'taskId', 'statusMessage', 'statusKind',
    'partial', 'partialByRound', 'sendFailure', 'skippedReason',
})
_ORIGIN_FIELDS = frozenset({
    'blockId', 'initiator', 'sourceConversationId', 'peerHuman', 'brain',
    'autopilotRunId', 'boardTaskId', 'scheduledTaskId', 'channel', 'forwardedFrom',
})
_FORWARDED_FROM_FIELDS = frozenset({'conversationId', 'turnId', 'steerId', 'instructionId'})
_COMPACTION_FIELDS = frozenset({
    'blockId', 'archiveId', 'conversationId', 'trigger', 'timestamp',
    'tokensBefore', 'tokensAfter', 'messagesBefore', 'messagesAfter',
    'reductionPercent', 'foldedToolRounds', 'estimatedPromptTokens',
})
_IMAGE_GENERATION_FIELDS = frozenset({
    'blockId', 'mode', 'status', 'results', 'error',
})
_IMAGE_RESULT_FIELDS = frozenset({
    'ok', 'prompt', 'model', 'providerId', 'aspectRatio', 'resolution',
    'imageUrl', 'remoteImageUrl', 'fileSize', 'elapsedSeconds', 'responseText',
    'error', 'errorType',
})


def _copy_named_mapping(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if key in fields}


def _legacy_initiator(projection: Mapping[str, Any]) -> str:
    declared = projection.get('_initiator')
    if declared in _VALID_INITIATORS:
        return str(declared)
    if projection.get('_peerMessage'):
        return 'operator' if projection.get('_peerHuman') else 'peer'
    if projection.get('_isVirtualUser') or projection.get('_autopilotRunId'):
        return 'autopilot'
    if projection.get('_proactive'):
        return 'proactive'
    if projection.get('_timer'):
        return 'timer'
    if projection.get('_brainDispatch'):
        return 'brain'
    if projection.get('_swarmAutoContinue'):
        return 'swarm'
    return 'human'


def _normalize_origin(projection: Mapping[str, Any]) -> dict[str, Any] | None:
    origin = _copy_named_mapping(projection.get('origin'), _ORIGIN_FIELDS)
    brain = _copy_named_mapping(origin.get('brain'), _BRAIN_FIELDS)
    legacy_brain = _copy_named_mapping(projection.get('_brainEpic'), _BRAIN_FIELDS)
    if legacy_brain:
        brain.update(legacy_brain)
    if brain:
        origin['brain'] = brain
    else:
        origin.pop('brain', None)
    channel = _copy_named_mapping(origin.get('channel'), _CHANNEL_ORIGIN_FIELDS)
    if channel.get('channel') and channel.get('thread'):
        origin['channel'] = {
            key: ([str(item) for item in value] if isinstance(value, list) else [])
            if key == 'eventIds' else str(value)
            for key, value in channel.items()}
    else:
        origin.pop('channel', None)
    # Router forwards keep their provenance pointer; anything malformed is not
    # a forward. require_human_turn verifies the pointer before any authority.
    forwarded = _copy_named_mapping(origin.pop('forwardedFrom', None), _FORWARDED_FROM_FIELDS)
    if (all(isinstance(value, str) and value for value in forwarded.values())
            and {'conversationId', 'turnId'} <= set(forwarded)):
        origin['forwardedFrom'] = forwarded

    initiator = origin.get('initiator')
    if initiator not in _VALID_INITIATORS:
        initiator = _legacy_initiator(projection)
    has_legacy_origin = any(projection.get(key) is not None for key in (
        '_initiator', '_peerMessage', '_peerHuman', '_fromConv',
        '_brainDispatch', '_brainEpic', '_isVirtualUser', '_autopilotRunId',
        '_proactive', '_proactiveTaskId', '_timer', '_swarmAutoContinue',
        '_boardTaskId', 'boardTaskId',
    ))
    if initiator == 'human' and not origin and not has_legacy_origin:
        return None
    origin['blockId'] = str(origin.get('blockId') or 'origin')
    origin['initiator'] = str(initiator)

    source_conversation_id = (
        origin.get('sourceConversationId') or projection.get('_fromConv')
    )
    if source_conversation_id:
        origin['sourceConversationId'] = str(source_conversation_id)
    if projection.get('_peerHuman') is not None:
        origin['peerHuman'] = bool(projection.get('_peerHuman'))
    for target, legacy in (
        ('autopilotRunId', '_autopilotRunId'),
        ('boardTaskId', '_boardTaskId'),
        ('scheduledTaskId', '_proactiveTaskId'),
    ):
        value = origin.get(target) or projection.get(legacy)
        if target == 'boardTaskId':
            value = value or projection.get('boardTaskId')
        if value:
            origin[target] = str(value)
    return origin


def _normalize_translation(projection: Mapping[str, Any]) -> dict[str, Any] | None:
    translation = _copy_named_mapping(
        projection.get('translation'), _TRANSLATION_FIELDS,
    )
    legacy_values = {
        'model': projection.get('_translateModel'),
        'error': projection.get('_translateError'),
        'taskId': projection.get('_translateTaskId'),
        'statusMessage': projection.get('_translateStatus'),
        'statusKind': projection.get('_translateStatusKind'),
        'partial': projection.get('_translatePartial'),
        'partialByRound': projection.get('_translatePartialByRound'),
        'sendFailure': projection.get('_translateFailed'),
        'skippedReason': projection.get('_translateSkippedReason'),
    }
    for key, value in legacy_values.items():
        if value is not None:
            translation[key] = value

    done_present = '_translateDone' in projection
    done = projection.get('_translateDone')
    if translation.get('error') is not None:
        translation['status'] = 'failed'
    elif translation.get('skippedReason'):
        translation['status'] = 'skipped'
    elif done is True:
        translation['status'] = 'completed'
    elif done is False or translation.get('taskId') or translation.get('partial'):
        translation['status'] = 'pending'
    elif (projection.get('translatedContent') is not None
          or projection.get('originalContent') is not None):
        translation.setdefault('status', 'completed')

    if not translation and not done_present:
        return None
    status = translation.get('status')
    if status not in {'pending', 'completed', 'skipped', 'failed'}:
        translation.pop('status', None)
    return translation or None


def _normalize_context(projection: Mapping[str, Any]) -> dict[str, Any] | None:
    current = projection.get('contextSnapshot')
    if isinstance(current, Mapping) and isinstance(current.get('snapshot'), Mapping):
        return {
            'blockId': str(current.get('blockId') or 'turn-context'),
            'snapshot': dict(current['snapshot']),
        }
    legacy = projection.get('_ctx')
    if isinstance(legacy, Mapping):
        return {'blockId': 'turn-context', 'snapshot': dict(legacy)}
    return None


def build_turn_ctx_snapshot(task_config: Mapping[str, Any]) -> dict[str, Any] | None:
    """Derive the per-turn context note from the config an input runs with.

    The browser captures this snapshot from its live composer
    (``buildTurnCtxSnapshot`` in ``frontend/src/runtime/sections/info-rail.js``).
    Inputs created without a browser (scheduler and background-command
    wakes, peer messages, Feishu, plan execution) get the same shape from
    their task config instead. Mirrors the chip mapping of
    ``_collectTools()``/``_short()`` in info-rail.js. ``depth``/``modes`` stay
    empty: the done frame's fact card (actualModel/actualDepth/actualModes)
    settles them. MCP/desktop chips are client-only state and stay absent —
    the note records what THIS config guarantees. The always-on browser is a
    default tool and, like the scheduler, never gets a chip.
    """
    tools = []
    search_mode = str(task_config.get('searchMode') or 'off')
    if search_mode != 'off':
        tools.append({
            'label': 'Search' if search_mode == 'single' else 'Search ×N',
            'tone': 'search',
        })
    for key, label, tone in (
        ('fetchEnabled', 'Fetch', 'net'),
        ('codeExecEnabled', 'Code Exec', 'code'),
        ('memoryEnabled', 'Memory', 'ai'),
        ('imageGenEnabled', 'Image Gen', 'ai'),
        ('autoTranslate', 'Translate', 'ai'),
    ):
        if task_config.get(key):
            tools.append({'label': label, 'tone': tone})
    roots = []
    project_path = str(task_config.get('projectPath') or '')
    if project_path:
        parts = [p for p in project_path.split('/') if p]
        short = '/'.join(parts[-2:]) if len(parts) > 2 else project_path
        read_only = task_config.get('readOnlyPaths')
        ro = isinstance(read_only, (list, tuple)) and project_path in read_only
        roots.append({'short': short, 'path': project_path, 'ro': ro})
    model = str(task_config.get('model') or '')
    if not tools and not roots and not model:
        return None
    return {
        'roots': roots,
        'tools': tools,
        'modes': [],
        'model': model,
        'depth': '',
    }


def with_turn_context_snapshot(
    projection: Any, config: Mapping[str, Any] | None,
) -> Any:
    """Give an input projection the context block it lacks; a supplied one wins."""
    if not isinstance(projection, Mapping) or _normalize_context(projection) is not None:
        return projection
    snapshot = build_turn_ctx_snapshot(config or {})
    if snapshot is None:
        return projection
    return {
        **projection,
        'contextSnapshot': {'blockId': 'turn-context', 'snapshot': snapshot},
    }


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _normalize_compaction(projection: Mapping[str, Any]) -> dict[str, Any] | None:
    compaction = _copy_named_mapping(
        projection.get('compaction'), _COMPACTION_FIELDS,
    )
    markers = projection.get('_compactions')
    marker = markers[0] if isinstance(markers, list) and markers else {}
    marker = marker if isinstance(marker, Mapping) else {}
    is_legacy = projection.get('_isCompactionSummary') is True
    if not compaction and not is_legacy:
        return None
    compaction['blockId'] = str(compaction.get('blockId') or 'compaction')
    text_fields = {
        'archiveId': projection.get('_compactionArchiveId') or marker.get('archiveId'),
        'conversationId': marker.get('convId'),
        'trigger': marker.get('trigger'),
    }
    for key, value in text_fields.items():
        if value is not None and value != '':
            compaction[key] = str(value)
    number_fields = {
        'timestamp': marker.get('ts'),
        'tokensBefore': marker.get('tokensBefore'),
        'tokensAfter': marker.get('tokensAfter'),
        'messagesBefore': marker.get('msgsBefore'),
        'messagesAfter': marker.get('msgsAfter'),
        'reductionPercent': marker.get('reductionPct'),
        'foldedToolRounds': (
            marker.get('foldedToolRounds') or projection.get('_foldedToolRounds')
        ),
        'estimatedPromptTokens': projection.get('_estimatedPromptTokens'),
    }
    for key, value in number_fields.items():
        normalized = _positive_int(value)
        if normalized is not None:
            compaction[key] = normalized
    return compaction


def _normalize_image_result(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result = _copy_named_mapping(value, _IMAGE_RESULT_FIELDS)
    aliases = {
        'providerId': 'provider_id',
        'aspectRatio': 'aspect_ratio',
        'imageUrl': 'image_url',
        'remoteImageUrl': 'remote_image_url',
        'fileSize': 'file_size',
        'elapsedSeconds': 'elapsed',
        'responseText': 'response_text',
        'errorType': 'error_type',
    }
    for target, legacy in aliases.items():
        if target not in result and value.get(legacy) is not None:
            result[target] = value.get(legacy)
    result['ok'] = bool(result.get('ok', value.get('image_url') or value.get('imageUrl')))
    result['prompt'] = str(result.get('prompt') or '')
    result['model'] = str(result.get('model') or '')
    if result.get('elapsedSeconds') is not None:
        try:
            result['elapsedSeconds'] = max(0.0, float(result['elapsedSeconds']))
        except (TypeError, ValueError):
            result.pop('elapsedSeconds', None)
    if result.get('fileSize') is not None:
        normalized_size = _positive_int(result.get('fileSize'))
        if normalized_size is None:
            result.pop('fileSize', None)
        else:
            result['fileSize'] = normalized_size
    return result


def _normalize_image_generation(
    projection: Mapping[str, Any],
) -> dict[str, Any] | None:
    current = _copy_named_mapping(
        projection.get('imageGeneration'), _IMAGE_GENERATION_FIELDS,
    )
    raw_results = current.get('results')
    legacy_single = projection.get('_igResult')
    legacy_batch = projection.get('_igResults')
    if not isinstance(raw_results, list):
        if isinstance(legacy_batch, list):
            raw_results = legacy_batch
        elif isinstance(legacy_single, Mapping):
            raw_results = [legacy_single]
        else:
            raw_results = []
    results = [
        normalized for item in raw_results
        if (normalized := _normalize_image_result(item)) is not None
    ]
    legacy_error = projection.get('_igError')
    has_legacy = bool(
        results or projection.get('_isImageGen') or projection.get('_isImageEdit')
        or legacy_error is not None or projection.get('_igBatchPending')
    )
    if not current and not has_legacy:
        return None
    mode = current.get('mode')
    if mode not in {'generate', 'edit', 'batch'}:
        mode = 'batch' if isinstance(legacy_batch, list) else (
            'edit' if projection.get('_isImageEdit') else 'generate'
        )
    status = current.get('status')
    if status not in {'running', 'completed', 'failed', 'cancelled'}:
        if projection.get('_igBatchPending'):
            status = 'running'
        elif legacy_error is not None or (results and not any(
            result.get('ok') for result in results
        )):
            status = 'failed'
        else:
            status = 'completed'
    return {
        'blockId': str(current.get('blockId') or 'image-generation'),
        'mode': mode,
        'status': status,
        'results': results,
        **({'error': current.get('error')} if current.get('error') is not None else {}),
        **({'error': legacy_error} if legacy_error is not None else {}),
    }


def _normalize_tool_rounds(value: Any) -> Any:
    """Remove the internal pending-results sentinel from public rounds.

    Executors use ``results=None`` to distinguish a running tool from a
    completed tool that returned no rows.  ``TurnToolRound.results`` is an
    optional array on the public wire, so publishing that internal sentinel
    makes an otherwise valid live snapshot fail its generated contract.  An
    omitted key preserves the pending meaning; a real list, including an
    empty completed result, remains byte-for-byte intact.
    """
    if not isinstance(value, list):
        return value
    normalized: list[Any] = []
    for item in value:
        if not isinstance(item, Mapping):
            normalized.append(item)
            continue
        if type(item) is SettledDict:
            public = memo_get(item, _PUBLIC_TOOL_ROUND_KEY)
            if public is None:
                public = memo_put(item, _PUBLIC_TOOL_ROUND_KEY, settle_unless_same(
                    item, _public_tool_round(item)))
            normalized.append(public)
            continue
        normalized.append(_public_tool_round(item))
    return normalized


_PUBLIC_TOOL_ROUND_KEY = ('public-tool-round',)


def _public_tool_round(item: Mapping[str, Any]) -> dict[str, Any]:
    from lib.background_command_projection import normalize_handoff_round
    round_record = dict(item)
    if round_record.get('results') is None:
        round_record.pop('results', None)
    return normalize_handoff_round(round_record)


def _normalize_rolled_back(value: Any) -> list[dict[str, Any]] | None:
    """Fail-closed repair of the rolledBack lane.

    Entries with neither lane text are dropped entirely; scalar fields are
    coerced to their canonical types; unknown keys are stripped.
    """
    if not isinstance(value, list):
        return None
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        entry = _copy_named_mapping(item, _ROLLED_BACK_FIELDS)
        content = entry.get('content')
        if not isinstance(content, str) or not content:
            entry.pop('content', None)
        thinking = entry.get('thinking')
        if not isinstance(thinking, str) or not thinking:
            entry.pop('thinking', None)
        if 'content' not in entry and 'thinking' not in entry:
            continue
        entry['blockId'] = str(entry.get('blockId') or 'rolled-back')
        if entry.get('attemptId') is not None:
            entry['attemptId'] = str(entry['attemptId'])
        else:
            entry.pop('attemptId', None)
        at = entry.get('at')
        if (isinstance(at, bool) or not isinstance(at, int) or at <= 0):
            entry.pop('at', None)
        normalized.append(entry)
    if not normalized:
        return None
    return normalized[-_ROLLED_BACK_MAX_ENTRIES:]


def normalize_projection_document(raw: Any) -> dict[str, Any]:
    """Return the canonical JSON document stored in a turn projection.

    Callers often start from the legacy-compatible derived message shape,
    which intentionally overlays identity/runtime fields on top of the stored
    projection.  Persisting that overlay would create two disagreeing copies
    of turn identity.  This boundary strips it and normalizes text-only input.
    """
    if isinstance(raw, str):
        return {'content': raw}
    if not isinstance(raw, Mapping):
        return {'content': ''}
    source = dict(raw)
    result = dict(source)
    from lib.background_command_projection import bound_projection_completion_previews
    bound_projection_completion_previews(result)
    for identity_key in _PROJECTION_IDENTITY_KEYS:
        result.pop(identity_key, None)
    if result.get('providerId') is None and result.get('provider_id') is not None:
        result['providerId'] = result.get('provider_id')
    result.pop('provider_id', None)
    if 'content' not in result and 'text' in result:
        result['content'] = result.get('text') or ''
    result.setdefault('content', '')
    # Task carriers use None before any billed response. The public contract
    # represents unknown usage by absence, never null or invented zero tokens.
    # Apply at the shared authority boundary for root, child and old rows alike.
    if result.get('usage') is None:
        result.pop('usage', None)
    if not isinstance(result.get('routeSnapshot'), Mapping):
        model_id = str(result.get('model') or '').strip()
        provider_id = str(result.get('providerId') or '').strip()
        if model_id or provider_id:
            from lib.model_routing import legacy_route_snapshot
            result['routeSnapshot'] = legacy_route_snapshot(
                model_id=model_id,
                provider_id=provider_id,
                route_id='',
            )
    if 'toolRounds' in result:
        result['toolRounds'] = _normalize_tool_rounds(result['toolRounds'])

    if isinstance(result.get('segments'), list):
        # Segment-owned rounds override toolRounds; normalize both read shapes.
        result['segments'] = [
            segment if (type(segment) in _DICT_KINDS and '_round' not in segment)
            else {**segment, '_round': _normalize_tool_rounds([segment['_round']])[0]}
            if isinstance(segment, Mapping) and isinstance(segment.get('_round'), Mapping)
            else segment for segment in result['segments']
        ]
    origin = _normalize_origin(source)
    translation = _normalize_translation(source)
    context_snapshot = _normalize_context(source)
    compaction = _normalize_compaction(source)
    image_generation = _normalize_image_generation(source)
    activity_timeline = normalize_activity_timeline(
        source.get('activityTimeline'))
    # A tag in arbitrary assistant prose is not execution authority. The Plan
    # task terminal boundary explicitly mints ``proposedPlan``; normalization
    # only verifies such a sidecar against the visible tagged transcript.
    raw_proposed_plan = source.get('proposedPlan')
    proposed_plan = (
        proposed_plan_document(raw_proposed_plan, content=source.get('content'))
        if isinstance(raw_proposed_plan, Mapping) else None
    )
    plan_execution = plan_execution_document(source.get('planExecution'))
    if origin is not None:
        result['origin'] = origin
    if translation is not None:
        result['translation'] = translation
    if context_snapshot is not None:
        result['contextSnapshot'] = context_snapshot
    if compaction is not None:
        result['compaction'] = compaction
    if image_generation is not None:
        result['imageGeneration'] = image_generation
    if activity_timeline is not None:
        result['activityTimeline'] = activity_timeline
    else:
        result.pop('activityTimeline', None)
    if proposed_plan is not None:
        result['proposedPlan'] = proposed_plan
    else:
        result.pop('proposedPlan', None)
    if plan_execution is not None:
        result['planExecution'] = plan_execution
    else:
        result.pop('planExecution', None)
    rolled_back = _normalize_rolled_back(source.get('rolledBack'))
    if rolled_back is not None:
        result['rolledBack'] = rolled_back
    else:
        result.pop('rolledBack', None)
    return {
        key: value for key, value in result.items()
        if key in PUBLIC_PROJECTION_FIELDS
    }


def is_text_projection_patch(patch: Any) -> bool:
    """Admit only set/append_text for the two revision-fenced text roots."""
    if not isinstance(patch, Mapping) or type(patch.get('version')) is not int:
        return False
    if patch['version'] != PROJECTION_PATCH_VERSION:
        return False
    operations = patch.get('operations')
    if not isinstance(operations, list) or len(operations) > 2:
        return False
    fields: set[str] = set()
    for operation in operations:
        if not isinstance(operation, Mapping):
            return False
        path = operation.get('path')
        if (not isinstance(path, list) or len(path) != 1
                or not isinstance(path[0], str)
                or path[0] not in {'content', 'thinking'} or path[0] in fields
                or operation.get('op') not in {'set', 'append_text'}
                or not isinstance(operation.get('value'), str)):
            return False
        fields.add(path[0])
    return True


def build_projection_patch(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    base_revision: int,
    target_revision: int,
) -> dict[str, Any]:
    """Return a deterministic patch from ``before`` to ``after``.

    Paths are arrays rather than JSON Pointer strings, avoiding a second
    escaping grammar.  Growing strings and lists use append operations; this
    is the load-bearing property for cumulative content and ``toolRounds``.
    Every patch names both revisions so a client can fail closed and request
    an authoritative snapshot when an event gap is detected.
    """

    previous = dict(before or {})
    current = dict(after or {})
    operations: list[dict[str, Any]] = []
    _diff_value(previous, current, [], operations)
    return {
        "version": PROJECTION_PATCH_VERSION,
        "baseRevision": int(base_revision),
        "targetRevision": int(target_revision),
        "operations": operations,
    }


def apply_projection_patch(
    projection: Mapping[str, Any] | None,
    raw_patch: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one versioned patch without mutating the prior projection.

    This is the storage-side twin of
    ``frontend/src/core/projection-patch.ts::applyProjectionPatch``.  Storage
    callers validate the named revisions against their locked row; this pure
    helper owns only operation/path validation and copy-on-write application.
    """
    if not isinstance(raw_patch, Mapping):
        raise ProjectionPatchError("Projection patch must be an object")
    version = raw_patch.get("version")
    operations = raw_patch.get("operations")
    if (not isinstance(version, int) or isinstance(version, bool)
            or version != PROJECTION_PATCH_VERSION
            or not isinstance(operations, list)):
        raise ProjectionPatchError("Projection patch version is unsupported")

    next_projection: Any = dict(projection or {})
    for raw_operation in operations:
        if not isinstance(raw_operation, Mapping):
            raise ProjectionPatchError("Projection patch operation must be an object")
        path = raw_operation.get("path")
        if not isinstance(path, list) or not all(
            (isinstance(part, str)
             or (isinstance(part, int) and not isinstance(part, bool)
                 and part >= 0))
            for part in path
        ):
            raise ProjectionPatchError("Projection patch path is invalid")
        operation = raw_operation.get("op")
        if operation == "set":
            next_projection = _update_at_path(
                next_projection, path,
                lambda _current: raw_operation.get("value"),
            )
        elif operation == "remove":
            next_projection = _remove_at_path(next_projection, path)
        elif operation == "append_text":
            value = raw_operation.get("value")
            if not isinstance(value, str):
                raise ProjectionPatchError(
                    "Projection text append value must be a string")

            def _append_text(current: Any) -> str:
                if not isinstance(current, str):
                    raise ProjectionPatchError(
                        "Projection text append target must be a string")
                return current + value

            next_projection = _update_at_path(
                next_projection, path, _append_text)
        elif operation == "append":
            value = raw_operation.get("value")
            if not isinstance(value, list):
                raise ProjectionPatchError(
                    "Projection list append value must be an array")

            def _append_list(current: Any) -> list[Any]:
                if not isinstance(current, list):
                    raise ProjectionPatchError(
                        "Projection list append target must be an array")
                return [*current, *value]

            next_projection = _update_at_path(
                next_projection, path, _append_list)
        elif operation == "truncate":
            length = raw_operation.get("length")
            if (not isinstance(length, int) or isinstance(length, bool)
                    or length < 0):
                raise ProjectionPatchError(
                    "Projection list truncation length is invalid")

            def _truncate(current: Any) -> list[Any]:
                if not isinstance(current, list) or length > len(current):
                    raise ProjectionPatchError(
                        "Projection list truncation target is invalid")
                return current[:length]

            next_projection = _update_at_path(
                next_projection, path, _truncate)
        else:
            raise ProjectionPatchError("Projection patch operation is unsupported")

    if not isinstance(next_projection, dict):
        raise ProjectionPatchError("Projection patch result must be an object")
    return next_projection


def _update_at_path(
    value: Any,
    path: list[str | int],
    update: Any,
    depth: int = 0,
) -> Any:
    if depth >= len(path):
        return update(value)
    part = path[depth]
    if isinstance(value, list):
        if not isinstance(part, int) or isinstance(part, bool) or part >= len(value):
            raise ProjectionPatchError("Projection patch array path is out of bounds")
        next_value = list(value)
        next_value[part] = _update_at_path(
            next_value[part], path, update, depth + 1)
        return next_value
    if not isinstance(value, Mapping) or not isinstance(part, str):
        raise ProjectionPatchError("Projection patch object path is invalid")
    next_value = dict(value)
    next_value[part] = _update_at_path(
        next_value.get(part), path, update, depth + 1)
    return next_value


def _remove_at_path(value: Any, path: list[str | int]) -> Any:
    if not path:
        raise ProjectionPatchError("Projection patch cannot remove its root")
    parent_path = path[:-1]
    leaf = path[-1]

    def _remove(container: Any) -> Any:
        if isinstance(container, list):
            if (not isinstance(leaf, int) or isinstance(leaf, bool)
                    or leaf >= len(container)):
                raise ProjectionPatchError(
                    "Projection patch array removal is out of bounds")
            next_container = list(container)
            next_container.pop(leaf)
            return next_container
        if not isinstance(container, Mapping) or not isinstance(leaf, str):
            raise ProjectionPatchError(
                "Projection patch object removal is invalid")
        next_container = dict(container)
        next_container.pop(leaf, None)
        return next_container

    return _update_at_path(value, parent_path, _remove)


def _diff_value(
    before: Any,
    after: Any,
    path: list[str | int],
    operations: list[dict[str, Any]],
) -> None:
    # Settled history keeps its identity across revisions; an identical
    # container is equal without walking it (scalars keep ``==``: NaN).
    if before is after and type(before) in _IDENTITY_EQUAL_CONTAINERS:
        return
    if before == after:
        return

    if isinstance(before, str) and isinstance(after, str):
        if after.startswith(before):
            suffix = after[len(before):]
            if suffix:
                operations.append({
                    "op": "append_text", "path": list(path), "value": suffix,
                })
            return
        operations.append({"op": "set", "path": list(path), "value": after})
        return

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys - after_keys, key=str):
            operations.append({"op": "remove", "path": [*path, str(key)]})
        for key in sorted(after_keys - before_keys, key=str):
            operations.append({
                "op": "set", "path": [*path, str(key)], "value": after[key],
            })
        for key in sorted(before_keys & after_keys, key=str):
            _diff_value(before[key], after[key], [*path, str(key)], operations)
        return

    if (_is_json_sequence(before) and _is_json_sequence(after)):
        before_list = list(before)
        after_list = list(after)
        start, evicted = _evicted_run(before_list, after_list)
        for _ in range(evicted):
            operations.append({"op": "remove", "path": [*path, start]})
        del before_list[start:start + evicted]
        shared = min(len(before_list), len(after_list))
        for index in range(start, shared):
            _diff_value(
                before_list[index], after_list[index], [*path, index], operations,
            )
        if len(after_list) < len(before_list):
            operations.append({
                "op": "truncate", "path": list(path), "length": len(after_list),
            })
        elif len(after_list) > len(before_list):
            operations.append({
                "op": "append", "path": list(path),
                "value": after_list[len(before_list):],
            })
        return

    operations.append({"op": "set", "path": list(path), "value": after})


def _same_item(before: Any, after: Any) -> bool:
    """Equality with ``_diff_value``'s settled-container identity shortcut."""
    return (before is after and type(before) in _IDENTITY_EQUAL_CONTAINERS) \
        or before == after


def _unaligned_items(before: list[Any], after: list[Any], offset: int) -> int:
    """Count items re-sent when ``after[i]`` is diffed against ``before[offset + i]``."""
    overlap = min(len(before) - offset, len(after))
    changed = sum(
        1 for index in range(overlap)
        if not _same_item(before[offset + index], after[index])
    )
    return changed + len(after) - overlap


def _evicted_run(before: list[Any], after: list[Any]) -> tuple[int, int]:
    """Return ``(start, count)``: the first divergence and the run evicted there.

    Activity timelines and trace status histories keep their newest rows; once
    full, each new row shifts every later index, so an index-aligned diff
    rewrites every field of every later row. The evicted run starts at the
    first divergence: the head, or behind rows the window retains (an error,
    a compaction receipt). Removing it keeps the retained rows aligned. The
    count is zero unless that re-sends fewer items than index alignment, so
    append-only and in-place updates keep their existing patch. Items before
    ``start`` are equal; the caller diffs from there.
    """
    shared = min(len(before), len(after))
    start = next(
        (index for index in range(shared)
         if not _same_item(before[index], after[index])),
        shared,
    )
    if start == shared:
        return start, 0
    evicted = next(
        (offset for offset in range(1, len(before) - start)
         if _same_item(before[start + offset], after[start])),
        0,
    )
    if not evicted:
        return start, 0
    before, after = before[start:], after[start:]
    shifted = evicted + _unaligned_items(before, after, evicted)
    return start, (evicted if shifted < _unaligned_items(before, after, 0) else 0)


_IDENTITY_EQUAL_CONTAINERS = frozenset({dict, list, SettledDict, SettledList})
_DICT_KINDS = (dict, SettledDict)


def _is_json_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


__all__ = [
    "PUBLIC_PROJECTION_FIELDS",
    "PROJECTION_PATCH_VERSION",
    "ProjectionPatchError",
    "apply_projection_patch",
    "build_projection_patch",
    "normalize_projection_document",
]
