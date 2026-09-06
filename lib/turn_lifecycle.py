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
from lib.storage_projection import (
    compact_tool_rounds_for_frame_budget,
    sanitize_api_rounds_for_persist,
    trim_tool_round_for_persist,
)
from lib.tool_round_identity import tool_rounds_with_execution_identity
from lib.tool_round_replay import (
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


def _delta_throttle_allows(attempt_id: str) -> bool:
    """Peek: True when a delta projection write is due for this attempt."""
    if _delta_record_min_interval_s() <= 0:
        return True
    with _delta_throttle_lock:
        last = _delta_last_recorded.get(attempt_id)
        return last is None or (time.monotonic() - last) >= _delta_record_min_interval_s()


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
    cfg = task.get('config') or {}
    owns_visible_run_turns = bool(task.get('_turnVisibleRunTurnIds'))
    content = (previous.get('content', '') if owns_visible_run_turns else
               (task.get('content') or cfg.get('contentPrefix') or ''))
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
    """Idempotently delete one pending queue row and its unexecuted pair."""
    user_id = require_user_id(user_id, context='cancel queued turn pair')
    try:
        return public_value_with_stable_segments(_turn_client(write=True).command(
            'turn.queue.cancel',
            {
                'conversation_id': conversation_id,
                'queue_id': queue_id,
                'user_id': user_id,
            },
            command_id=f'turn-queue-cancel:{conversation_id}:{queue_id}',
        ))
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
) -> dict[str, Any]:
    """Durably append a pending injection block before waking the live worker."""
    user_id = require_user_id(user_id, context='commit user steer')
    try:
        return public_value_with_stable_segments(_turn_client(write=True).command(
            'turn.steer.commit',
            {
                'conversation_id': conversation_id,
                'attempt_id': attempt_id,
                'command_id': command_id,
                'text': text,
                'user_id': user_id,
            },
            command_id=f'turn-steer-commit:{conversation_id}:{command_id}',
        ))
    except StorageError as exc:
        if exc.code == 'database_not_found':
            raise LifecycleNotFound(str(exc)) from exc
        if exc.code in {'database_conflict', 'turn_in_progress'}:
            raise LifecycleConflict('steer_window_closed', str(exc)) from exc
        raise


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
) -> dict[str, Any] | None:
    """Publish the pending→running transition at physical worker entry."""
    user_id = require_user_id(user_id, context='start bound turn task')
    return _turn_client(write=True).command(
        'turn.attempt.start', {
            'attempt_id': attempt_id,
            'task_id': task_id,
            'user_id': user_id,
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
    }
    if same_operation:
        for key in ('state', 'commandId', 'error', 'effect'):
            if key in previous_file_changes:
                file_changes[key] = previous_file_changes[key]
    return file_changes


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
    content = (projection.get('content', '') if owns_visible_run_turns else
               (task.get('content') or cfg.get('contentPrefix') or ''))
    checkpoint_rounds = (task.get('_checkpointToolRounds')
                         or cfg.get('checkpointToolRounds') or [])
    attempt_id = task.get('_attemptId') or task.get('attemptId') or ''
    task_id = task.get('id') or task.get('taskId') or ''
    # A Turn can outlive several executors. Preserve checkpoint ownership and
    # stamp this attempt's fresh rounds before they cross the durable projection
    # boundary; llmRound/roundNum restart for every resumed executor.
    merged_rounds = tool_rounds_with_execution_identity(
        checkpoint_rounds, attempt_id='', task_id='',
    ) + tool_rounds_with_execution_identity(
        task.get('toolRounds') or [],
        attempt_id=attempt_id,
        task_id=task_id if attempt_id else '',
        overwrite=bool(attempt_id),
    )
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
    for source, target in (
        ('segments', 'segments'), ('usage', 'usage'), ('model', 'model'),
        ('provider_id', 'providerId'),
        ('preset', 'preset'), ('thinkingDepth', 'thinkingDepth'),
        ('modifiedFiles', 'modifiedFiles'),
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
    ):
        if task.get(source) is not None:
            projection[target] = task[source]
    # Display-only injection records are copied and normalized independently:
    # unlike content/tool facts, their stable render identity is part of the
    # projection contract and must survive hydration.  Normalize previous
    # projections too so the next event upgrades rows written before blockId.
    for source, target, channel in _PROJECTION_INJECTION_LANES:
        records = (task[source] if task.get(source) is not None
                   else projection.get(target))
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
            )
            if _settled_cost is None:
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
    settlement = {
        'outcome': outcome,
        'cause': cause,
        'evidence': verdict.evidence.value,
        'streamState': stream_state.value if stream_state is not None else None,
        'providerFinishReason': finish or None,
        'error': error,
        'resumeOptions': options,
    }
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
                                      event_kind: str) -> None:
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
        'attempt=%s event=%s; signaling cooperative abort',
        str(task.get('id') or '?')[:8], str(attempt_id or '?')[:8], event_kind,
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
        # 'failed' mirrors the v1 skip-on-error discipline (the human may
        # want to fix something before the queued message runs); aborts and
        # truncations drain, exactly like v1.
        if status not in ('completed', 'interrupted', 'truncated'):
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
        if get_queue_depth(
                conversation_id, user_id=owner_user_id) == 0:
            return
        import threading

        def _drain():
            try:
                from lib.message_queue import dispatch_next_queued
                new_task_id = dispatch_next_queued(
                    conversation_id, user_id=owner_user_id)
                if new_task_id:
                    logger.info('[Queue] turn-native settlement drain dispatched '
                                'queued message → task %s for conv=%s',
                                new_task_id[:8], conversation_id[:8])
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
            or revision < 0 or not isinstance(projection, dict)):
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
) -> None:
    """Retain one bounded live baseline; terminal cleanup releases it."""
    task[_TURN_PROJECTION_STATE_KEY] = {
        'attemptId': attempt_id,
        'userId': user_id,
        'conversationId': str(
            turn.get('conversationId') or task.get('convId') or ''),
        'turnId': str(turn.get('turnId') or task.get('_turnId') or ''),
        'actor': str(turn.get('actor') or task.get('_turnActor') or 'assistant'),
        'projectionRevision': projection_revision,
        'projection': projection,
    }


