"""Authoritative Turn / Attempt lifecycle for the turn-native chat protocol.

One visible row owns one stable ``turn_id`` and each execution owns one
``attempt_id``. This module is a stateless domain facade over the semantic
storage protocol; backend transactions live exclusively in the sidecar. Task
ids remain an internal bridge to the model/tool executor.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.error_envelope import make_envelope, normalize_envelope
from lib.conversation_sync.dispatch_contract import (
    CONVERSATION_EXECUTOR_DISPATCH_MODE,
    normalize_attempt_dispatch_mode,
)
from lib.identity import PrincipalContext, require_user_id
from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.tasks_pkg.failure_attribution import (
    ensure_failure_attribution,
    turn_retry_chain,
)
from lib.storage.storage_projection import (
    compact_tool_rounds_for_frame_budget,
    sanitize_api_rounds_for_persist,
    trim_tool_round_for_persist,
)
from lib.tool_results.tool_round_identity import tool_rounds_with_execution_identity
from lib.tool_results.tool_round_replay import (
    checkpoint_retention_positions,
    scan_replayable_tool_round_prefix,
)
from lib.turn_verdict import (
    derive_turn_verdict,
    task_terminal_evidence,
)
from lib.turn_projection_segments import (
    projection_with_stable_segments,
    public_turn_with_stable_segments,
    public_value_with_stable_segments,
)
from lib.turn_projection_patch import build_projection_patch


logger = get_logger(__name__)


def _turn_client(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


ACTORS = frozenset({'human', 'assistant', 'planner', 'critic', 'virtual_user'})
OPERATIONS = frozenset({'generate', 'continue', 'checkpoint_resume', 'regenerate',
                        'answer_guidance'})
TERMINAL_STATUSES = frozenset({'completed', 'interrupted', 'truncated', 'failed'})
LIVE_ATTEMPT_STATUSES = frozenset({'pending', 'running'})
_PROJECTION_INJECTION_LANES = (
    ('_inboxInjects', '_inboxInjects', 'inbox'),
    ('_peerInjects', '_peerInjects', 'peer'),
    ('_userSteerInjects', '_userSteerInjects', 'user-steer'),
    ('_bgCommandInjects', '_bgCommandInjects', 'background-command'),
    ('_stallNudges', '_stallNudges', 'stall-nudge'),
)
_PROJECTION_PROVENANCE_FIELDS = (
    ('_memoryPrefetch', 'memoryPrefetch'),
    ('_mcpLoginHint', 'mcpLoginHint'),
    ('_mcpToolsDelta', 'mcpToolsDelta'),
    ('_projectPathChange', 'projectPathChange'),
    ('_preferencesApplied', 'preferencesApplied'),
    ('_preferencesLearned', 'preferencesLearned'),
    ('_relatedConversations', 'relatedConversations'),
)

_PROJECTION_TASK_FIELDS = (
        ('segments', 'segments'), ('usage', 'usage'), ('model', 'model'),
        ('_executionActualsComplete', 'usageComplete'),
        ('provider_id', 'providerId'),
        ('preset', 'preset'), ('thinkingDepth', 'thinkingDepth'),
        ('modifiedFiles', 'modifiedFiles'),
        ('modifiedFilesTruncated', 'modifiedFilesTruncated'),
        ('modifiedFileList', 'modifiedFileList'), ('todoState', 'todoState'),
        ('waitingOn', 'waitingOn'),
        # Fallback metadata must survive into the turn-native projection so the
        # finish tag can show "requested → actual" instead of silently
        # displaying only the fallback model.  Without these fields the
        # user sees e.g. "gpt-5.3-codex-spark" when they picked glm-5.3
        # and the dispatcher fell back after a 402 credit-exhausted error.
        # The task stores them with underscore prefix (_fallback_model);
        # the projection exposes them as camelCase (fallbackModel) to
        # match the frontend finish-tag contract.
        ('_fallback_model', 'fallbackModel'),
        ('_fallback_from', 'fallbackFrom'),
        ('_fallback_reason', 'fallbackReason'),
        ('_fallback_kind', 'fallbackKind'),
        # Live "size of the prompt JUST sent" reading, stashed by
        # llm_fallback._emit_round_usage on EVERY LLM round.  This is the turn-native
        # successor of the v1 SSE ``round_usage`` → ``_liveLastRoundUsage``
        # feed: riding the durable turn projection (instead of a session-only
        # SSE frame) means the context-health gauge keeps moving per round
        # under the turn-native lane AND survives reconnect replay / slim-delta
        # windows for free (slim frames patch only content/thinking on the
        # turn row; tail hydration re-serves this row).
        ('_lastRoundUsage', 'lastRoundUsage'),
        # Structured, credential-redacted route evidence. This is the sole
        # source for provider/model failover timelines after reload.
        ('_route_snapshot', 'routeSnapshot'),
)

_turn_search_backfill_lock = threading.Lock()
_turn_search_backfill_started = False
_TURN_SEARCH_BACKFILL_INITIAL_DELAY_SECONDS = 60.0

# One process-stable dispatch owner makes ``turn.attempt.claim`` safely
# replayable after an ambiguous sidecar acknowledgement. A fresh process gets
# a fresh identity; boot recovery settles claims left by its predecessor
# instead of silently repeating billable work.
_ATTEMPT_DISPATCH_OWNER_ID = uuid.uuid4().hex
_ATTEMPT_CLAIM_MAX_ATTEMPTS = 4
_ATTEMPT_CLAIM_RPC_DEADLINE_SECONDS = 2.0
# Every in-process dispatch entry point shares these stripes from claim through
# bind/spawn. This makes same-owner claim replay safe without retaining an
# unbounded lock/token map for arbitrary attempt IDs.
_ATTEMPT_DISPATCH_LOCKS = tuple(threading.Lock() for _ in range(256))


# ── Text-delta write coalescing ──────────────────────────────────────────
# ``record_task_event`` is on the per-token hot path: every streamed ``delta``
# used to commit a FULL-projection write transaction (CAS + event row +
# conversation revision bump) through the single storage writer. At token
# rate that saturates the writer lane; the acquisition deadline then trips
# and the caller WITHHOLDS the frame from clients — the live stream starves
# until the next conversation switch forces a snapshot (owner incident
# 2026-08-17: "agent bubble only appears after switching conversations").
#
# A projection is cumulative (last write wins) and every structural event
# (phase/tool lifecycle/interaction/terminal) still persists immediately,
# carrying any coalesced progress with it. Coalescing pure PROGRESS frames
# (token deltas, streaming program output, tool progress ticks) therefore
# loses nothing: replay/snapshot readers converge to the same state, just
# without the intermediate 10ms slices. ``TOFU_TURN_DELTA_RECORD_MS=0``
# restores the old write-every-frame behaviour.
_COALESCIBLE_EVENT_KINDS = frozenset({'delta', 'program_output', 'tool_progress'})
def _delta_record_min_interval_s() -> float:
    import os
    try:
        return max(0.0, float(os.environ.get(
            'TOFU_TURN_DELTA_RECORD_MS', '300')) / 1000.0)
    except (TypeError, ValueError):
        return 0.3


_delta_throttle_lock = threading.Lock()
_delta_last_recorded: dict[str, float] = {}
_DELTA_THROTTLE_MAX_KEYS = 4096


def next_progress_projection_deadline(attempt_id: str) -> float | None:
    """Observe the committed projection cadence; never reserve or authorize a write.

    Provider text may shorten its existing trailing window to this monotonic
    deadline. Missing observations retain the caller's own window; an eager
    projection policy returns an already-due deadline. No storage read occurs.
    """
    interval = _delta_record_min_interval_s()
    if interval <= 0:
        return 0.0
    with _delta_throttle_lock:
        last = _delta_last_recorded.get(attempt_id)
    return None if last is None else last + interval


def _delta_throttle_allows(attempt_id: str) -> bool:
    """Peek: True when a delta projection write is due for this attempt."""
    deadline = next_progress_projection_deadline(attempt_id)
    return deadline is None or time.monotonic() >= deadline


def _delta_throttle_stamp(attempt_id: str) -> None:
    with _delta_throttle_lock:
        if len(_delta_last_recorded) >= _DELTA_THROTTLE_MAX_KEYS:
            _delta_last_recorded.clear()
        _delta_last_recorded[attempt_id] = time.monotonic()


def _delta_throttle_clear(attempt_id: str) -> None:
    with _delta_throttle_lock:
        _delta_last_recorded.pop(attempt_id, None)


# ── Structural-fold cadence for DELTA-class writes ────────────────────────
# Pure PROGRESS frames (delta / program_output / tool_progress) must only
# advance the cumulative ``content``/``thinking`` text, not re-fold the
# growing ``toolRounds`` list on every allowed write — that made the
# authority write grow O(toolRounds) as the turn lengthened.  The full
# structural fold (``_task_projection``) still runs on every structural /
# terminal frame AND, for the restart-recovery consumer that computes resume
# options from the DURABLE projection, at a slower cadence
# (``TOFU_TURN_STRUCTURAL_RECORD_MS``, default 3s) so ``toolRounds`` freshness
# stays bounded even if the attempt dies mid-delta-stream with no intervening
# tool event.  Tradeoff: a crash during a long pure-delta burst can lose at
# most one cadence window of toolRounds (in practice none — tool rounds
# mutate through tool_start / tool_result, which are structural and fold
# immediately).
def _structural_record_min_interval_s() -> float:
    import os
    try:
        return max(0.0, float(os.environ.get(
            'TOFU_TURN_STRUCTURAL_RECORD_MS', '3000')) / 1000.0)
    except (TypeError, ValueError):
        return 3.0


_structural_fold_lock = threading.Lock()
_structural_last_fold: dict[str, float] = {}
_STRUCTURAL_FOLD_MAX_KEYS = 4096
_OVERSIZE_PROJECTION_RETRY_SECONDS = 30.0


def _structural_fold_due(attempt_id: str) -> bool:
    """True when a DELTA-class write should also re-fold the full projection."""
    if _structural_record_min_interval_s() <= 0:
        return True
    with _structural_fold_lock:
        last = _structural_last_fold.get(attempt_id)
        return last is None or (
            time.monotonic() - last) >= _structural_record_min_interval_s()


def _structural_fold_stamp(attempt_id: str) -> None:
    with _structural_fold_lock:
        if len(_structural_last_fold) >= _STRUCTURAL_FOLD_MAX_KEYS:
            _structural_last_fold.clear()
        _structural_last_fold[attempt_id] = time.monotonic()


def _structural_fold_clear(attempt_id: str) -> None:
    with _structural_fold_lock:
        _structural_last_fold.pop(attempt_id, None)


def _delta_text_fields(task: dict[str, Any],
                       previous: dict[str, Any]) -> tuple[str, str]:
    """Cumulative content/thinking for a DELTA-class write.

    Mirrors the content/thinking rules of ``_task_projection`` without the
    structural fold (no toolRounds / segments / usage recomputation).
    """
    owns_visible_run_turns = bool(task.get('_turnVisibleRunTurnIds'))
    # Same content rule as ``_task_projection`` — see its contentPrefix note.
    content = (previous.get('content', '') if owns_visible_run_turns else
               (task.get('content') or ''))
    thinking = (previous.get('thinking', '') if owns_visible_run_turns else
                (task.get('thinking') if task.get('thinking') is not None
                 else previous.get('thinking', '')))
    return content, thinking


@dataclass
class LifecycleConflict(RuntimeError):
    code: str
    message: str
    turn: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


class LifecycleNotFound(LookupError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


# Delta-sync watermark sanity bound: a ``since_ms`` further into the future
# than this is client/server clock confusion, and the read degrades to a full
# snapshot instead of trusting it.
_DELTA_MAX_SKEW_MS = 300_000


def create_turn_pair(conversation_id: str, *, command_id: str,
                     input_projection: Any, config: dict[str, Any] | None,
                     lane_id: str = 'main', parent_turn_id: str | None = None,
                     kind: str = 'reply', output_actor: str = 'assistant',
                     run_id: str = '', user_id: Any,
                     input_actor: str = 'human', input_kind: str = 'input',
                     require_parent_is_lane_tail: bool = False,
                     expected_parent_projection_revision: int | None = None,
                     require_parent_not_stopped: bool = False,
                     require_lane_idle: bool = False,
                     reject_if_human_queued: bool = False,
                     conversation_defaults: dict[str, Any] | None = None,
                     dispatch_mode: str = '',
                     input_presentation_id: str = '',
                     output_presentation_id: str = '',
                     queue_binding: dict[str, Any] | None = None,
                     ) -> dict[str, Any]:
    """Atomically create the input turn, output turn and first attempt."""
    user_id = require_user_id(user_id, context='create turn pair')
    if not command_id:
        raise ValueError('commandId is required')
    if output_actor not in ACTORS or output_actor == 'human':
        raise ValueError('invalid output actor')
    if input_actor not in {'human', 'virtual_user', 'critic'}:
        raise ValueError('invalid input actor')
    dispatch_mode = normalize_attempt_dispatch_mode(dispatch_mode)
    lane_id = lane_id or 'main'
    from lib.storage import StorageError
    normalized_input_projection = projection_with_stable_segments(
        input_projection,
        actor=input_actor,
        status='completed',
    )
    payload = {
        'conversation_id': conversation_id, 'user_id': user_id,
        'command_id': command_id, 'input_projection': normalized_input_projection,
        'config': config or {}, 'lane_id': lane_id,
        'parent_turn_id': parent_turn_id, 'kind': kind or 'reply',
        'output_actor': output_actor, 'run_id': run_id,
        'input_actor': input_actor, 'input_kind': input_kind or 'input',
        'require_parent_is_lane_tail': bool(require_parent_is_lane_tail),

        'require_parent_not_stopped': bool(require_parent_not_stopped),
        'require_lane_idle': bool(require_lane_idle),
        'reject_if_human_queued': bool(reject_if_human_queued),
        'conversation_defaults': conversation_defaults or {},
        'dispatch_mode': dispatch_mode,
        'input_presentation_id': (
            input_presentation_id or f'{command_id}:input'),
        'output_presentation_id': (
            output_presentation_id or f'{command_id}:output'),
        'queue_binding': queue_binding or {},
    }
    if expected_parent_projection_revision is not None:
        if (type(expected_parent_projection_revision) is not int
                or expected_parent_projection_revision < 0 or not parent_turn_id):
            raise ValueError('Invalid parent projection revision')
        payload['expected_parent_projection_revision'] = expected_parent_projection_revision
    try:
        return public_value_with_stable_segments(_turn_client(write=True).command(
            'turn.create_pair', payload, command_id)
        )
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'turn_in_progress':
            latest = None
            try:
                rows = _turn_client().query(
                    'turn.list', {
                        'conversation_id': conversation_id,
                        'user_id': user_id,
                        'lane_id': lane_id,
                    })
                latest = rows[-1] if rows else None
            except StorageError:
                # Delivery needs the authoritative live attempt. Preserve a
                # failed read's typed availability/retry contract for the API;
                # an unknown target must not become an empty busy receipt.
                raise
            except Exception:
                logger.debug(
                    'Could not hydrate lane-busy turn conv=%s',
                    conversation_id[:12], exc_info=True)
            raise LifecycleConflict(
                'lane_busy', str(exc), latest) from exc
        if exc.code == 'turn_parent_invalid':
            raise LifecycleConflict(
                'invalid_parent_turn', str(exc)) from exc
        if exc.code == 'turn_lane_advanced':
            raise LifecycleConflict('lane_advanced', str(exc)) from exc
        if exc.code == 'turn_superseded_by_human':
            raise LifecycleConflict('superseded_by_human', str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def activate_queued_turn_pair(
    conversation_id: str, queue_id: str, *, user_id: Any,
) -> dict[str, Any]:
    """Atomically move one already-created queued pair into the main lane."""
    user_id = require_user_id(user_id, context='activate queued turn pair')
    try:
        return public_value_with_stable_segments(_turn_client(write=True).command(
            'turn.queue.activate',
            {
                'conversation_id': conversation_id,
                'queue_id': queue_id,
                'user_id': user_id,
            },
            command_id=f'turn-queue-activate:{conversation_id}:{queue_id}',
        ))
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'turn_in_progress':
            raise LifecycleConflict('lane_busy', str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def cancel_queued_turn_pair(
    conversation_id: str, queue_id: str, *, user_id: Any,
) -> dict[str, Any]:
    """Cancel pending delivery, deleting an unexecuted pair when present."""
    user_id = require_user_id(user_id, context='cancel queued turn pair')
    try:
        result = public_value_with_stable_segments(_turn_client(write=True).command(
            'turn.queue.cancel',
            {
                'conversation_id': conversation_id,
                'queue_id': queue_id,
                'user_id': user_id,
            },
            command_id=f'turn-queue-cancel:{conversation_id}:{queue_id}',
        ))
        if result.get('cancelled'):
            # A completion can also have a live inbox twin. Fence late twin
            # publication only after the owner-scoped durable cancel commits.
            from lib.agent_inbox import consume_peer
            consume_peer(conversation_id, [queue_id])
        return result
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def commit_user_steer(
    conversation_id: str,
    attempt_id: str,
    *,
    command_id: str,
    text: str,
    user_id: Any,
    images: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Commit a bounded receipt; callers must wake the inbox before hydration."""
    user_id = require_user_id(user_id, context='commit user steer')
    payload: dict[str, Any] = {
        'conversation_id': conversation_id,
        'attempt_id': attempt_id,
        'command_id': command_id,
        'text': text,
        'user_id': user_id,
    }
    if images:
        payload['images'] = images
    try:
        return public_value_with_stable_segments(_turn_client(write=True).command(
            'turn.steer.commit',
            payload,
            command_id=f'turn-steer-commit:{conversation_id}:{command_id}',
        ))
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code in {'database_conflict', 'turn_in_progress'}:
            raise LifecycleConflict('steer_window_closed', str(exc)) from exc
        raise