def record_task_event(task: dict[str, Any], raw_event: dict[str, Any],
                      task_event: dict[str, Any] | None = None):
    """Persist one task projection/event before it becomes client-visible.

    Returns False for a task without a turn attempt, a stale attempt, a
    duplicate terminal event,
    or superseded executor.  Those events must not mutate turn-native authority.

    Returns the string ``'coalesced'`` (truthy, NOT a rejection) when a pure
    text ``delta`` is folded into the next durable write by the coalescing
    window — see the module-level note.  The frame's content is not lost:
    the next persisted event (any structural frame, the window's first due
    delta, or the terminal settlement) carries the cumulative projection.

    When ``task_event`` (``{task_id, sequence, event}``) is attached and the
    sidecar path applies, the frame's storage_events row commits INSIDE the
    turn authority transaction and the return is the string ``'carried'`` —
    one frame = one authority transaction (2026-08-20 double-write root
    fix).  Every other outcome leaves the event row to the caller's
    standalone append.
    """
    with _task_projection_state_lock(task):
        return _record_task_event_locked(task, raw_event, task_event)


def _record_task_event_locked(
    task: dict[str, Any],
    raw_event: dict[str, Any],
    task_event: dict[str, Any] | None,
    *,
    allow_rebase: bool = True,
):
    """Record one event while this task owns its projection state lock."""
    attempt_id = task.get('_attemptId') or task.get('attemptId')
    if not attempt_id:
        return False
    from lib.tasks_pkg.manager._registry import task_user_id
    user_id = task_user_id(task)
    now = _now_ms()
    event_kind = str(raw_event.get('type') or 'projection')
    if (event_kind in _COALESCIBLE_EVENT_KINDS
            and not _delta_throttle_allows(attempt_id)):
        # Coalesced frames do not enter the write transaction that normally
        # proves the attempt fence, so retain the small authoritative status
        # read on this lane. A superseded worker must never leak a late delta
        # merely because its projection baseline was cached.
        attempt = get_attempt(attempt_id, user_id=user_id)
        if attempt['status'] not in LIVE_ATTEMPT_STATUSES:
            _signal_stale_attempt_abort(task, attempt_id, 'attempt-not-live')
            return False
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
    slim = not terminal and (
        oversize_circuit_open
        or (
            event_kind in _COALESCIBLE_EVENT_KINDS
            and not _structural_fold_due(attempt_id)
        )
    )
    if slim:
        content, thinking = _delta_text_fields(task, previous)
        projection = {'content': content, 'thinking': thinking}
    else:
        projection = _task_projection(task, previous, raw_event)
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
    _live_phase = None if terminal else task.get('phase')
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
    command_payload = {
        'attempt_id': attempt_id, 'user_id': user_id,
        'task_id': str(task.get('id') or ''),
        'terminal': terminal, 'status': status, 'settlement': settlement,
        'error': error, 'event_type': event_type,
        'event_payload': event_payload, 'now': now,
    }
    if slim:
        command_payload['slim'] = True
        command_payload['content'] = content
        command_payload['thinking'] = thinking
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
                and allow_rebase and not slim):
            # A legitimate external projection CAS advanced the row after our
            # last event. Refresh exactly once and rebuild this same raw event
            # against that base; repeated contention still fails closed.
            task.pop(_TURN_PROJECTION_STATE_KEY, None)
            return _record_task_event_locked(
                task,
                raw_event,
                task_event,
                allow_rebase=False,
            )
        if exc.code == 'database_integrity':
            _signal_authority_integrity_abort(
                task, attempt_id, event_kind)
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
                or task_event is None or slim):
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
        content, thinking = _delta_text_fields(task, previous)
        projection = {'content': content, 'thinking': thinking}
        command_payload.update({
            'event_payload': event_payload,
            'slim': True,
            'content': content,
            'thinking': thinking,
        })
        command_payload.pop('projection_patch', None)
        command_payload.pop('projection_segments_stable', None)
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
                    task, attempt_id, event_kind)
            raise
    applied = bool(result.get('applied'))
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
                # Sidecar patch writes normalize their locked base through
                # the public stable-segment projection before applying the
                # next delta. Keep this local baseline on the same shape: a
                # slim text write intentionally leaves the at-rest segment
                # mirror stale until the next structural write, while every
                # public ``turn.get`` already presents the repaired mirror.
                applied_projection = projection_with_stable_segments(
                    applied_projection,
                    actor=str(turn.get('actor') or 'assistant'),
                    status=status,
                )
            _remember_task_projection_state(
                task,
                attempt_id=attempt_id,
                user_id=user_id,
                turn=turn,
                projection=applied_projection,
                projection_revision=applied_revision,
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
    visible_result = _turn_client(write=True).command(
        'turn.visible.sync', {
            'conversation_id': conversation_id, 'attempt_id': attempt_id,
            'root_turn_id': root_turn_id, 'messages': messages,
            'user_id': user_id,
            'default_kind': default_kind,
            'run_id': (task.get('config') or {}).get('runId') or attempt_id,
        }, f'turn-visible:{attempt_id}:{len(messages)}:{now}')
    visible_ids = (
        visible_result.get('visibleTurnIds')
        if isinstance(visible_result, dict)
        else visible_result
    )
    if visible_ids:
        task['_turnVisibleRunTurnIds'] = visible_ids
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


def get_attempt(attempt_id: str, *, user_id: Any) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='get turn attempt')
    row = _turn_client().query(
        'turn.attempt.get', {'attempt_id': attempt_id, 'user_id': user_id})
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
                           user_id: Any) -> dict[str, Any]:
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
                and existing.get('files') == files):
            return None
        projection['modifiedFileList'] = [
            dict(item) if isinstance(item, dict) else item for item in files
        ]
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