def hydrate_user_steer(
    conversation_id: str, receipt: Mapping[str, Any], *, user_id: Any,
) -> dict[str, Any]:
    """Expand a committed steer only after its inbox wakeup has been published.

    This owner-scoped read returns current authority, not a receipt snapshot.
    Read failures must propagate outside the durable-commit inbox callback.
    """
    result = dict(receipt)
    result['turn'] = get_turn(conversation_id, str(receipt['turnId']), user_id=user_id)
    return result


def append_settled_turn(
    conversation_id: str,
    *,
    command_id: str,
    actor: str,
    projection: dict[str, Any],
    user_id: Any,
    kind: str = 'ingested',
    status: str = 'completed',
    settlement: dict[str, Any] | None = None,
    created_at: int | None = None,
    lane_id: str = 'main',
    run_id: str = '',
    conversation_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one terminal turn through the canonical ingestion boundary."""
    user_id = require_user_id(user_id, context='append settled turn')
    payload: dict[str, Any] = {
        'conversation_id': conversation_id,
        'user_id': user_id,
        'command_id': command_id,
        'actor': actor,
        'projection': projection_with_stable_segments(
            projection, actor=actor, status=status,
        ),
        'kind': kind,
        'status': status,
        'lane_id': lane_id or 'main',
        'run_id': run_id,
        'conversation_defaults': conversation_defaults or {},
        'settlement': settlement or {
            'outcome': status,
            'cause': 'ingested',
            'resumeOptions': [],
        },
    }
    if created_at is not None:
        payload['created_at'] = int(created_at)
    from lib.storage import StorageError
    try:
        return public_value_with_stable_segments(
            _turn_client(write=True).command(
                'turn.append_settled', payload, command_id)
        )
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def announce_related_turns(
    attempt_id: str, turn_ids: list[str], *, user_id: Any,
) -> bool:
    """Publish server-created orchestration identities on a parent stream."""
    user_id = require_user_id(user_id, context='announce related turns')
    if not attempt_id or not turn_ids:
        return False
    now = _now_ms()
    result = _turn_client(write=True).command(
        'turn.related.announce', {
            'attempt_id': attempt_id,
            'turn_ids': turn_ids,
            'user_id': user_id,
        },
        f'turn-related:{attempt_id}:{now}')
    return bool(result.get('changed')) if isinstance(result, dict) else bool(result)


def create_attempt(conversation_id: str, turn_id: str, *, command_id: str,
                   operation: str, expected_projection_revision: int,
                   config: dict[str, Any] | None = None,
                   resume_anchor: dict[str, Any] | None = None,
                   input_update: dict[str, Any] | None = None,
                   expected_input_projection_revision: int | None = None,
                   dispatch_mode: str = CONVERSATION_EXECUTOR_DISPATCH_MODE,
                   user_id: Any) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='create turn attempt')
    if not command_id:
        raise ValueError('commandId is required')
    if operation not in OPERATIONS - {'generate'}:
        raise ValueError('operation must be continue, checkpoint_resume, regenerate, or answer_guidance')
    dispatch_mode = normalize_attempt_dispatch_mode(dispatch_mode)

    executable_config = dict(config or {})
    target_actor = None
    target_kind = None
    if operation == 'regenerate':
        from lib.tasks_pkg.plan_mode import (
            interaction_mode_generated_turn_identity,
            normalize_interaction_mode_runtime_config,
        )
        executable_config = normalize_interaction_mode_runtime_config(
            executable_config
        )
        target_actor, target_kind = interaction_mode_generated_turn_identity(
            executable_config
        )
    from lib.storage import StorageError
    try:
        return _turn_client(write=True).command(
            'turn.attempt.create', {
                'conversation_id': conversation_id, 'user_id': user_id,
                'turn_id': turn_id, 'command_id': command_id,
                'operation': operation,
                'expected_projection_revision': expected_projection_revision,
                'config': executable_config, 'resume_anchor': resume_anchor,
                'input_update': input_update,
                'expected_input_projection_revision': expected_input_projection_revision,

                'target_actor': target_actor,
                'target_kind': target_kind,
                'dispatch_mode': dispatch_mode,
            }, command_id)
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'turn_projection_stale':
            latest = None
            try:
                latest = get_turn(
                    conversation_id, turn_id, user_id=user_id)
            except LifecycleNotFound:
                pass
            raise LifecycleConflict(
                'stale_projection', str(exc), latest) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def bind_task(
    attempt_id: str, task_id: str, *, user_id: Any,
) -> dict[str, Any] | None:
    """Bind scheduler identity while the attempt remains durably pending."""
    user_id = require_user_id(user_id, context='bind turn task')
    return _turn_client(write=True).command(
        'turn.attempt.bind', {'attempt_id': attempt_id,
                              'task_id': task_id,
                              'user_id': user_id,
                              'dispatch_owner_id': _ATTEMPT_DISPATCH_OWNER_ID},
        f'turn-bind:{attempt_id}:{task_id}')


def mark_task_started(
    attempt_id: str, task_id: str, *, user_id: Any,
    automation_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Publish the pending→running transition at physical worker entry."""
    user_id = require_user_id(user_id, context='start bound turn task')
    return _turn_client(write=True).command(
        'turn.attempt.start', {
            'attempt_id': attempt_id,
            'task_id': task_id,
            'user_id': user_id,
            **({'automation_context': automation_context} if automation_context is not None else {}),
        },
        f'turn-start:{attempt_id}:{task_id}',
    )


def dispatch_attempt_to_worker(
    principal: PrincipalContext,
    attempt_id: str,
    *,
    priority: int = 100,
    now_ms: int | None = None,
) -> dict[str, Any] | None:
    """Atomically bind an accepted attempt to one durable worker job.

    This is a storage foundation, not the in-process executor switch.  The job
    contains durable turn references and the explicit principal only; a
    production handler must still prove event replay, terminal accounting, and
    external side-effect fencing before its kind is eligible for claims.
    """
    if not isinstance(principal, PrincipalContext):
        raise TypeError('worker dispatch requires PrincipalContext')
    owner_user_id = principal.require_owner(
        context='conversation worker dispatch')
    if not attempt_id:
        raise ValueError('attempt_id is required')
    effective_now_ms = _now_ms() if now_ms is None else int(now_ms)
    return _turn_client(write=True).command(
        'turn.attempt.dispatch_worker', {
            'attempt_id': attempt_id,
            'user_id': owner_user_id,
            'principal': principal.to_payload(),
            'priority': int(priority),
            'now_ms': effective_now_ms,
        },
        f'turn-worker-dispatch:{attempt_id}',
    )


def attempt_dispatch_lock(attempt_id: str) -> threading.Lock:
    """Return the bounded process-wide serialization stripe for one attempt."""
    normalized_attempt_id = str(attempt_id or '')
    if not normalized_attempt_id:
        raise ValueError('attempt_id is required for dispatch serialization')
    return _ATTEMPT_DISPATCH_LOCKS[
        hash(normalized_attempt_id) % len(_ATTEMPT_DISPATCH_LOCKS)
    ]


def claim_attempt_start(attempt_id: str, *, user_id: Any) -> bool:
    """Acquire the one-shot executor-dispatch lease for an accepted attempt.

    This closes the commit-to-task-bind window. The claim carries one
    process-stable owner, so an ambiguous acknowledgement can be retried by
    this process while a different process still loses the CAS. A process
    crash after the claim is intentionally recovered as ``interrupted`` on
    boot, never auto-retried.
    """
    user_id = require_user_id(user_id, context='claim turn attempt')
    last_error: StorageError | None = None
    for attempt_no in range(_ATTEMPT_CLAIM_MAX_ATTEMPTS):
        try:
            claimed = _turn_client(write=True).command(
                'turn.attempt.claim', {
                    'attempt_id': attempt_id,
                    'user_id': user_id,
                    'dispatch_owner_id': _ATTEMPT_DISPATCH_OWNER_ID,
                },
                f'turn-claim:{attempt_id}',
                deadline=_ATTEMPT_CLAIM_RPC_DEADLINE_SECONDS,
            )
            if last_error is not None:
                logger.info(
                    '[TurnLifecycle] attempt dispatch claim recovered after '
                    '%d transient failure(s) attempt=%s',
                    attempt_no,
                    attempt_id[:12],
                )
            return bool(claimed)
        except StorageError as exc:
            if not exc.retryable:
                raise
            last_error = exc
            if attempt_no + 1 >= _ATTEMPT_CLAIM_MAX_ATTEMPTS:
                raise
            delay = min(
                0.5,
                max(
                    float(exc.retry_after_ms or 0) / 1000.0,
                    0.05 * (2 ** attempt_no),
                ),
            )
            logger.warning(
                '[TurnLifecycle] transient attempt dispatch claim failure; '
                'retrying in %.2fs attempt=%s code=%s try=%d/%d',
                delay,
                attempt_id[:12],
                exc.code,
                attempt_no + 1,
                _ATTEMPT_CLAIM_MAX_ATTEMPTS,
            )
            time.sleep(delay)
    raise last_error or RuntimeError('Attempt claim retry loop exited')


def fail_start(attempt_id: str, error: Any, *, user_id: Any) -> None:
    # Validate ownership before constructing an internal task carrier. Worker
    # event recording derives scope from the durable attempt, but callers at
    # the command boundary must still name the authenticated owner explicitly.
    user_id = require_user_id(user_id, context='fail turn attempt start')
    get_attempt(attempt_id, user_id=user_id)
    task = {'_attemptId': attempt_id, '_userId': user_id,
            'id': '', 'status': 'error',
            'error': error, 'content': '', 'thinking': '', 'toolRounds': []}
    record_task_event(task, {'type': 'error', 'error': error})


def _projection_injection_records(records: Any, channel: str) -> Any:
    """Copy a display-only injection lane and assign durable block identity.

    Task dictionaries remain executor-owned and are never mutated here.  The
    projection boundary is the first shared authority seen by reconnects and
    every frontend, so it is also the only safe place to repair legacy records
    that predate ``blockId``.  Duplicate producer IDs are disambiguated in
    lane order instead of leaving DOM identity up to a renderer.
    """
    if not isinstance(records, list):
        return records
    claimed_ids: dict[str, int] = {}
    projected_records: list[Any] = []
    for record in records:
        if not isinstance(record, dict):
            projected_records.append(record)
            continue
        projected = dict(record)
        round_value = projected.get('round')
        round_token = (str(round_value) if isinstance(round_value, int)
                       and not isinstance(round_value, bool)
                       and round_value >= 0 else 'unknown')
        declared_id = projected.get('blockId')
        preferred_id = (declared_id.strip()
                        if isinstance(declared_id, str) and declared_id.strip()
                        else f'injection:{channel}:round-{round_token}')
        occurrence = claimed_ids.get(preferred_id, 0) + 1
        claimed_ids[preferred_id] = occurrence
        projected['blockId'] = (preferred_id if occurrence == 1
                                else f'{preferred_id}~{occurrence}')
        projected_records.append(projected)
    return projected_records


def _merge_user_steer_records(previous: Any, local: Any) -> Any:
    """Fold confirmed steer delivery into its accepted pending card.

    A steer is committed to the Turn before its executor is woken. Its
    confirmation appears later in the worker's sidecar; replacing the entire
    lane with that sidecar loses other accepted (still pending) messages, while
    blindly concatenating renders one steer twice. Stable blockId is the only
    identity; never deduplicate by message text or by round number.
    """
    if not isinstance(local, list):
        return local
    old = previous if isinstance(previous, list) else []
    result = [dict(item) if isinstance(item, dict) else item for item in old]
    index_by_id = {
        item['blockId']: index for index, item in enumerate(result)
        if isinstance(item, dict) and isinstance(item.get('blockId'), str)
        and item['blockId']
    }
    for item in local:
        if not isinstance(item, dict):
            result.append(item)
            continue
        block_id = item.get('blockId')
        if not isinstance(block_id, str) or not block_id:
            result.append(dict(item))
            continue
        index = index_by_id.get(block_id)
        if index is None:
            index_by_id[block_id] = len(result)
            result.append(dict(item))
        else:
            current_state = result[index].get('deliveryState')
            next_state = item.get('deliveryState')
            # A stale executor snapshot cannot demote a confirmed delivery or
            # an already queued next-Turn fallback. Both resolve 'pending'.
            if current_state in {'delivered', 'queued'} and next_state == 'pending':
                continue
            if current_state in {'delivered', 'queued'} and next_state in {'delivered', 'queued'} and current_state != next_state:
                continue
            result[index] = {**result[index], **item}
    return result


def _projection_provenance(task: dict[str, Any], previous: Any) -> Any:
    provenance = dict(previous) if isinstance(previous, dict) else {}
    for source, target in _PROJECTION_PROVENANCE_FIELDS:
        if task.get(source) is not None:
            value = task[source]
            if isinstance(value, dict):
                provenance[target] = dict(value)
            elif isinstance(value, list):
                provenance[target] = [
                    dict(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                provenance[target] = value
    if len(provenance) <= (1 if provenance.get('blockId') else 0):
        return previous
    provenance['blockId'] = 'provenance'
    return provenance


def _file_changes_block(
    task_id: str,
    projection: dict[str, Any],
) -> dict[str, Any] | None:
    """Derive the stable ``fileChanges`` block from a projection's file list.

    Shared by ``_task_projection`` (live/terminal fold) and
    ``apply_commit_round_file_changes`` (post-settlement fold) so both paths
    produce the identical block identity, count rule, and undo/redo state
    carry-over.
    """
    modified_file_list = projection.get('modifiedFileList')
    if not isinstance(modified_file_list, list):
        return None
    modified_file_count = projection.get('modifiedFiles')
    if (not isinstance(modified_file_count, int)
            or isinstance(modified_file_count, bool)
            or modified_file_count < 0):
        modified_file_count = 0
    previous_file_changes = projection.get('fileChanges')
    previous_file_changes = (
        previous_file_changes
        if isinstance(previous_file_changes, dict) else {}
    )
    next_files = [dict(item) if isinstance(item, dict) else item
                  for item in modified_file_list]
    from lib.conversation_sync.file_change_policy import local_undo_available
    truncated = bool(projection.get('modifiedFilesTruncated'))
    same_operation = bool(
        task_id
        and previous_file_changes.get('taskId') == task_id
        and previous_file_changes.get('files') == next_files
    )
    file_changes = {
        'blockId': 'file-changes',
        **({'taskId': task_id} if task_id else {}),
        'count': max(modified_file_count, len(modified_file_list)),
        'state': 'applied',
        'files': next_files,
        **({'undoAvailable': False} if not local_undo_available(next_files, truncated=truncated) else {}),
        **({'truncated': True} if truncated else {}),
    }
    if same_operation:
        for key in ('state', 'commandId', 'error', 'effect'):
            if key in previous_file_changes:
                file_changes[key] = previous_file_changes[key]
    return file_changes


def task_event_projection_is_progress(event):
    """The projection owner alone classifies shareable progress snapshots."""
    return event.get('type') in _COALESCIBLE_EVENT_KINDS


def capture_task_projection_source(task, event, *, previous_source=None):
    """Freeze only projection inputs, excluding context, sockets and locks.

    Progress frames share the previous detached structural graph. Structural
    events replace it; live task mutation can never change an accepted frame.
    The worker rebases these inputs against the actual committed projection.
    """
    from copy import deepcopy
    progress = task_event_projection_is_progress(event)
    if progress and previous_source is not None:
        source = dict(previous_source)
    else:
        keys = {
            'id', 'taskId', '_attemptId', 'attemptId', '_turnActor', '_turnStatus',
            'status', '_turnVisibleRunTurnIds', '_checkpointToolRounds', '_mcpImages',
            'toolRounds', 'apiRounds', 'error', '_activeModelRequestSpan',
            '_timingStatusHistory', '_timingStatusDroppedCount',
        }
        keys.update(key for key, _ in _PROJECTION_TASK_FIELDS)
        keys.update(key for key, _, _ in _PROJECTION_INJECTION_LANES)
        keys.update(key for key, _ in _PROJECTION_PROVENANCE_FIELDS)
        values = {key: task[key] for key in keys if key in task}
        if 'apiRounds' in values:
            values['apiRounds'] = sanitize_api_rounds_for_persist(values['apiRounds'])
        if 'toolRounds' in values:
            values['toolRounds'] = compact_tool_rounds_for_frame_budget([
                trim_tool_round_for_persist(dict(row)) if isinstance(row, dict) else row
                for row in values['toolRounds']])
        source = deepcopy(values)
        if task.get('_todoState'):
            # The owned checklist changes during tool execution, before any
            # terminal projection. Structural round receipts must preserve it
            # so a hard restart never needs to replay a completed tool.
            from lib.tools.todo import public_todo_state
            source['todoState'] = public_todo_state(task['_todoState'])
        source['config'] = deepcopy({'checkpointToolRounds':
            (task.get('config') or {}).get('checkpointToolRounds', [])})
    from lib.tasks_pkg.manager._events import snapshot_task_text
    source['content'], source['thinking'], _ = snapshot_task_text(task)
    source['phase'] = deepcopy(task.get('phase'))
    return source


def _task_projection(
    task: dict[str, Any],
    previous: dict[str, Any],
    raw_event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    projection = dict(previous)
    cfg = task.get('config') or {}
    # Flow phases commit their visible rows independently through
    # ``sync_visible_run_turns``.  Later orchestration bookkeeping events must
    # not fold the aggregate task buffer back over the first phase's bubble.
    # The per-node flow_iteration reset clears task['thinking'] as well, so
    # the first turn's committed thinking needs the same guard as content.
    owns_visible_run_turns = bool(task.get('_turnVisibleRunTurnIds'))
    # cfg['contentPrefix'] is deliberately NOT a fallback here: it is a
    # one-time display seed applied to task['content'] at resume hydration
    # (apply_resume_state). Falling back to it re-projects the interrupted
    # attempt's stale prose whenever _discard_pretool_prose empties the
    # accumulator mid-run — the resumed turn then shows the old blob as its
    # terminal answer until finalize (conv mto8xq9we7xc6o, 2026-09-05).
    content = (projection.get('content', '') if owns_visible_run_turns else
               (task.get('content') or ''))
    checkpoint_rounds = (task.get('_checkpointToolRounds')
                         or cfg.get('checkpointToolRounds') or [])
    attempt_id = task.get('_attemptId') or task.get('attemptId') or ''
    task_id = task.get('id') or task.get('taskId') or ''
    # A Turn can outlive several executors. Preserve checkpoint ownership and
    # stamp unowned fresh rounds before the durable projection boundary.
    # Delegated producer stamps remain intact; display attempts are not their
    # execution identity. Logical counters continue from the retained frontier;
    # historical local-counter rows keep their exact producer scope.
    merged_rounds = tool_rounds_with_execution_identity(
        checkpoint_rounds, attempt_id='', task_id='',
    ) + tool_rounds_with_execution_identity(
        task.get('toolRounds') or [],
        attempt_id=attempt_id,
        task_id=task_id if attempt_id else '',
        overwrite=bool(attempt_id),
    )
    if owns_visible_run_turns:
        merged_rounds = previous.get('toolRounds') or []
    projected_rounds = [
        trim_tool_round_for_persist(dict(item))
        if isinstance(item, dict) else item
        for item in merged_rounds
    ]
    # Keep the durable document below one storage frame: without this cap a
    # long tool-heavy turn grows past the wire limit and every authoritative
    # write starts failing closed (task mtdx825fjmhmx5: 379 rejected frames).
    projected_rounds = compact_tool_rounds_for_frame_budget(projected_rounds)
    projection.update({
        'content': content,
        'thinking': (projection.get('thinking', '') if owns_visible_run_turns
                     else (task.get('thinking')
                           if task.get('thinking') is not None
                           else projection.get('thinking', ''))),
        'toolRounds': projected_rounds,
    })

    # New executors always own this field. Its presence intentionally clears
    # stale images on regenerate, while checkpoint/continue initialization
    # preserves prior refs before appending new MCP result images.
    if task.get('_mcpImages') is not None:
        projection['images'] = list(task.get('_mcpImages') or [])
    for source, target in _PROJECTION_TASK_FIELDS:
        if owns_visible_run_turns and source in {
            'segments', 'modifiedFiles', 'modifiedFileList', 'modifiedFilesTruncated', 'todoState',
        }:
            continue
        if task.get(source) is not None:
            projection[target] = task[source]
    if not owns_visible_run_turns and task.get('_todoState'):
        from lib.tools.todo import public_todo_state
        projection['todoState'] = public_todo_state(task['_todoState'])
    # Display-only injection records are copied and normalized independently:
    # unlike content/tool facts, their stable render identity is part of the
    # projection contract and must survive hydration.  Normalize previous
    # projections too so the next event upgrades rows written before blockId.
    for source, target, channel in _PROJECTION_INJECTION_LANES:
        records = (task[source] if task.get(source) is not None
                   else projection.get(target))
        if source == '_bgCommandInjects' and (
                records is not None or previous.get(target) is not None
                or (raw_event and raw_event.get('type') == 'background_command_inject')):
            # A lost commit ACK can leave the executor behind durable receipt
            # state. Never let its older sidecar erase a committed delivery.
            committed = _projection_injection_records(previous.get(target), channel)
            local = _projection_injection_records(records, channel)
            records = list(committed) if isinstance(committed, list) else []
            known = {item['blockId'] for item in records
                     if isinstance(item, dict) and item.get('blockId')}
            for item in local if isinstance(local, list) else []:
                if not isinstance(item, dict) or item.get('blockId') not in known:
                    records.append(item)
            if raw_event and raw_event.get('type') == 'background_command_inject':
                from lib.background_command_projection import background_command_injection_record
                receipt = background_command_injection_record(
                    raw_event, attempt_id=str(attempt_id))
                records = [item for item in records if not isinstance(item, dict)
                           or item.get('blockId') != receipt['blockId']]
                records.append(receipt)
        if source == '_userSteerInjects' and records is not None:
            records = _merge_user_steer_records(previous.get(target), records)
        if records is not None:
            projection[target] = _projection_injection_records(records, channel)
    provenance = _projection_provenance(task, projection.get('provenance'))
    if provenance is not None:
        projection['provenance'] = provenance
    # The legacy counter/list remain readable during migration, but the
    # renderer consumes one explicit, stable content block.  Derive it at the
    # projection authority so every client sees the same identity and list.
    file_changes = _file_changes_block(str(task.get('id') or ''), projection)
    if file_changes is not None:
        projection['fileChanges'] = file_changes
    # Per-round usage breakdown. The context-health gauge's "last round
    # prompt" reading is only honest when per-round usage survives a reload;
    # without it the frontend can only divide the turn's ACCUMULATED bill by
    # one and presents the whole turn's cost as a single prompt
    # (2026-08-20 fake "1.3M / 1.1M = 100%" reading on a ~170k prompt).
    # Sanitize like the legacy persist path so ``_wire_*`` diagnostics never
    # reach durable rows (measured GiB-class bloat in round_usage storage).
    if task.get('apiRounds'):
        projection['apiRounds'] = sanitize_api_rounds_for_persist(
            task['apiRounds'])
    # Authoritative settled-cost snapshot: ONE top-level total the finish
    # footer / cost popover read; apiRounds stays the per-round ledger.
    # Mirrors the done-event stamp in orchestrator/_finalize so live and
    # reload paths sum each API round under its own model/provider/tier.
    # Without this fold a reloaded projection carried usage but no cost, and
    # the footer fell back to a client-side batch lookup whose async landing
    # was diffed away by the surface footer compare (2026-08-29,
    # mtd9ci53zq3xfm: no hover cost breakdown after reload).
    if projection.get('usage'):
        try:
            from lib.cost import compute_api_rounds_cost, compute_cost
            _fallback_model = str(
                projection.get('model') or task.get('model') or '')
            _fallback_provider = task.get('provider_id') or None
            _settled_cost = compute_api_rounds_cost(
                projection.get('apiRounds'),
                fallback_model_id=_fallback_model,
                fallback_provider_id=_fallback_provider,
                use_frozen_costs=task.get('_nativeFrozenCostProjection') is True,
            )
            if _settled_cost is None and task.get('_nativeFrozenCostProjection') is not True:
                _settled_cost = compute_cost(
                    projection['usage'],
                    model_id=_fallback_model,
                    provider_id=_fallback_provider,
                )
            if _settled_cost:
                projection['cost'] = _settled_cost
        except Exception as _cost_exc:
            logger.debug('[TurnLifecycle] settled-cost fold failed: %s',
                         _cost_exc)
    if raw_event is not None:
        # Runtime events remain the canonical facts.  The public Turn owns one
        # bounded, replay-safe presentation projection so a refresh preserves
        # exactly when a tool was isolated, a model switched, or an error
        # occurred without putting any of those diagnostics into LLM context.
        from lib.turn_activity_timeline import fold_activity_timeline
        activity_timeline = fold_activity_timeline(
            projection.get('activityTimeline'), raw_event, task,
        )
        if activity_timeline is not None:
            projection['activityTimeline'] = activity_timeline
    # The live status remains transient for rendering, but its bounded history
    # is durable diagnostic evidence. Provider-ingress isolation may keep an
    # individual phase frame memory-local; this cumulative projection crosses
    # the next authoritative boundary and prevents that user-visible prompt
    # from disappearing from postmortem analysis.
    from lib.tasks_pkg.turn_trace import project_running_trace_status
    projection = project_running_trace_status(projection, task)
    return projection_with_stable_segments(
        projection,
        actor=str(task.get('_turnActor') or 'assistant'),
        status=str(task.get('_turnStatus') or task.get('status') or 'running'),
    )


def _supports_lossless_prefill(task: dict[str, Any], projection: dict[str, Any]) -> bool:
    if not projection.get('content'):
        return False
    model = task.get('model') or (task.get('config') or {}).get('model') or ''
    if not model:
        return False
    try:
        from lib.model_info import model_supports_assistant_prefill
        return bool(model_supports_assistant_prefill(model))
    except Exception as exc:
        logger.debug('[TurnLifecycle] prefill capability probe failed: %s', exc)
        return False


def automatic_recovery_blocker(error: Any) -> dict[str, Any] | None:
    """Return typed evidence that replay cannot resolve a terminal failure.

    ``retryable`` permits an eventual caller-initiated retry, not an immediate
    unattended retry. Keep this policy shared by settlement and swarm wakes;
    never infer authority from human/model prose.
    """
    if not isinstance(error, Mapping):
        return None
    from lib.tool_results.tool_rejection import tool_rejection_descriptor

    rejection = tool_rejection_descriptor(error)
    if rejection and rejection.get('kind') == 'git_guard':
        return {
            'kind': 'git_guard',
            'condition': rejection.get('condition') or 'git_guard_condition_changed',
            'retryPolicy': {'mode': 'after_condition_change', 'automatic': False},
        }
    policy = error.get('retryPolicy')
    if isinstance(policy, Mapping) and policy.get('automatic') is False:
        return dict(error)
    # A request that cannot be represented will fail before model execution;
    # completed children do not change its protocol or continuity constraints.
    protocol_failure = (
        error.get('context') == 'request-prepare'
        and any(str(error.get(field) or '').startswith('protocol_conversion:')
                for field in ('detail', 'raw', 'code'))
    )
    if (protocol_failure or error.get('autoRetryExhausted')
            or (error.get('retryable') is False
                and error.get('code') != 'task_start_failed')):
        return {
            **error,
            'retryPolicy': {'mode': 'manual', 'automatic': False},
        }
    return None


def _settlement(task: dict[str, Any], raw_event: dict[str, Any],
                projection: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    terminal_evidence = task_terminal_evidence(task, raw_event)
    finish = terminal_evidence.finish_reason
    raw_error = raw_event.get('error') or task.get('error')
    stream_state = terminal_evidence.stream_state
    verdict = derive_turn_verdict(terminal_evidence)
    status = verdict.status.value
    outcome = verdict.outcome.value
    cause = verdict.cause
    error = None
    if status == 'failed':
        error = normalize_envelope(
            raw_error,
            context='turn-settlement',
            source='turn-lifecycle',
            require_complete=True,
        )
        if error is None:
            # A terminal error frame without its payload is itself a contract
            # failure. Persist an actionable envelope here at the authority
            # boundary so neither snapshots nor reconnect replay can degrade
            # to the opaque policy cause ``generation_error``.
            detail = str(
                raw_event.get('detail')
                or raw_event.get('content')
                or finish
                or 'Terminal generation event contained no error detail.'
            )[:300]
            model = str(
                task.get('model') or (task.get('config') or {}).get('model') or '')
            stream_error_kind = (
                str(finish)
                if finish in {'premature_close', 'abnormal_stop'}
                else 'premature_close'
                if cause == 'provider_stream_error'
                else 'storage_fence'
                if cause == 'internal_fence_abort'
                else 'internal'
                if cause == 'completion_evidence_missing'
                else 'generic'
            )
            error = make_envelope(
                stream_error_kind,
                detail=detail, model=model,
                context='turn-settlement', source='turn-lifecycle',
                raw=detail,
            )
    blocker = automatic_recovery_blocker(raw_error)
    if error is not None and blocker is not None:
        error['retryPolicy'] = dict(blocker['retryPolicy'])
        error['autoRetryExhausted'] = True
        if blocker.get('condition'):
            error['condition'] = blocker['condition']
    options: list[dict[str, Any]] = []
    checkpoint_prefix = scan_replayable_tool_round_prefix(
        projection.get('toolRounds') or [])
    resumable = status in {'interrupted', 'truncated', 'failed'}
    if resumable and _supports_lossless_prefill(task, projection):
        options.append({
            'operation': 'continue',
            'anchor': {'type': 'lossless_prefill',
                       'contentChars': len(projection.get('content') or '')},
        })
    elif (resumable and not projection.get('content')
            and (checkpoint_prefix.rounds or projection.get('thinking'))):
        # No prose tail to prefill, so continuing needs no prefill
        # capability at all: the replayed checkpoint prefix is the wire
        # continuity and the write boundary preserves the interrupted
        # thinking tail as a rolled-back block. Offering only
        # checkpoint_resume here would force a needless projection rewrite.
        options.append({
            'operation': 'continue',
            'anchor': {'type': 'replay_only', 'contentChars': 0},
        })
    if (resumable and checkpoint_prefix.rounds):
        # Retention is wider than replay: the durable projection keeps every
        # pre-gap row (display carriers included) so a resume never erases
        # rendered history; only discarded provider-attempt artifacts are
        # filtered.  Replay still uses ``checkpoint_prefix`` alone.
        kept_boundary, retained_positions = checkpoint_retention_positions(
            projection.get('toolRounds') or [], checkpoint_prefix)
        options.append({
            'operation': 'checkpoint_resume',
            'anchor': {
                'type': 'tool_checkpoint',
                # ``keptToolRounds`` is the raw retention boundary (all rows
                # before the first causal gap).  Positions are the semantic
                # authority: they omit only discarded provider-attempt
                # artifacts.
                'keptToolRounds': kept_boundary,
                'replayableToolRounds': len(checkpoint_prefix.rounds),
                'retainedToolRoundPositions': retained_positions,
                # Terminal lanes restart empty: the attempt-creation
                # boundary moves the interrupted content/thinking tail into
                # ``projection.rolledBack`` instead of seeding it back. A
                # checkpoint resume regenerates the tail on the wire, so a
                # seed would display text the model never produced and then
                # wipe it.
                'content': '', 'thinking': '', 'segments': [],
            },
        })
    if (status in {'interrupted', 'truncated', 'failed'}
            and checkpoint_prefix.blocked_position is not None
            and checkpoint_prefix.blocked_reason == 'missing_tool_result'):
        raw_rounds = projection.get('toolRounds') or []
        gap_round = (
            raw_rounds[checkpoint_prefix.blocked_position]
            if checkpoint_prefix.blocked_position < len(raw_rounds) else None
        )
        # A turn that died while blocked on ask_human persists the question
        # round with no result. Offer the late-answer resume: the user can
        # still complete THAT tool call and continue the loop from it,
        # instead of letting a plain continue amputate the question and
        # making the model re-ask.
        if (isinstance(gap_round, Mapping)
                and gap_round.get('toolName') == 'ask_human'
                and gap_round.get('status') == 'awaiting_human'
                and isinstance(gap_round.get('guidanceId'), str)
                and gap_round['guidanceId']):
            kept_boundary, retained_positions = checkpoint_retention_positions(
                raw_rounds, checkpoint_prefix)
            options.append({
                'operation': 'answer_guidance',
                'anchor': {
                    'type': 'human_guidance',
                    'guidanceId': gap_round['guidanceId'],
                    'toolCallId': str(gap_round.get('toolCallId') or ''),
                    'question': str(gap_round.get('guidanceQuestion') or ''),
                    'responseType': str(gap_round.get('guidanceType') or 'free_text'),
                    'roundPosition': checkpoint_prefix.blocked_position,
                    'keptToolRounds': kept_boundary,
                    'retainedToolRoundPositions': retained_positions,
                },
            })
    options.append({'operation': 'regenerate', 'anchor': {'type': 'turn_start'}})
    if error is not None:
        # Backstop: EVERY durable failure record carries structured
        # attribution (round / tool / phase) when any execution context is
        # known, even if the failing seam did not stamp its own.
        error = ensure_failure_attribution(error, task, projection=projection)
    settlement = {
        'outcome': outcome,
        'cause': cause,
        'evidence': verdict.evidence.value,
        'streamState': stream_state.value if stream_state is not None else None,
        'providerFinishReason': finish or None,
        'error': error,
        'resumeOptions': options,
    }
    retry_chain = turn_retry_chain(task)
    if retry_chain:
        # Cross-attempt traceability for transparent whole-turn auto-retries:
        # attempt N's discarded error is linked to the retry decisions that
        # followed it, so a settled turn never shows an orphaned failure.
        settlement['retryChain'] = retry_chain
    if task.get('_nextAttemptId'):
        settlement['continuation'] = {
            'turnId': task.get('_nextTurnId') or '',
            'attemptId': task['_nextAttemptId'],
        }
    return status, settlement


_INTERACTION_EVENTS = frozenset({
    'stdin_request', 'human_guidance_request', 'write_approval_request',
    'ask_human', 'approval_request',
})
# Raw frames preserved inside their durable attempt event (never the
# projection) so the frontend debug surfaces can rebuild without a separate
# task-event stream. Kept deliberately narrow: only frames whose consumers
# lost their transport in the conversation-sync migration.
_DEBUG_EVIDENCE_EVENTS = frozenset({'messages_snapshot', 'tool_compacted'})
_TERMINAL_EVENTS = frozenset({'done', 'error', 'aborted'})


def _signal_stale_attempt_abort(task: dict[str, Any], attempt_id: str,
                                reason: str) -> None:
    """Plant the cooperative abort triple when a turn-native event is rejected because
    its attempt is definitively stale (settled or superseded).

    Without this the worker (agent round loop / LLM stream / tool heartbeat)
    never learns its writes are being discarded and keeps emitting events for
    days — each one a rejected authoritative write + an ERROR log row (the
    120k-row 'turn-native event rejected: attempt is stale' flood). CAS-contention
    rejections are transient and must NOT come through here; only call this on
    the branches where the attempt row itself proves staleness.

    The stamp is EXACTLY the one the append_event fence
    (lib/tasks_pkg/manager/_events.py::_persist_before_push) plants and the
    round-start gate / abort_check consume: ``aborted`` + ``_abort_timestamp``
    + ``_abort_reason='turn_attempt_stale_fence'``. A divergent key set would
    both break that contract and, by pre-setting ``aborted``, suppress the
    fence's own stamp. First stamp wins — a prior user abort must never be
    clobbered.
    """
    if task.get('aborted'):
        return
    logger.warning('[TurnLifecycle] turn-native attempt stale (%s); signaling abort '
                   'task=%s attempt=%s', reason,
                   (task.get('id') or '?')[:8], (attempt_id or '?')[:8])
    try:
        task['aborted'] = True
        task['_abort_timestamp'] = time.time()
        task['_abort_reason'] = 'turn_attempt_stale_fence'
        abort_event = task.get('abort_event')
        if abort_event is not None:
            abort_event.set()
    except Exception:
        logger.debug('[TurnLifecycle] stale-attempt abort signal failed '
                     '(non-fatal)', exc_info=True)


def _is_frame_overflow_error(exc: 'StorageError') -> bool:
    """Both deterministic fences that make one authoritative frame unwritable.

    ``storage_payload_too_large`` is the sidecar payload cap; the 64 MiB wire
    frame cap (lib/storage/protocol.py::MAX_FRAME_BYTES) is enforced by the
    client encoder before the command leaves the process, surfacing as a
    ``database_protocol_error`` whose message names the frame limit. Retrying
    either shape with the same projection can never succeed.
    """
    if exc.code == 'storage_payload_too_large':
        return True
    return (exc.code == 'database_protocol_error'
            and 'frame exceeds the size limit' in str(exc).lower())


def _signal_frame_overflow_abort(task: dict[str, Any], attempt_id: str,
                                 event_kind: str) -> None:
    """Plant the cooperative abort triple when even a text-only frame is
    unwritable, so the worker stops emitting events nothing can persist.

    Same stamp contract as the stale-attempt fence (``aborted`` +
    ``_abort_timestamp`` + ``_abort_reason``); a prior user abort must never
    be clobbered. The turn itself settles via the normal reaper lane — no
    write path can record a terminal frame bigger than the wire cap.
    """
    if task.get('aborted'):
        return
    logger.error('[TurnLifecycle] unwritable frame for task=%s attempt=%s '
                 'event=%s; signaling abort (storage_frame_overflow)',
                 str(task.get('id') or '?')[:8], str(attempt_id or '?')[:8],
                 event_kind)
    try:
        task['aborted'] = True
        task['_abort_timestamp'] = time.time()
        task['_abort_reason'] = 'storage_frame_overflow'
        abort_event = task.get('abort_event')
        if abort_event is not None:
            abort_event.set()
    except Exception:
        logger.debug('[TurnLifecycle] frame-overflow abort signal failed '
                     '(non-fatal)', exc_info=True)


def _signal_authority_integrity_abort(task: dict[str, Any], attempt_id: str,
                                      event_kind: str,
                                      *, detail: str = '') -> None:
    """Stop work after a deterministic corruption fence rejects authority.

    Retrying ``database_integrity`` on every stream/tool event cannot recover
    the Turn and previously let an invisible worker spend model rounds while
    thousands of durable-before-visible frames were withheld. The normal
    recovery/reaper boundary owns settlement; this signal only stops further
    expensive execution.
    """
    if task.get('aborted'):
        return
    logger.error(
        '[TurnLifecycle] conversation authority integrity failure for task=%s '
        'attempt=%s event=%s; signaling cooperative abort (authority detail: '
        '%s)',
        str(task.get('id') or '?')[:8], str(attempt_id or '?')[:8], event_kind,
        detail or 'no authority diagnostic',
    )
    try:
        task['aborted'] = True
        task['_abort_timestamp'] = time.time()
        task['_abort_reason'] = 'storage_authority_integrity'
        abort_event = task.get('abort_event')
        if abort_event is not None:
            abort_event.set()
    except Exception:
        logger.debug('[TurnLifecycle] authority-integrity abort signal failed '
                     '(non-fatal)', exc_info=True)

def _drain_queue_after_settlement(task: dict[str, Any], conversation_id: str,
                                  status: str) -> None:
    """Drain the conversation's durable message queue once a turn-native attempt
    settles and its lane is provably free.

    The legacy (v1) drain rides persist_task_result → _dispatch_queued_message,
    which fires BEFORE the done event — the turn-native attempt row is still live at
    that point, so a create_turn_pair there would lane_busy-fail. turn-native
    conversations therefore drain HERE, immediately after the terminal
    settlement commits. Best-effort: any failure leaves the row leased for
    the reaper's next tick, never dropped.
    """
    try:
        # Failure reports can wake the explicit Coordinator continuation policy,
        # while ordinary human/background queues keep their skip-on-error rule.
        if status not in ('completed', 'interrupted', 'truncated', 'failed'):
            return
        if not conversation_id:
            return
        # An autopilot/continuation successor already owns the lane — the
        # queued human turn drains when THAT successor settles instead.
        if (task.get('_autopilot_spawned_followup')
                or task.get('_autopilotNextAttempt')):
            return
        from lib.tasks_pkg.manager import task_user_id
        owner_user_id = int(task_user_id(task))
        from lib.message_queue import get_queue_depth
        has_queued_work = status != 'failed' and get_queue_depth(
            conversation_id, user_id=owner_user_id) > 0
        import threading

        def _drain():
            try:
                if status == 'failed':
                    from lib.project_dispatch.recovery import recover_conversation_dispatch
                    recover_conversation_dispatch(conversation_id, user_id=owner_user_id, coordinator_only=True)
                    return
                from lib.message_queue import (dispatch_next_queued,
                                               dispatch_completed_background_batch)
                new_task_id = (dispatch_next_queued(
                    conversation_id, user_id=owner_user_id)
                    if has_queued_work else None)
                if not new_task_id:
                    new_task_id = dispatch_completed_background_batch(
                        conversation_id, user_id=owner_user_id)
                if new_task_id:
                    logger.info('[Queue] turn-native settlement drain dispatched '
                                'queued message → task %s for conv=%s',
                                new_task_id[:8], conversation_id[:8])
                else:
                    from lib.project_dispatch.recovery import recover_conversation_dispatch
                    recover_conversation_dispatch(
                        conversation_id, user_id=owner_user_id)
            except Exception as exc:
                logger.debug('[Queue] turn-native settlement drain failed conv=%s: %s',
                             conversation_id[:8], exc)

        threading.Thread(
            target=_drain, daemon=True,
            name=f'turn-native-queue-drain-{conversation_id[:8]}').start()
    except Exception:
        logger.debug('[Queue] turn-native settlement drain hook failed', exc_info=True)


_TURN_PROJECTION_STATE_KEY = '_turnProjectionState'
_TURN_PROJECTION_LOCK_KEY = '_turnProjectionStateLock'


def _task_projection_state_lock(task: dict[str, Any]) -> threading.RLock:
    """Return one task-local lock for projection revision/cache ownership."""
    existing = task.get(_TURN_PROJECTION_LOCK_KEY)
    if hasattr(existing, 'acquire') and hasattr(existing, 'release'):
        return existing
    candidate = threading.RLock()
    return task.setdefault(_TURN_PROJECTION_LOCK_KEY, candidate)


def _task_projection_state(
    task: dict[str, Any], attempt_id: str, user_id: int,
) -> dict[str, Any] | None:
    """Return the validated last-applied Turn state for this executor."""
    state = task.get(_TURN_PROJECTION_STATE_KEY)
    if not isinstance(state, dict):
        return None
    revision = state.get('projectionRevision')
    projection = state.get('projection')
    if (state.get('attemptId') != attempt_id
            or state.get('userId') != user_id
            or not isinstance(revision, int) or isinstance(revision, bool)
            or revision < 0 or not isinstance(projection, dict)
            or not isinstance(state.get('segmentsStable', True), bool)):
        return None
    task_turn_id = str(task.get('_turnId') or '')
    if task_turn_id and state.get('turnId') != task_turn_id:
        return None
    return state


def _remember_task_projection_state(
    task: dict[str, Any],
    *,
    attempt_id: str,
    user_id: int,
    turn: Mapping[str, Any],
    projection: dict[str, Any],
    projection_revision: int,
    segments_stable: bool = True,
) -> None:
    """Retain one bounded live baseline and its segment-normalization fence."""
    task[_TURN_PROJECTION_STATE_KEY] = {
        'attemptId': attempt_id,
        'userId': user_id,
        'conversationId': str(
            turn.get('conversationId') or task.get('convId') or ''),
        'turnId': str(turn.get('turnId') or task.get('_turnId') or ''),
        'actor': str(turn.get('actor') or task.get('_turnActor') or 'assistant'),
        'projectionRevision': projection_revision,
        'projection': projection,
        'segmentsStable': segments_stable,
    }


def record_task_event(task: dict[str, Any], raw_event: dict[str, Any],
                      task_event: dict[str, Any] | None = None,
                      *,
                      fence_out: dict[str, Any] | None = None,
                      projection_source: dict[str, Any] | None = None):
    """Persist one task projection/event before it becomes client-visible.

    Returns False for a task without a turn attempt, a stale attempt, a
    duplicate terminal event, superseded executor, or rejected live write.
    A retained ``_terminalTurnPending`` receipt distinguishes a rejected live
    terminal write from a durable terminal/superseded witness.

    Returns the string ``'coalesced'`` (truthy, NOT a rejection) when a pure
    text ``delta`` is folded into the next durable write by the coalescing
    window — see the module-level note.  The frame's content is not lost:
    the next persisted event (any structural frame, the window's first due
    delta, or the terminal settlement) carries the cumulative projection.

    When ``task_event`` (``{task_id, sequence, event}``) is attached and the
    sidecar path applies, the frame's storage_events row commits INSIDE the
    turn authority transaction and the return is the string ``'carried'`` —
    one frame = one authority transaction (2026-08-20 double-write root
    fix). Other non-carried outcomes leave the event row to the caller's
    standalone append, except a rejected live terminal write: its retained
    receipt must be retried atomically, never appended/pushed independently.

    On the ``'coalesced'`` outcome the attempt-liveness fence does NOT
    execute here; when ``fence_out`` is supplied it receives
    ``require_attempts_live`` — the attempt ids the caller's standalone
    append must have the storage authority verify inside the append
    transaction (2026-09-18 stream-churn fix; see the coalesced branch).
    """
    with _task_projection_state_lock(task):
        if task.get('_flowLiveTurn') and raw_event.get('type') not in _TERMINAL_EVENTS:
            from lib.orchestration.chat_live_turn import project_live_flow_task
            projected = project_live_flow_task(task)
            try:
                return _record_task_event_locked(
                    projected, raw_event, task_event, fence_out=fence_out)
            finally:
                task['_flowLiveTurn']['projectionState'] = projected.get(_TURN_PROJECTION_STATE_KEY)
        terminal = raw_event.get('type') in _TERMINAL_EVENTS
        attempt_id = task.get('_attemptId') or task.get('attemptId')
        if terminal and attempt_id:
            # Transfer ownership BEFORE the write, including explicit rejection
            # and ambiguous ACKs. Reuse the task-result retry/eviction fence;
            # only authority evidence can discharge this one retained receipt.
            from copy import deepcopy
            task['_terminalTurnPending'] = (
                deepcopy(raw_event), deepcopy(task_event))
            task['_terminalPersistencePending'] = True
            task['_terminalPersistenceRetryReady'] = True
        try:
            outcome = _record_task_event_locked(
                task, raw_event, task_event, fence_out=fence_out,
                projection_source=projection_source)
            if terminal and attempt_id:
                settled = bool(outcome)
                if not settled:
                    from lib.tasks_pkg.manager._registry import task_user_id
                    # False can also be a live CAS rejection. Never mistake it
                    # for a durable terminal/superseded witness; a failed read
                    # likewise leaves the receipt owned by maintenance.
                    attempt = get_attempt(attempt_id, user_id=task_user_id(task))
                    settled = attempt['status'] not in LIVE_ATTEMPT_STATUSES
                if settled:
                    task.pop('_terminalTurnPending', None)
            return outcome
        except Exception as exc:
            if (isinstance(exc, StorageError)
                    and exc.code == 'database_protocol_error'):
                # Deterministic engine rejection (InvalidPayload): retrying
                # the identical request shape can never succeed, and the
                # daemon echoes only the code as the message — so the
                # client-side request shape logged here is the only record
                # of WHICH validation fence fired.
                from lib.tasks_pkg.manager._registry import task_user_id
                state = (_task_projection_state(
                    task, attempt_id, task_user_id(task))
                    if attempt_id else None)
                logger.warning(
                    '[TurnLifecycle] deterministic event-record rejection '
                    'task=%s attempt=%s kind=%s terminal=%s client_rev=%s',
                    str(task.get('id') or '?')[:8],
                    str(attempt_id or '?')[:8],
                    raw_event.get('type'), terminal,
                    (state or {}).get('projectionRevision'))
            if terminal and attempt_id:
                # A timeout may follow a committed transaction. Re-read the
                # attempt on retry before rebuilding any projection patch.
                task.pop(_TURN_PROJECTION_STATE_KEY, None)
            raise


def ensure_terminal_task_settled(task: dict[str, Any]) -> bool:
    """Discharge terminal Turn debt before executor diagnostics can succeed.

    Finalizers normally supply the fully enriched terminal event first. Direct
    terminal persistence callers still owe settlement: reconstruct its terminal
    facts from the retained task rather than treating task_results as authority.
    No new receipt/state machine is needed; maintenance re-enters this boundary.
    """
    with _task_projection_state_lock(task):
        pending = task.get('_terminalTurnPending')
        if pending:
            event, carrier = pending
        else:
            from lib.tasks_pkg.manager._registry import task_user_id
            attempt_id = task.get('_attemptId') or task.get('attemptId')
            attempt = get_attempt(attempt_id, user_id=task_user_id(task))
            if attempt['status'] not in LIVE_ATTEMPT_STATUSES:
                return True
            event = {'type': 'done', 'finishReason': task.get('finishReason') or 'unknown'}
            if task.get('error') is not None:
                event['error'] = task['error']
            carrier = None
        record_task_event(task, event, task_event=carrier)
        settled = not task.get('_terminalTurnPending')
        if settled and pending and carrier and task.get('_eventDeliveryLane') is not None:
            # An ambiguous ACK may already have committed this exact terminal
            # carrier. Confirm its immutable sequence before opening hot replay;
            # a superseded attempt without that row revokes local publication.
            from lib.tasks_pkg.event_log import read_events
            sequence = int(carrier['sequence'])
            rows = read_events(task['id'], since_event_id=sequence - 1,
                               limit=1, raise_on_error=True)
            if (rows and int(rows[0]['event_id']) == sequence
                    and rows[0]['payload'].get('type') == event.get('type')
                    and rows[0]['payload'].get('attemptId') == task.get('_attemptId')):
                from lib.tasks_pkg.event_delivery import advance_event_visibility
                advance_event_visibility(task, sequence)
                task.pop('_pushWithheldAt', None)
                task.pop('_pushWithheldCount', None)
                from lib.translate.terminal import schedule_terminal_turn_translations
                schedule_terminal_turn_translations(task)
            else:
                task['_eventDeliveryRevoked'] = True
        return settled


def _record_task_event_locked(
    task: dict[str, Any],
    raw_event: dict[str, Any],
    task_event: dict[str, Any] | None,
    *,
    allow_rebase: bool = True,
    fence_out: dict[str, Any] | None = None,
    projection_source: dict[str, Any] | None = None,
):
    """Record one event while this task owns its projection state lock."""
    attempt_id = task.get('_attemptId') or task.get('attemptId')
    if not attempt_id:
        return False
    from lib.tasks_pkg.manager._registry import task_user_id
    user_id = task_user_id(task)
    source = task if projection_source is None else projection_source
    if projection_source is not None:
        source['_activityLastLlmRound'] = task.get('_activityLastLlmRound', 0)
    now = _now_ms()
    event_kind = str(raw_event.get('type') or 'projection')
    if (event_kind in _COALESCIBLE_EVENT_KINDS
            and not (task_event or {}).get('preceding_events')
            and not _delta_throttle_allows(attempt_id)):
        # Coalesced frames do not enter the write transaction that normally
        # proves the attempt fence. The fence is not dropped — it travels
        # with the frame's standalone storage_events append as
        # ``require_attempts_live``, and the storage authority enforces it
        # INSIDE the append transaction (fail-closed on a settled,
        # superseded, or missing attempt). This used to be a per-frame
        # authoritative pre-read here: one extra round trip per visible
        # delta (~10-40 reads/s per streaming task, plus a second read for
        # a Flow phase's root attempt), which queued hardest exactly when
        # the authority was wedged (2026-09-18 incident). A stale verdict
        # now surfaces as the append's ``attempt_not_live`` rejection and
        # the caller plants the identical cooperative-abort stamp.
        if fence_out is not None:
            require_live = [attempt_id]
            if task.get('_flowRootAttemptId'):
                require_live.append(task['_flowRootAttemptId'])
            fence_out['require_attempts_live'] = require_live
        return 'coalesced'
    state = _task_projection_state(task, attempt_id, user_id)
    if state is not None:
        turn = {
            'conversationId': state['conversationId'],
            'turnId': state['turnId'],
            'actor': state['actor'],
            'projectionRevision': state['projectionRevision'],
            'projection': state['projection'],
        }
    else:
        attempt = get_attempt(attempt_id, user_id=user_id)
        if attempt['status'] not in LIVE_ATTEMPT_STATUSES:
            _signal_stale_attempt_abort(task, attempt_id, 'attempt-not-live')
            return False
        turn = get_turn(
            attempt['conversationId'], attempt['turnId'], user_id=user_id)
        # Public reads retain the exact committed replay base. Only the
        # producer normalizes its application patch baseline; Sidecar does
        # the same under the revision lock and publishes any repair delta
        # from the unmodified wire document in the committed event.
        turn = {**turn, 'projection': projection_with_stable_segments(
            turn.get('projection'),
            actor=str(turn.get('actor') or 'assistant'),
            status=str(turn.get('status') or 'running'),
        )}
    previous = turn.get('projection') or {}
    terminal = event_kind in _TERMINAL_EVENTS
    try:
        oversize_retry_at = float(
            task.get('_turnProjectionOversizeRetryAt') or 0.0)
    except (TypeError, ValueError, OverflowError):
        oversize_retry_at = 0.0
    oversize_circuit_open = bool(
        task_event is not None
        and not terminal
        and time.monotonic() < oversize_retry_at
    )
    background_receipt = event_kind == 'background_command_inject'
    effect_intent = event_kind == 'tool_effect'
    slim = not terminal and not background_receipt and not effect_intent and (
        oversize_circuit_open
        or (
            event_kind in _COALESCIBLE_EVENT_KINDS
            and not _structural_fold_due(attempt_id)
        )
    )
    if slim and not oversize_circuit_open:
        content, thinking = _delta_text_fields(source, previous)
        if ((content and not previous.get('content'))
                or (thinking and not previous.get('thinking'))):
            # The first nonempty channel creates a structural render block.
            # Later tokens can patch only cumulative text; skipping this
            # boundary leaves reasoning invisible until an unrelated tool.
            slim = False
    if slim:
        content, thinking = _delta_text_fields(source, previous)
        projection = {'content': content, 'thinking': thinking}
    else:
        if state is not None and state.get('segmentsStable', True) is False:
            # Text appends retain the committed structural shape. Repair its
            # mirrors once when a structural/terminal patch needs the stable
            # baseline that the authority independently derives under CAS.
            previous = projection_with_stable_segments(
                previous,
                actor=str(turn.get('actor') or 'assistant'),
                status='running',
            )
        projection = _task_projection(source, previous, raw_event)
    if projection_source is not None and '_activityLastLlmRound' in source:
        task['_activityLastLlmRound'] = source['_activityLastLlmRound']
    settlement = {}
    status = 'running'
    error = {}
    if terminal:
        from lib.tasks_pkg.turn_trace import finalize_trace_projection
        projection = finalize_trace_projection(
            projection,
            task,
            raw_event,
            now_ms=now,
            pending_sequence=(
                task_event.get('sequence')
                if isinstance(task_event, Mapping) else None
            ),
        )
        status, settlement = _settlement(task, raw_event, projection)
        error = settlement.get('error') or {}
        # Only a successfully completed Plan-mode executor may mint executable
        # plan authority. Never infer it from arbitrary assistant prose at the
        # generic projection normalizer. A retry that fails, truncates, or no
        # longer runs in Plan Mode also clears any prior attempt's sidecar.
        projection.pop('proposedPlan', None)
        from lib.tasks_pkg.plan_mode import plan_mode_enabled
        if status == 'completed' and plan_mode_enabled(task.get('config')):
            from lib.plan_contract import proposed_plan_document
            proposed_plan = proposed_plan_document(
                content=projection.get('content'))
            if proposed_plan is not None:
                projection['proposedPlan'] = proposed_plan
        projection = projection_with_stable_segments(
            projection,
            actor=str(turn.get('actor') or task.get('_turnActor') or 'assistant'),
            status=status,
        )
    event_type = 'terminal_settlement' if terminal else (
        'interaction_request' if event_kind in _INTERACTION_EVENTS
        else 'projection_updated')
    # Live phase rides the EVENT payload, never the persisted turn
    #   projection: the projection is the durable document (a stale phase
    #   must never survive into a settled turn), while the event payload
    #   is the wire frame the frontend folds into its session slice
    #   (streamSessions) — the same discipline as v1, where task['phase']
    #   is tracked in _emit BEFORE this call so every frame carries the
    #   CURRENT phase (phase events their own, delta/terminal frames
    #   None).  None is sent explicitly so replay clears the HUD.
    _live_phase = None if terminal else source.get('phase')
    # The projection is already represented once by ``projection_patch`` in
    # the command.  It must not ride inside this event envelope too: the
    # Sidecar replaces this private command shape with its own canonical
    # revision patch before durable replay / sync capture.
    event_payload = {'phase': _live_phase}
    if terminal:
        event_payload = {'status': status, 'settlement': settlement,
                         'phase': None}
    elif event_type == 'interaction_request':
        event_payload['request'] = raw_event
    else:
        event_payload['updateKind'] = event_kind
        if event_kind in _DEBUG_EVIDENCE_EVENTS:
            # Diagnostic evidence (debug panel / Request Inspector) never
            # folds into the projection. Carrying the live frame on its own
            # durable attempt event restores the per-frame delivery that the
            # conversation-sync migration removed — replay of the attempt
            # stream re-delivers the same frame, so live and cold reconnect
            # converge. Use the LIVE wire frame, never the delta-encoded
            # storage form: that form carries prefix/tool references only the
            # event-log decoder (``snapshot_delta.rebuild_snapshots``) can
            # resolve.
            event_payload['event'] = raw_event
    command_payload = {
        'attempt_id': attempt_id, 'user_id': user_id,
        'task_id': str(task.get('id') or ''),
        'terminal': terminal, 'status': status, 'settlement': settlement,
        'error': error, 'event_type': event_type,
        'event_payload': event_payload, 'now': now,
    }
    if terminal:
        candidate = None
        task['_modelReplayPublishStatus'] = 'not_completed'
        if status in ('completed', 'truncated') and not task.get('_transientRuntime'):
            try:
                from lib.tasks_pkg.model_replay import prepare_completed_task
                candidate = prepare_completed_task(task)
            except Exception:
                task['_modelReplayPublishStatus'] = 'unavailable'
                logger.warning('[ModelReplay] preparation unavailable', exc_info=True)
        command_payload['model_continuation'] = candidate or {
            'status': task['_modelReplayPublishStatus']}
    if slim:
        # Revision-fenced text patches carry only the new suffix. The
        # authority preserves its exact structural baseline; no segment
        # normalization is implied by a text-only update.
        command_payload['projection_patch'] = build_projection_patch(
            {field: previous[field] for field in ('content', 'thinking')
             if field in previous},
            projection,
            base_revision=int(turn.get('projectionRevision') or 0),
            target_revision=int(turn.get('projectionRevision') or 0) + 1,
        )
    else:
        base_revision = int(turn.get('projectionRevision') or 0)
        command_payload['projection_patch'] = build_projection_patch(
            previous,
            projection,
            base_revision=base_revision,
            target_revision=base_revision + 1,
        )
        # ``_task_projection`` and the terminal boundary above both return a
        # canonical stable-segment document.  This private evidence lets the
        # Sidecar reuse that exact revision without re-normalizing it on the
        # next structural event; older producers omit it and safely fall back.
        command_payload['projection_segments_stable'] = True
    if background_receipt:
        receipt = projection['_bgCommandInjects'][-1]
        command_payload['background_command_ack'] = {
            'block_id': receipt['blockId'],
            'deliveries': [
                {'queue_id': preview['queueId'], 'command_id': preview.get('commandId')}
                for preview in receipt['previews'] if 'queueId' in preview
            ],
        }
    if task.get('_flowRootAttemptId'):
        command_payload['flow_root_attempt_id'] = task['_flowRootAttemptId']
    if task_event is not None:
        command_payload['task_event'] = task_event
    client = _turn_client(write=True)
    command_id = (
        f'turn-event:{attempt_id}:{now}:{event_kind}'
        f'{":rebase" if not allow_rebase else ""}'
    )
    try:
        result = client.command(
            'turn.event.record', command_payload, command_id,
            # Projection frames are high-rate executor output, not interactive
            # commands.  Putting them on the default user lane let a fleet of
            # live turns occupy all eight user-weighted queue slots and starve
            # Send / Regenerate behind multi-MiB projection writes.
            priority='event')
    except StorageError as exc:
        if (exc.code == 'turn_projection_stale'
                and allow_rebase):
            # A legitimate external projection CAS advanced the row after our
            # last event. Refresh exactly once and rebuild this same raw event
            # against that base; repeated contention still fails closed.
            task.pop(_TURN_PROJECTION_STATE_KEY, None)
            return _record_task_event_locked(
                task,
                raw_event,
                task_event,
                allow_rebase=False,
                fence_out=fence_out,
                projection_source=projection_source,
            )
        if exc.code == 'database_integrity':
            _signal_authority_integrity_abort(
                task, attempt_id, event_kind,
                detail=str(exc)[:300])
            raise
        # A payload-cap rejection is deterministic. Retrying the same full
        # projection on every subsequent progress frame once turned one
        # 10.3M-character command log into hundreds of multi-MiB serializations
        # and an 8 GiB RSS kill. If the exact raw task event is carried in this
        # transaction, retry once with the cumulative text-only projection and
        # keep a bounded probe circuit open. The raw structural fact remains
        # durable; a later full probe or terminal settlement converges the turn
        # document. Calls without a carrier still fail closed because slimming
        # those would discard their only structural fact.
        # Terminal events are not exempt: a turn whose text alone no longer
        # fits one frame still has to settle, and the slim path is the only
        # writable shape left (task mtdx825fjmhmx5 burned 2.5h emitting
        # rejected full-projection terminal frames).
        if (not _is_frame_overflow_error(exc)
                or task_event is None or slim or background_receipt or effect_intent):
            raise
        retry_count = int(task.get('_turnProjectionOversizeCount') or 0) + 1
        task['_turnProjectionOversizeCount'] = retry_count
        task['_turnProjectionOversizeRetryAt'] = (
            time.monotonic() + _OVERSIZE_PROJECTION_RETRY_SECONDS)
        logger.warning(
            '[TurnLifecycle] oversized projection for task=%s attempt=%s '
            'event=%s; carrying the exact task event with a slim projection '
            'and probing full state again in %.0fs (count=%d)',
            str(task.get('id') or '?')[:8], str(attempt_id)[:8], event_kind,
            _OVERSIZE_PROJECTION_RETRY_SECONDS, retry_count,
        )
        content, thinking = _delta_text_fields(source, previous)
        projection = {'content': content, 'thinking': thinking}
        command_payload.update({
            'event_payload': event_payload,
            'slim': True,
            'content': content,
            'thinking': thinking,
        })
        command_payload.pop('projection_patch', None)
        command_payload.pop('projection_segments_stable', None)
        if terminal:
            command_payload['model_continuation'] = {'status': 'frame_over_budget'}
        slim = True
        try:
            result = client.command(
                'turn.event.record', command_payload, command_id,
                priority='event')
        except StorageError as retry_exc:
            # Even the text-only frame was rejected: the cumulative content
            # alone exceeds one frame, every later write can only grow, and
            # the worker is now burning tokens on events nothing can persist.
            # Plant the cooperative abort triple so the round loop stops at
            # its next gate instead of repeating this failure for hours.
            if _is_frame_overflow_error(retry_exc):
                _signal_frame_overflow_abort(task, attempt_id, event_kind)
            elif retry_exc.code == 'database_integrity':
                _signal_authority_integrity_abort(
                    task, attempt_id, event_kind,
                    detail=str(retry_exc)[:300])
            raise
    applied = bool(result.get('applied'))
    preceding = (task_event or {}).get('preceding_events')
    batch_count = result.get('task_event_count')
    if applied and preceding and (not isinstance(batch_count, int)
            or isinstance(batch_count, bool) or batch_count != len(preceding) + 1):
        raise StorageError('database_protocol_error',
                           'Storage did not confirm the complete progress event batch')
    if applied and background_receipt:
        # Only committed evidence enters the executor mirror. Failed writes
        # keep the original pending items and cannot leak a confirmed block
        # through the next ordinary projection/checkpoint.
        task['_bgCommandInjects'] = projection['_bgCommandInjects']
        pending = task.get('_bgcmd_inject_pending')
        if isinstance(pending, list):
            del pending[:len(raw_event['previews'])]
            if not pending:
                task.pop('_bgcmd_inject_pending', None)
        task.pop('_bgcmd_delivery_event', None)
        queue_ids = [preview['queueId'] for preview in raw_event['previews']
                     if preview.get('queueId')]
        if queue_ids:
            from lib.agent_inbox import consume_peer
            consume_peer(str(turn['conversationId']), queue_ids)
    if applied and not terminal:
        applied_revision = result.get('projection_revision')
        if (isinstance(applied_revision, int)
                and not isinstance(applied_revision, bool)
                and applied_revision >= 0):
            applied_projection = projection
            if slim:
                applied_projection = dict(previous)
                applied_projection['content'] = content
                applied_projection['thinking'] = thinking
                # Pure text frames need only their two root strings. Keep
                # structural values unchanged and normalize lazily at the
                # next structural boundary, instead of rewalking tools and
                # segments after every acknowledged token frame.
            _remember_task_projection_state(
                task,
                attempt_id=attempt_id,
                user_id=user_id,
                turn=turn,
                projection=applied_projection,
                projection_revision=applied_revision,
                segments_stable=not slim,
            )
        else:
            task.pop(_TURN_PROJECTION_STATE_KEY, None)
    elif applied:
        # No later task frame may trust a terminal attempt as live. The
        # terminal projection is already durable and the ordinary heavy-state
        # release will drop the same baseline shortly afterward.
        task.pop(_TURN_PROJECTION_STATE_KEY, None)
    else:
        task.pop(_TURN_PROJECTION_STATE_KEY, None)
        try:
            latest_attempt = get_attempt(attempt_id, user_id=user_id)
        except LifecycleNotFound:
            latest_attempt = None
        if (latest_attempt is None
                or latest_attempt.get('status') not in LIVE_ATTEMPT_STATUSES):
            _signal_stale_attempt_abort(
                task, attempt_id, 'event-record-rejected')
    if applied and event_kind in _COALESCIBLE_EVENT_KINDS:
        _delta_throttle_stamp(attempt_id)
    if applied and not slim and not terminal:
        _structural_fold_stamp(attempt_id)
    if applied and (terminal or not slim):
        task.pop('_turnProjectionOversizeRetryAt', None)
        task.pop('_turnProjectionOversizeCount', None)
    if terminal:
        _delta_throttle_clear(attempt_id)
        _structural_fold_clear(attempt_id)
    if applied and terminal:
        task['_modelReplayPublishStatus'] = result.get(
            'model_continuation_status', 'unavailable')

        logger.info(
            '[ModelReplay] conv=%s turn=%s attempt=%s checkpoint_settled=%s',
            str(turn.get('conversationId') or ''), str(turn.get('turnId') or ''),
            str(attempt_id), task['_modelReplayPublishStatus'])
        _drain_queue_after_settlement(
            task, str(turn.get('conversationId') or ''), status)
    if applied and task_event is not None and result.get('task_event') is not None:
        return 'carried'
    return applied


def sync_visible_run_turns(task: dict[str, Any], messages: list[dict[str, Any]],
                           *, default_kind: str = 'flow_node') -> int | None:
    """Commit Flow/Autopilot visible messages as explicit turns.

    The first generated row reuses the output ``turn_id`` allocated by the
    command.  Later phase rows use deterministic identities and terminal
    synthetic attempts, so replaying the accumulated phase list is idempotent
    without consulting array tails or public task ids.
    """
    attempt_id = task.get('_attemptId')
    root_turn_id = task.get('_turnId')
    conversation_id = task.get('convId')
    if not (attempt_id and root_turn_id
            and conversation_id and messages):
        return None
    now = _now_ms()
    from lib.tasks_pkg.manager._registry import task_user_id
    user_id = task_user_id(task)
    offset = max(0, min(int(task.get('_flowSyncedCount') or 0), len(messages) - 1))
    visible_result = _turn_client(write=True).command(
        'turn.visible.sync', {
            'conversation_id': conversation_id, 'attempt_id': attempt_id,
            'root_turn_id': root_turn_id, 'messages': messages[offset:],
            'start_index': offset,
            'user_id': user_id,
            'default_kind': default_kind,
            'run_id': (task.get('config') or {}).get('runId') or attempt_id,
        }, f'turn-visible:{attempt_id}:{len(messages)}:{now}')
    visible_ids = (
        visible_result.get('visibleTurnIds')
        if isinstance(visible_result, dict)
        else visible_result
    )
    if not visible_ids:
        raise StorageError(
            'database_conflict', 'Flow phase settlement lost Turn authority')
    task['_turnVisibleRunTurnIds'] = visible_ids
    task['_flowSyncedCount'] = len(messages)
    task.pop('_flowLiveTurn', None)
    task.pop('_turnProjectionState', None)
    return None


def list_turns(conversation_id: str, *, user_id: Any,
               lane_id: str | None = None, after_ordinal: int | None = None,
               limit: int = 500, light: bool = False,
               since_ms: int | None = None,
               known_revisions: dict[str, int] | None = None) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='list conversation turns')
    limit = min(max(int(limit or 500), 1), 2000)
    client = _turn_client()
    # Delta sync (the resync-storm fix): a watermark-bearing client asks
    # for ONLY the rows changed since its last sync instead of re-
    # receiving the full multi-MB projection per conv_changed frame.
    # Filtered reads (lane/window) retain full-snapshot semantics.
    if (since_ms is not None and lane_id is None
            and after_ordinal is None):
        now_entry = _now_ms()
        # A non-positive or far-future watermark is clock confusion —
        # degrade to a full snapshot rather than trusting it.
        if 0 < int(since_ms) <= now_entry + _DELTA_MAX_SKEW_MS:
            delta = client.query(
                'turn.list_delta', {
                    'conversation_id': conversation_id,
                    'user_id': user_id,
                    'since_ms': int(since_ms),
                    **({'known_revisions': known_revisions}
                       if known_revisions else {}),
                })
            rows = [
                public_turn_with_stable_segments(row)
                for row in (delta.get('turns') or [])
            ]
            if len(rows) > limit:
                # A delta this large means the client is far behind
                # (first sync after a mass update).  Truncating would
                # silently drop rows the client never re-fetches — the
                # watermark advances past them — so degrade to the full
                # snapshot below instead.
                logger.warning(
                    '[turns] delta overflow conv=%s rows=%d limit=%d — '
                    'falling back to full snapshot',
                    conversation_id[:8], len(rows), limit)
            else:
                if light:
                    for row in rows:
                        row['projection'] = {
                            key: row['projection'][key]
                            for key in ('content', 'thinking', 'segments',
                                        'model', 'usage')
                            if key in row.get('projection', {})}
                revision = client.query(
                    'turn.revision', {'conversation_id': conversation_id,
                                      'user_id': user_id})
                if client.query('conversation.get', {
                        'conv_id': conversation_id,
                        'user_id': user_id,
                        'derive_messages': False}) is None:
                    raise LifecycleNotFound('Conversation not found')
                return {
                    'conversationId': conversation_id,
                    'conversationRevision': int(revision),
                    'turns': rows,
                    'deletedTurnIds': list(
                        delta.get('deletedTurnIds') or []),
                    'serverNowMs': int(
                        delta.get('serverNowMs') or now_entry),
                    'delta': True,
                    'cutoverActive': True,
                    # A delta is never a deletion authority on its own;
                    # the tombstone list carries removals explicitly.
                    'authoritativeFull': False,
                }
    turns = [
        public_turn_with_stable_segments(row)
        for row in client.query(
        'turn.list', {
            'conversation_id': conversation_id, 'user_id': user_id,
            **({'lane_id': lane_id} if lane_id else {}),
        })
    ]
    if after_ordinal is not None:
        turns = [row for row in turns
                 if int(row.get('ordinal', 0)) > int(after_ordinal)]
    turns = turns[:limit]
    if light:
        for row in turns:
            row['projection'] = {
                key: row['projection'][key]
                for key in ('content', 'thinking', 'segments', 'model', 'usage')
                if key in row.get('projection', {})}
    revision = client.query(
        'turn.revision', {'conversation_id': conversation_id, 'user_id': user_id})
    if client.query('conversation.get', {
            'conv_id': conversation_id,
            'user_id': user_id,
            'derive_messages': False}) is None:
        raise LifecycleNotFound('Conversation not found')
    return {
        'conversationId': conversation_id,
        'conversationRevision': int(revision), 'turns': turns,
        'cutoverActive': True,
        # Watermark seed: the client's first delta sync anchors here.
        'serverNowMs': _now_ms(),
        'authoritativeFull': bool(lane_id is None and after_ordinal is None
                                  and len(turns) < limit),
    }


def get_turn(conversation_id: str, turn_id: str, *, user_id: Any) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='get conversation turn')
    row = _turn_client().query(
        'turn.get', {'conversation_id': conversation_id,
                     'turn_id': turn_id, 'user_id': user_id})
    if row is None:
        raise LifecycleNotFound('Turn not found')
    return public_turn_with_stable_segments(row)


def get_attempt(attempt_id: str, *, user_id: Any, include_execution_config: bool = False) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='get turn attempt')
    if type(include_execution_config) is not bool:
        raise TypeError('Invalid execution config flag')
    payload={'attempt_id':attempt_id,'user_id':user_id}
    if include_execution_config:
        payload['include_execution_config']=True
    row = _turn_client().query('turn.attempt.get',payload)
    if row is None:
        raise LifecycleNotFound('Attempt not found')
    return row


def list_dispatchable_attempts(
    *, created_before_ms: int, limit: int = 8,
) -> list[dict[str, Any]]:
    """Read one bounded system batch of accepted, never-dispatched attempts.

    The semantic operation applies the dispatch-mode and empty-task proof
    before its limit. Each returned item includes the durable owner identity;
    callers must pass that owner back through the normal command service.
    """
    cutoff = int(created_before_ms)
    bounded_limit = int(limit)
    if cutoff < 0:
        raise ValueError('created_before_ms must be non-negative')
    if not 1 <= bounded_limit <= 32:
        raise ValueError('dispatchable attempt limit must be between 1 and 32')
    rows = _turn_client().query(
        'turn.attempt.dispatchable.list', {
            'created_before_ms': cutoff,
            'limit': bounded_limit,
        },
        deadline=2.0,
    )
    if not isinstance(rows, list):
        raise StorageError(
            'database_protocol_error',
            'Dispatchable attempt query returned an invalid result',
        )
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def get_conversation_revision(conversation_id: str, *, user_id: Any) -> int:
    user_id = require_user_id(user_id, context='get conversation revision')
    revision = _turn_client().query(
        'turn.revision', {'conversation_id': conversation_id,
                          'user_id': user_id})
    if revision == 0:
        # A zero revision is valid for a newly created conversation; use
        # the conversation domain to distinguish missing from empty.
        if _turn_client().query(
                'conversation.get', {'conv_id': conversation_id,
                                     'user_id': user_id,
                                     'derive_messages': False}) is None:
            raise LifecycleNotFound('Conversation not found')
    return int(revision)


def update_turn_projection(conversation_id: str, turn_id: str, *,
                           projection: dict[str, Any],
                           expected_projection_revision: int,
                           user_id: Any, preserve_activity: bool = False) -> dict[str, Any]:
    """CAS-edit one settled visible turn without creating an attempt."""
    user_id = require_user_id(user_id, context='update turn projection')
    # Identity and renderer-only fields never enter the durable projection.
    from lib.turn_projection_patch import normalize_projection_document
    current = get_turn(conversation_id, turn_id, user_id=user_id)
    normalized = projection_with_stable_segments(
        normalize_projection_document(projection),
        actor=str(current.get('actor') or 'assistant'),
        status=str(current.get('status') or 'completed'),
    )
    from lib.storage import StorageError
    try:
        return _turn_client(write=True).command(
            'turn.projection.update', {
                'conversation_id': conversation_id, 'user_id': user_id,
                'turn_id': turn_id, 'projection': normalized,
                'expected_projection_revision': expected_projection_revision,
                'preserve_activity': preserve_activity,
            }, f'turn-projection:{turn_id}:{expected_projection_revision}')
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'turn_projection_stale':
            raise LifecycleConflict('stale_projection', str(exc)) from exc
        if exc.code == 'turn_in_progress':
            raise LifecycleConflict('turn_in_progress', str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def record_turn_perception(
    conversation_id: str,
    turn_id: str,
    *,
    attempt_id: str,
    observation: Mapping[str, Any],
    user_id: Any,
) -> dict[str, Any]:
    """Append one owner-scoped, content-free browser perception receipt."""
    user_id = require_user_id(user_id, context='record turn perception')
    observation_id = str(observation.get('observationId') or '')
    if not observation_id:
        raise ValueError('observationId is required')
    receipt_identity = hashlib.sha256(
        f'{user_id}\0{attempt_id}\0{observation_id}'.encode('utf-8')
    ).hexdigest()
    try:
        return _turn_client(write=True).command(
            'turn.perception.record', {
                'conversation_id': conversation_id,
                'user_id': user_id,
                'turn_id': turn_id,
                'attempt_id': attempt_id,
                'observation': dict(observation),
            }, f'turn-perception:{user_id}:{receipt_identity}')
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code in {'database_conflict', 'turn_projection_stale'}:
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def apply_commit_round_file_changes(
    conversation_id: str,
    turn_id: str,
    *,
    files: list[Any],
    modified_count: Any,
    task_id: str,
    user_id: Any,
    attempts: int = 4,
    truncated: bool = False,
) -> dict[str, Any] | None:
    """Fold the async commit-round's modified-file list into a settled turn.

    The commit round (``lib.tasks_pkg.commit_round``) derives the list AFTER
    the terminal settlement, so the done-time projection lacks it and
    ``record_task_event`` correctly refuses the post-settlement
    ``round_committed`` frame.  This is the dedicated post-settlement seam:
    CAS-patch only the file-change fields through the same
    ``turn.projection.update`` authority the undo/redo commands use (which
    also emits the live ``turn.patch`` push), carrying every other projection
    byte through unchanged.  Without it the turn-native UI never renders the
    files-changed card (2026-08-26 regression from moving the derivation off
    the done hot path).

    Returns the update result, or None when the fold is unnecessary (no
    files, or the commit thread had already won the settlement race and the
    identical block is present).
    """
    user_id = require_user_id(user_id, context='commit-round file changes')
    if not files:
        return None
    task_id = str(task_id or '')
    for attempt in range(max(1, attempts)):
        current = get_turn(conversation_id, turn_id, user_id=user_id)
        projection = dict(current.get('projection') or {})
        existing = projection.get('fileChanges')
        if (isinstance(existing, dict)
                and str(existing.get('taskId') or '') == task_id
                and existing.get('files') == files
                and (not truncated or existing.get('truncated'))):
            return None
        projection['modifiedFileList'] = [
            dict(item) if isinstance(item, dict) else item for item in files
        ]
        if truncated:
            projection['modifiedFilesTruncated'] = True
        if isinstance(modified_count, int) and not isinstance(
                modified_count, bool) and modified_count >= 0:
            projection['modifiedFiles'] = modified_count
        block = _file_changes_block(task_id, projection)
        if block is None:
            return None
        projection['fileChanges'] = block
        try:
            return update_turn_projection(
                conversation_id,
                turn_id,
                projection=projection,
                expected_projection_revision=int(
                    current.get('projectionRevision') or 0),
                user_id=user_id,
            )
        except LifecycleConflict as conflict:
            if conflict.args and conflict.args[0] == 'turn_in_progress':
                # The Flow-managed path can race the terminal settlement;
                # give the done fold a brief moment to land, then retry.
                if attempt + 1 < max(1, attempts):
                    time.sleep(0.2)
            continue
    raise LifecycleConflict(
        'stale_projection',
        'commit-round file changes could not be folded into the turn',
    )


def create_branch_lane(conversation_id: str, parent_turn_id: str, *,
                       title: str, anchor_text: str = '',
                       parent_selection: str = '', kind: str = 'branch',
                       expected_projection_revision: int,
                       user_id: Any) -> dict[str, Any]:
    """Create server-issued branch lane metadata on its parent turn."""
    user_id = require_user_id(user_id, context='create branch lane')
    from lib.storage import StorageError
    try:
        return _turn_client(write=True).command(
            'turn.branch.create', {
                'conversation_id': conversation_id, 'user_id': user_id,
                'parent_turn_id': parent_turn_id, 'title': title,
                'anchor_text': anchor_text, 'parent_selection': parent_selection,
                'kind': kind, 'expected_projection_revision': expected_projection_revision,
            }, f'turn-branch:{parent_turn_id}:{expected_projection_revision}')
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def delete_branch_lane(conversation_id: str, parent_turn_id: str,
                       lane_id: str, *, user_id: Any) -> dict[str, Any]:
    """Delete one explicit branch lane and all of its diagnostic attempts."""
    user_id = require_user_id(user_id, context='delete branch lane')
    from lib.storage import StorageError
    try:
        return _turn_client(write=True).command(
            'turn.branch.delete', {'conversation_id': conversation_id,
                'user_id': user_id, 'parent_turn_id': parent_turn_id,
                'lane_id': lane_id}, f'turn-branch-delete:{parent_turn_id}:{lane_id}')
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def delete_turns(conversation_id: str, turn_ids: list[str], *,
                 user_id: Any) -> dict[str, Any]:
    """Delete explicitly named settled visible turns by stable identity."""
    user_id = require_user_id(user_id, context='delete conversation turns')
    wanted = list(dict.fromkeys(str(item) for item in turn_ids if item))
    if not wanted:
        raise ValueError('turnIds required')
    from lib.storage import StorageError
    try:
        return _turn_client(write=True).command(
            'turn.delete', {'conversation_id': conversation_id,
                'user_id': user_id, 'turn_ids': wanted},
            f'turn-delete:{conversation_id}:{",".join(sorted(wanted))}')
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise


def read_events(attempt_id: str, *, after: int = 0,
                user_id: Any, limit: int = 1000,
                projection_mode: str = 'full') -> list[dict[str, Any]]:
    user_id = require_user_id(user_id, context='read turn events')
    events = _turn_client().query(
        'turn.events.list', {'attempt_id': attempt_id, 'after': int(after or 0),
                             'user_id': user_id,
                             'limit': min(max(int(limit), 1), 5000),
                             'projection_mode': (
                                 'patch' if projection_mode == 'patch'
                                 else 'full')})
    if events is None:
        raise LifecycleNotFound('Attempt not found')
    return events


def attempt_is_terminal(attempt_id: str, *, user_id: Any) -> bool:
    user_id = require_user_id(user_id, context='read turn attempt status')
    row = _turn_client().query(
        'turn.attempt.get', {'attempt_id': attempt_id, 'user_id': user_id})
    if row is None:
        raise LifecycleNotFound('Attempt not found')
    return row['status'] not in LIVE_ATTEMPT_STATUSES


def _notify_abort_busy_projection(conversation_id: Any, user_id: Any) -> None:
    """Re-broadcast the conv busy projection the instant a turn-native abort lands.

    Parity with the v1 ``/api/v1/chat/abort/<task_id>`` handler (which emits
    ``notify_conv_changed`` unconditionally — see routes/chat_poll_abort.py's
    "User-Stop busy-projection broadcast"). The busy projection already
    EXCLUDES an aborted task by design, but a frame only leaves the server
    when someone emits it; without this emit the sidebar dot / sibling tabs
    keep reading busy until the task fully unwinds (up to a whole prep stage
    or tool call later). Fail-open: a notify error must never break abort.
    """
    if not conversation_id:
        return
    try:
        from lib.conversations.change_notifications import notify_conv_changed
        notify_conv_changed(str(conversation_id), rev=None, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 - fail-open by contract
        logger.debug('[turns] abort busy-notify failed: %s', exc)


def abort_attempt(attempt_id: str, *, user_id: Any, withdraw_dispatch: bool = True) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='abort turn attempt')
    row = _turn_client().query(
        'turn.attempt.get', {'attempt_id': attempt_id, 'user_id': user_id,
                             'resolve_executor': True})
    if row is None:
        raise LifecycleNotFound('Attempt not found')
    # The bound executor task id rides the attempt row (written by
    # ``bind_task``); without it the cooperative abort below can never
    # reach the registry and the worker keeps cycling for hours while
    # every event persistence is rejected as stale (2026-08-17 flood).
    task_id = str(row.get('taskId') or '')
    status = row['status']
    if status not in LIVE_ATTEMPT_STATUSES:
        if withdraw_dispatch:
            from lib.project_dispatch.recovery import revoke_conversation_dispatch
            revoke_conversation_dispatch(row['conversationId'], user_id=user_id)
        return {'attemptId': attempt_id, 'status': status, 'alreadyTerminal': True}
    queue_binding = row.get('queueBinding')
    if isinstance(queue_binding, dict) and queue_binding.get('queueId'):
        # A queue-bound attempt has no executor task: stopping it withdraws
        # the queued intent itself (queue-bar cancel semantics). Settling it
        # 'interrupted' would orphan the durable queue row — undispatchable
        # forever yet still counted by queue.depth (the composer badge).
        cancelled = cancel_queued_turn_pair(
            str(row['conversationId']), str(queue_binding['queueId']),
            user_id=user_id)
        if withdraw_dispatch:
            from lib.project_dispatch.recovery import revoke_conversation_dispatch
            revoke_conversation_dispatch(row['conversationId'], user_id=user_id)
        return {
            'attemptId': attempt_id,
            'status': 'cancelled',
            'queueId': str(queue_binding['queueId']),
            'cancelled': bool(cancelled.get('cancelled')),
            'deletedTurnIds': list(cancelled.get('deletedTurnIds') or []),
        }
    from lib.tasks_pkg.manager.cancellation import cancel_task

    receipt = (cancel_task(task_id, user_id=user_id, source='turn_attempt_abort')
               if task_id else {'found': False})
    if not receipt['found']:
        # Registry miss (or no task bound yet): plant the durable abort
        # tombstone so a live-but-evicted worker's abort_check consumes it
        # at its next poll, then settle the attempt row.
        if task_id:
            try:
                from lib.tasks_pkg.manager import plant_abort_tombstone
                plant_abort_tombstone(
                    task_id, source='turn_attempt_abort', user_id=int(user_id))
            except Exception as e:
                logger.debug('[TurnLifecycle] abort tombstone plant failed '
                             'attempt=%s task=%s: %s', attempt_id, task_id, e)
        turn = get_turn(row['conversationId'], row['turnId'], user_id=user_id)
        projection = turn.get('projection') or {}
        record_task_event(
            {'_attemptId': row['attemptId'], '_userId': user_id,
             'id': '', 'status': 'aborted',
             'aborted': True, 'content': projection.get('content') or '',
             'thinking': projection.get('thinking') or '',
             'toolRounds': projection.get('toolRounds') or [],
             'segments': projection.get('segments') or [],
             'model': projection.get('model') or ''}, {'type': 'aborted'})
    if withdraw_dispatch:
        from lib.project_dispatch.recovery import revoke_conversation_dispatch
        revoke_conversation_dispatch(row['conversationId'], user_id=user_id)
    _notify_abort_busy_projection(row.get('conversationId'), user_id)
    return {'attemptId': attempt_id, 'status': 'abort_signaled'}


def build_api_messages(
    conversation_id: str,
    turn_id: str,
    config: dict[str, Any],
    *,
    user_id: int,
    allow_model_replay: bool = True,
) -> list[dict[str, Any]] | None:
    """Project turn-native turns into the existing executor's API-ready message form."""
    owner_user_id = require_user_id(
        user_id, context='turn message projection')
    client = _turn_client()
    from lib.storage.model_continuation import ModelContinuationStore
    try:
        context = ModelContinuationStore(client=client).read_context(
            conversation_id=conversation_id, user_id=owner_user_id,
            turn_id=turn_id, replay=allow_model_replay)
    except StorageError as exc:
        # Upgrade-window compatibility: an older engine binary does not know
        # turn.context.read yet — fall back to the plain turn.list projection
        # rather than failing the whole turn start. Older certified engines
        # wrap the same condition as database_internal/'unknown_operation'
        # (exact engine text; a bare database_internal still fails hard).
        if exc.code not in ('unknown_operation', 'operation_not_implemented') and not (
                exc.code == 'database_internal' and str(exc) == 'unknown_operation'):
            raise
        context = {'supported': False}
    if context is None:
        return None
    if isinstance(context, dict) and context.get('supported'):
        turns = context['turns']
    else:
        context = None
        turns = client.query('turn.list', {
            'conversation_id': conversation_id, 'user_id': owner_user_id})
    target = next((item for item in turns if item['turnId'] == turn_id), None)
    if target is None:
        return None
    lane_id = target.get('laneId') or 'main'
    direct_parent = next(
        (item for item in turns
         if item['turnId'] == target.get('parentTurnId')),
        None,
    )
    parent_projection = (
        direct_parent.get('projection')
        if isinstance(direct_parent, dict) else None
    )
    plan_execution = (
        parent_projection.get('planExecution')
        if isinstance(parent_projection, dict) else None
    )
    if (isinstance(plan_execution, dict)
            and plan_execution.get('contextMode') == 'fresh'):
        # Fresh execution is a model-context boundary, not a destructive
        # history mutation. Keep the exact accepted handoff plus the empty
        # target turn; workspace/system constraints are composed normally.
        selected = [direct_parent, target]
        context = None
    else:
        selected = []
        if lane_id != 'main':
            lane_rows = [item for item in turns if item.get('laneId') == lane_id]
            parent_id = lane_rows[0].get('parentTurnId') if lane_rows else None
            parent = next(
                (item for item in turns if item['turnId'] == parent_id), None)
            if parent is not None:
                selected.extend(
                    item for item in turns
                    if item.get('laneId') == parent.get('laneId', 'main')
                    and item['ordinal'] <= parent['ordinal'])
        selected.extend(
            item for item in turns
            if item.get('laneId') == lane_id
            and item['ordinal'] <= target['ordinal'])
    from lib.tasks_pkg.model_replay import restore_prefix
    prefix_messages, covered = restore_prefix(
        selected, config, conversation_id=conversation_id, user_id=owner_user_id,
        validated_context=context)
    if context and context.get('covered') and not prefix_messages:
        # Policy/media/provider refusal needs the full selected history, never
        # just the validated suffix. The explicit cold read reports replay_disabled;
        # retain the original refusal which actually caused this rebuild.
        refusal = config.get('_modelReplayStatus', 'unknown')
        rebuilt = build_api_messages(conversation_id, turn_id, config,
                                     user_id=owner_user_id, allow_model_replay=False)
        config['_modelReplayStatus'] = refusal
        logger.warning('[ModelReplay] conv=%s restore=%s fallback=full_history messages=%d',
                       conversation_id[:8], refusal, len(rebuilt) if rebuilt is not None else 0)
        return rebuilt
    logger.info('[ModelReplay] conv=%s restore=%s turns=%d messages=%d',
                conversation_id[:8], config.get('_modelReplayStatus'),
                covered, len(prefix_messages))
    raw = []
    for row in selected[covered:]:
        projection = dict(row.get('projection') or {})
        role = 'user' if row.get('actor') in {'human', 'virtual_user', 'critic'} else 'assistant'
        projection['role'] = role
        projection['_turnId'] = row['turnId']
        projection['_turnStatus'] = row.get('status')
        raw.append(projection)
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    return _transform_messages(
        raw, config, exclude_last=bool(config.get('excludeLast')),
        user_id=owner_user_id, prefix_messages=prefix_messages)


def backfill_turn_search_index(*, max_rounds: int = 100_000) -> dict[str, Any]:
    """Converge historical turn-native search projections off the hot path.

    Each sidecar command scans a cursor-bounded authority slice and writes on
    the maintenance lane, so a large existing store cannot block readiness or
    starve user writes.  New/changed turns are maintained by their own atomic
    lifecycle transactions; this sweep exists only for rows created before the
    per-turn index shipped.
    """
    cursor = ''
    totals = {'scanned': 0, 'indexed': 0, 'failed': 0}
    sweep_id = f'{_now_ms()}:{uuid.uuid4().hex[:12]}'
    for round_no in range(max(1, int(max_rounds))):
        result = _turn_client(write=True).command(
            'turn.search.backfill', {
                'cursor': cursor,
                'max_rows': 8,
                'max_bytes': 2_000_000,
            },
            f'turn-search-backfill:{sweep_id}:{round_no}',
            priority='maintenance', deadline=60.0)
        if not isinstance(result, dict):
            raise RuntimeError('turn search backfill returned an invalid result')
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        next_cursor = str(result.get('nextCursor') or cursor)
        if not bool(result.get('remaining')):
            return {**totals, 'complete': True}
        if next_cursor == cursor:
            raise RuntimeError('turn search backfill cursor made no progress')
        cursor = next_cursor
        # Fair scheduling already prioritizes user/event lanes.  A tiny yield
        # also prevents a fast empty-fragment corpus from monopolizing the
        # client connection between maintenance chunks.
        time.sleep(0.01)
    return {**totals, 'complete': False, 'nextCursor': cursor}


def start_turn_search_backfill(
    *, initial_delay_seconds: float = _TURN_SEARCH_BACKFILL_INITIAL_DELAY_SECONDS,
) -> bool:
    """Start one historical search-index worker after the startup hot window.

    Newly settled turns maintain their own search rows transactionally. The
    sweep only repairs historical derived rows, so it may yield the first
    minute to browser hydration and recovery writers without weakening
    durability or current search correctness.
    """
    global _turn_search_backfill_started
    with _turn_search_backfill_lock:
        if _turn_search_backfill_started:
            return False
        _turn_search_backfill_started = True

    threading.Thread(
        target=_run_turn_search_backfill_worker,
        kwargs={
            'initial_delay_seconds': max(0.0, float(initial_delay_seconds)),
        },
        name='turn-search-backfill', daemon=True).start()
    return True


def _run_turn_search_backfill_worker(
    *, initial_delay_seconds: float = 0.0,
) -> None:
    """Converge the derived index despite transient Sidecar pressure.

    Startup maintenance is intentionally outside readiness.  A one-shot
    worker therefore cannot treat the first ``database_busy``/timeout after
    readiness as terminal: doing so leaves historical turn conversations
    unsearchable until the whole web process restarts.  Retry only classified
    transient storage failures; malformed responses and protocol/data errors
    remain loud, terminal failures.
    """
    from lib.storage import StorageError

    delay = max(0.0, float(initial_delay_seconds))
    if delay:
        logger.info(
            '[turn-search] historical projection backfill deferred %.0fs '
            'past the startup hot window', delay)
        time.sleep(delay)

    transient_failures = 0
    while True:
        try:
            stats = backfill_turn_search_index()
        except Exception as exc:
            if not isinstance(exc, StorageError) or not exc.retryable:
                logger.warning(
                    '[turn-search] historical projection backfill stopped: %s',
                    exc, exc_info=True)
                return
            transient_failures += 1
            retry_hint = max(0.0, float(exc.retry_after_ms or 0) / 1000.0)
            exponential = 0.25 * (2 ** min(transient_failures - 1, 7))
            delay = min(30.0, max(retry_hint, exponential))
            # Keep the first and exponentially-spaced failures visible without
            # turning a prolonged outage into a periodic warning flood.
            log = (logger.warning
                   if transient_failures & (transient_failures - 1) == 0
                   else logger.debug)
            log('[turn-search] historical projection backfill transient '
                'failure code=%s; retrying in %.2fs (attempt=%d)',
                exc.code, delay, transient_failures)
            time.sleep(delay)
            continue

        if transient_failures:
            logger.info('[turn-search] historical projection backfill '
                        'recovered after %d transient failure(s)',
                        transient_failures)
            transient_failures = 0
        if stats.get('complete'):
            logger.info('[turn-search] historical projection backfill '
                        'finished: %s', stats)
            return

        # The per-sweep round cap is a safety valve, not a semantic stopping
        # condition.  A very large authority must continue in another bounded
        # sweep instead of remaining partially searchable until next boot.
        logger.warning('[turn-search] historical projection backfill reached '
                       'the per-sweep cap; continuing: %s', stats)
        time.sleep(0.25)


def recover_running_attempts(*, created_before_ms: int | None = None,
                             exclude_task_ids=None) -> int:
    """Atomically settle pre-boot attempts; never starts billable work.

    The sidecar ``turn.recover`` operation settles a BOUNDED chunk per call
    (multi-MiB projections rewrite whole rows and can individually approach
    the writer watchdog — one unbounded transaction used to roll the whole
    recovery back and leave 'running' zombies forever, the 2026-08-19
    "回答中/重连中" incident). Loop the chunks until none remain.

    ``created_before_ms`` / ``exclude_task_ids`` are the liveness guards for
    the POST-SERVING backstop (serving_loop_lifecycle): only attempts older
    than the serving gate and not bound to a live in-registry task may be
    settled. Boot recovery passes neither (nothing is live at boot).
    """
    now = _now_ms()
    payload: dict[str, Any] = {}
    # Freeze the sweep's scope even at boot. New attempts cannot extend it
    # indefinitely while serving/recovery overlap.
    payload['created_before_ms'] = int(created_before_ms) if created_before_ms is not None else now
    if exclude_task_ids:
        payload['exclude_task_ids'] = sorted(str(t) for t in exclude_task_ids if t)
    recovered = 0
    # Each transaction remains byte/row bounded. The old 64-round cap silently
    # abandoned everything past 512 attempts at the native eight-row default.
    # A frozen cutoff plus strictly decreasing durable remainder bounds the
    # sweep by real work rather than an unrelated task-count constant.
    round_no = 0
    previous_remaining = None
    while True:
        result = _turn_client(write=True).command(
            'turn.recover', payload, f'turn-recover:{now}:{round_no}',
            deadline=30.0)
        if isinstance(result, dict):
            progress = int(result.get('recovered') or 0)
            remaining = int(result.get('remaining') or 0)
            recovered += progress
            if remaining <= 0:
                break
            if progress <= 0 or (previous_remaining is not None and remaining >= previous_remaining):
                raise StorageError('database_busy',
                    'Turn recovery made no durable progress; retry the remaining attempts',
                    True, 1000)
            previous_remaining = remaining
            round_no += 1
        else:
            # Pre-chunking sidecar build returned a bare count.
            recovered += int(result or 0)
            break
    if recovered:
        logger.warning('[TurnLifecycle] settled %d pre-boot attempt(s) as interrupted', recovered)
    return recovered


def cleanup_superseded_attempts(*, retention_ms: int = 6 * 60 * 60 * 1000,
                                limit: int = 64) -> int:
    """One bounded cleanup page within every supported backend's budget.

    Native authority admits at most 64 Attempts per transaction. Keep the
    application default inside that bound rather than failing during startup.
    Explicit caller limits still pass through authority validation.
    """
    cutoff = _now_ms() - max(int(retention_ms), 0)
    result = _turn_client(write=True).command(
        'turn.cleanup', {'retention_ms': retention_ms, 'limit': limit},
        f'turn-cleanup:{cutoff}:{limit}')
    # Native cleanup includes bounded scan diagnostics; Sidecar returns only
    # the count. Keep this domain facade's public count contract for both.
    count = result.get('deletedAttempts') if isinstance(result, Mapping) else result
    if type(count) is not int or count < 0:
        raise StorageError('database_protocol_error', 'Invalid Attempt cleanup count')
    return count



def retry_turn_commit(operation, *, abort_event=None, label='turn commit'):
    """Host persistence adapter for replaying an identical fenced turn commit.

    Core execution receives this callable through its runtime ports and never
    imports a concrete storage retry policy. The host classifies authority
    stalls and owns the bounded, abortable retry schedule.
    """
    from lib.storage.retry import call_with_transient_retry
    return call_with_transient_retry(operation, abort_event=abort_event, label=label)


__all__ = [
    'LifecycleConflict', 'LifecycleNotFound', 'TERMINAL_STATUSES',
    'create_turn_pair', 'append_settled_turn', 'announce_related_turns',
    'create_attempt', 'attempt_dispatch_lock', 'claim_attempt_start',
    'dispatch_attempt_to_worker',
    'bind_task', 'mark_task_started', 'fail_start',
    'record_task_event', 'sync_visible_run_turns', 'next_progress_projection_deadline',
    'list_turns', 'get_turn', 'get_attempt', 'list_dispatchable_attempts',
    'update_turn_projection', 'accept_live_guidance_answer',
    'create_branch_lane', 'delete_branch_lane',
    'delete_turns',
    'get_conversation_revision', 'read_events',
    'attempt_is_terminal', 'abort_attempt', 'build_api_messages',
    'backfill_turn_search_index', 'start_turn_search_backfill',
    'recover_running_attempts', 'cleanup_superseded_attempts', 'retry_turn_commit',
]


def accept_live_guidance_answer(conversation_id: str, turn_id: str, *,
                                guidance_id: str, response: str, command_id: str,
                                expected_projection_revision: int, user_id: Any) -> dict[str, Any]:
    """Commit the exact question's answer before any process-local wake."""
    user_id = require_user_id(user_id, context='answer live human guidance')
    from lib.storage import StorageError
    try:
        return _turn_client(write=True).command('turn.projection.update', {
            'conversation_id': conversation_id, 'turn_id': turn_id, 'user_id': user_id,
            'expected_projection_revision': expected_projection_revision,
            'human_guidance_answer': {'guidanceId': guidance_id, 'response': response,
                                      'commandId': command_id},
        }, f'human-guidance:{turn_id}:{command_id}')
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code == 'database_conflict':
            raise LifecycleConflict(exc.code, str(exc)) from exc
        raise