def abort_attempt(attempt_id: str, *, user_id: Any) -> dict[str, Any]:
    user_id = require_user_id(user_id, context='abort turn attempt')
    row = _turn_client().query(
        'turn.attempt.get', {'attempt_id': attempt_id, 'user_id': user_id})
    if row is None:
        raise LifecycleNotFound('Attempt not found')
    # The bound executor task id rides the attempt row (written by
    # ``bind_task``); without it the cooperative abort below can never
    # reach the registry and the worker keeps cycling for hours while
    # every event persistence is rejected as stale (2026-08-17 flood).
    task_id = str(row.get('taskId') or '')
    status = row['status']
    if status not in LIVE_ATTEMPT_STATUSES:
        return {'attemptId': attempt_id, 'status': status, 'alreadyTerminal': True}
    task = None
    if task_id:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        task = chat_task_runtime.get_owned(task_id, user_id=int(user_id))
    if task is not None:
        was_pending = str(task.get('status') or '') == 'pending'
        chat_task_runtime.abort_owned(task_id, user_id=int(user_id))
        chat_task_runtime.update_fields(
            task_id,
            fields={
                'aborted': True,
                '_abort_timestamp': time.time(),
                '_abort_reason': 'turn_attempt_abort',
            },
        )
        if was_pending:
            from lib.tasks_pkg.spawn import cancel_queued_task

            if cancel_queued_task(task_id):
                from lib.tasks_pkg.manager import finalize_chat_task_aborted

                finalize_chat_task_aborted(task)
    else:
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
            {'_attemptId': attempt_id, '_userId': user_id,
             'id': '', 'status': 'aborted',
             'aborted': True, 'content': projection.get('content') or '',
             'thinking': projection.get('thinking') or '',
             'toolRounds': projection.get('toolRounds') or [],
             'segments': projection.get('segments') or [],
             'model': projection.get('model') or ''}, {'type': 'aborted'})
    _notify_abort_busy_projection(row.get('conversationId'), user_id)
    return {'attemptId': attempt_id, 'status': 'abort_signaled'}


def build_api_messages(
    conversation_id: str,
    turn_id: str,
    config: dict[str, Any],
    *,
    user_id: int,
) -> list[dict[str, Any]] | None:
    """Project turn-native turns into the existing executor's API-ready message form."""
    owner_user_id = require_user_id(
        user_id, context='turn message projection')
    turns = _turn_client().query(
        'turn.list', {
            'conversation_id': conversation_id,
            'user_id': owner_user_id,
        })
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
    raw = []
    for row in selected:
        projection = dict(row.get('projection') or {})
        role = 'user' if row.get('actor') in {'human', 'virtual_user', 'critic'} else 'assistant'
        projection['role'] = role
        projection['_turnId'] = row['turnId']
        raw.append(projection)
    from lib.tasks_pkg.conv_message_builder._transform import _transform_messages
    return _transform_messages(
        raw, config, exclude_last=bool(config.get('excludeLast')),
        user_id=owner_user_id)


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
    if created_before_ms is not None:
        payload['created_before_ms'] = int(created_before_ms)
    if exclude_task_ids:
        payload['exclude_task_ids'] = sorted(str(t) for t in exclude_task_ids if t)
    recovered = 0
    # Each round is one bounded sidecar transaction; the hard cap only
    # guards against a pathological producer re-creating rows mid-sweep.
    for round_no in range(64):
        result = _turn_client(write=True).command(
            'turn.recover', payload, f'turn-recover:{now}:{round_no}',
            deadline=30.0)
        if isinstance(result, dict):
            recovered += int(result.get('recovered') or 0)
            if int(result.get('remaining') or 0) <= 0:
                break
        else:
            # Pre-chunking sidecar build returned a bare count.
            recovered += int(result or 0)
            break
    if recovered:
        logger.warning('[TurnLifecycle] settled %d pre-boot attempt(s) as interrupted', recovered)
    return recovered


def cleanup_superseded_attempts(*, retention_ms: int = 6 * 60 * 60 * 1000,
                                limit: int = 500) -> int:
    """Bounded diagnostic-retention cleanup for replaced attempts."""
    cutoff = _now_ms() - max(int(retention_ms), 0)
    return int(_turn_client(write=True).command(
        'turn.cleanup', {'retention_ms': retention_ms, 'limit': limit},
        f'turn-cleanup:{cutoff}:{limit}'))


__all__ = [
    'LifecycleConflict', 'LifecycleNotFound', 'TERMINAL_STATUSES',
    'create_turn_pair', 'append_settled_turn', 'announce_related_turns',
    'create_attempt', 'attempt_dispatch_lock', 'claim_attempt_start',
    'dispatch_attempt_to_worker',
    'bind_task', 'mark_task_started', 'fail_start',
    'record_task_event', 'sync_visible_run_turns',
    'list_turns', 'get_turn', 'get_attempt', 'list_dispatchable_attempts',
    'update_turn_projection',
    'create_branch_lane', 'delete_branch_lane',
    'delete_turns',
    'get_conversation_revision', 'read_events',
    'attempt_is_terminal', 'abort_attempt', 'build_api_messages',
    'backfill_turn_search_index', 'start_turn_search_backfill',
    'recover_running_attempts', 'cleanup_superseded_attempts',
]
