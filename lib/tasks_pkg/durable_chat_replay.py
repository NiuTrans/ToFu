"""Owner-scoped durable chat task state and cursor replay.

This leaf service is the cold counterpart of the process-local chat
``TaskRuntime``.  It reads only declared storage operations, projects one
public task shape, and preserves producer-owned absolute event sequences.
Routes depend on this service instead of decoding ``task_results`` records or
inventing storage fallbacks themselves.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.error_envelope import from_json as error_from_json
from lib.log import get_logger
from lib.task_replay import (
    TASK_REPLAY_TERMINAL_EVENT_TYPES,
    TASK_REPLAY_TERMINAL_STATUSES,
    TaskReplayPage,
    project_bounded_replay_payload,
    safe_replay_cursor,
)
from lib.tasks_pkg.event_log import read_event_bounds, read_events


logger = get_logger(__name__)

DURABLE_CHAT_REPLAY_FETCH_LIMIT = 129

# These fields are bounded public projections produced by ``build_result_meta``.
# Identity/fencing/routing fields and the dedicated Flow trace remain in their
# owning surfaces instead of leaking through the generic task endpoint.
_PUBLIC_RESULT_METADATA_FIELDS = (
    'finishReason',
    'usage',
    'preset',
    'toolSummary',
    'model',
    'provider_id',
    'routeSnapshot',
    'thinkingDepth',
    'costExperiment',
    'programRuns',
    'toolOrchestrationDecisions',
    'programmaticAdoptionNudges',
    'toolRoundTripNudges',
    'todoState',
    'todoBlocked',
    'waitingOn',
    'apiRounds',
    'compactionUsage',
    'promptAdmission',
    'modifiedFiles',
    'modifiedFileList',
    'flowMode',
    'flowPhase',
    'flowIteration',
    'flowProjection',
    'turnRole',
    'emits',
    'vuMsgId',
    'autopilotRunId',
    'flowStopReason',
)


def _positive_owner(value: Any) -> int | None:
    try:
        owner = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return owner if owner > 0 else None


def _nonnegative_milliseconds(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _json_object(value: Any, *, task_id: str, field: str) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning(
            '[DurableTaskReplay] invalid %s task=%s: %s',
            field,
            task_id[:8],
            exc,
        )
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _owned_task_replay_record(
    task_id: str,
    *,
    user_id: int,
    include_terminal_payload: bool = False,
    include_metadata: bool = False,
) -> tuple[dict, int] | None:
    """Read the compact Sidecar replay projection for one exact owner."""
    if not isinstance(task_id, str) or not task_id:
        return None
    expected_owner = _positive_owner(user_id)
    if expected_owner is None:
        return None
    from lib.storage import get_storage_client

    value = get_storage_client().query(
        'task_results.replay_get', {
            'key': task_id,
            'user_id': expected_owner,
            'include_terminal_payload': bool(include_terminal_payload),
            'include_metadata': bool(include_metadata),
        },
        deadline=30,
    )
    if not isinstance(value, Mapping):
        return None
    value = dict(value)
    return value, _nonnegative_milliseconds(value.get('updated_at_ms'))


def persisted_chat_task_owner_matches(task_id: str, *, user_id: int) -> bool:
    """Check root/swarm-child access through the parent's durable owner row."""
    parent_task_id = task_id.partition('#agent:')[0] or task_id
    return _owned_task_replay_record(
        parent_task_id, user_id=user_id) is not None


@dataclass(frozen=True, slots=True)
class DurableChatTaskSnapshot:
    """One owner-checked task-result row plus its durable replay boundary."""

    task_id: str
    user_id: int
    status: str
    conv_id: str
    content: str
    thinking: str
    error: dict | None
    metadata: dict
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int
    event_replay: dict[str, int]
    terminal_payload_loaded: bool

    @property
    def terminal(self) -> bool:
        return self.status in TASK_REPLAY_TERMINAL_STATUSES

    def public_task(self) -> dict:
        """Return the public task-state mapping consumed by ``_public_task``."""
        if not self.terminal_payload_loaded:
            raise RuntimeError(
                'public task state requires the complete task-result row')
        created_at = self.created_at_ms / 1000 if self.created_at_ms else 0.0
        updated_at = self.updated_at_ms / 1000 if self.updated_at_ms else (
            created_at)
        finished_at = (
            self.completed_at_ms / 1000
            if self.terminal and self.completed_at_ms else None
        )
        task = {
            'id': self.task_id,
            'kind': 'chat',
            'status': self.status,
            'convId': self.conv_id,
            'content': self.content,
            'thinking': self.thinking,
            'error': self.error,
            'created_at': created_at,
            'updated_at': updated_at,
            'finished_at': finished_at,
            'event_replay': dict(self.event_replay),
            'meta': {'convId': self.conv_id} if self.conv_id else {},
        }
        for field in _PUBLIC_RESULT_METADATA_FIELDS:
            if field in self.metadata:
                task[field] = self.metadata[field]
        return task

    def replay_payload(self, cursor: Any) -> dict:
        """Read one bounded cold page and return the shared replay wire shape."""
        requested = safe_replay_cursor(cursor)
        base_cursor = int(self.event_replay['base_cursor'])
        boundary = int(self.event_replay['next_cursor'])
        effective = max(base_cursor, min(requested, boundary))
        rows = []
        if effective < boundary:
            rows = read_events(
                self.task_id,
                since_event_id=effective - 1,
                limit=DURABLE_CHAT_REPLAY_FETCH_LIMIT,
                raise_on_error=True,
            )

        events = []
        for row in rows:
            sequence = int(row.get('event_id'))
            # ``event.bounds`` and this page are separate read transactions.
            # Do not mix a concurrently appended row into the older boundary;
            # a running reader refreshes the snapshot on its next iteration.
            if sequence < effective or sequence >= boundary:
                continue
            payload = row.get('payload')
            if not isinstance(payload, Mapping):
                raise ValueError(
                    f'durable task event {sequence} is not an object')
            event = dict(payload)
            event['seq'] = sequence
            events.append(event)

        extras = {
            'taskId': self.task_id,
            'createdAt': self.created_at_ms,
            'updatedAt': self.updated_at_ms or self.created_at_ms,
        }
        request_id = self.metadata.get('requestId')
        if request_id:
            extras['requestId'] = request_id
        model = self.metadata.get('model')
        if model:
            extras['model'] = model
        if self.terminal:
            extras['finishedAt'] = self.completed_at_ms
            if self.error is not None:
                extras['error'] = self.error

        page = TaskReplayPage(
            events=events,
            next_cursor=boundary,
            run_status=self.status,
            done=self.terminal,
            requested_cursor=requested,
            cursor_reset=effective != requested,
        )
        payload = page.payload(extras)
        return (
            self.enrich_terminal_payload(payload)
            if self.terminal_payload_loaded else payload
        )

    def with_terminal_payload(self) -> 'DurableChatTaskSnapshot':
        """Fetch cumulative content exactly once when a reader catches up."""
        if self.terminal_payload_loaded:
            return self
        owned = _owned_task_replay_record(
            self.task_id,
            user_id=self.user_id,
            include_terminal_payload=True,
        )
        if owned is None:
            raise LookupError('durable task result disappeared during replay')
        value, record_updated_at_ms = owned
        return _snapshot_from_value(
            self.task_id,
            user_id=self.user_id,
            value=value,
            record_updated_at_ms=record_updated_at_ms,
            event_replay=self.event_replay,
            terminal_payload_loaded=True,
        )

    def bounded_replay_payload(
        self,
        cursor: Any,
    ) -> tuple['DurableChatTaskSnapshot', dict]:
        """Project one HTTP/SSE page and upgrade only a caught-up terminal."""
        replay_payload = self.replay_payload(cursor)
        projected = project_bounded_replay_payload(replay_payload)
        snapshot = self
        if self.terminal and projected.get('caught_up'):
            snapshot = self.with_terminal_payload()
            projected = project_bounded_replay_payload(
                snapshot.enrich_terminal_payload(replay_payload))
        return snapshot, projected

    def enrich_terminal_payload(self, payload: dict) -> dict:
        """Attach cumulative truth only to a caught-up terminal projection."""
        if not self.terminal or not self.terminal_payload_loaded:
            return payload
        enriched = dict(payload)
        if self.content:
            enriched['content'] = self.content
        if self.thinking:
            enriched['thinking'] = self.thinking
        if self.error is not None:
            enriched['error'] = self.error
        events = []
        for source in payload.get('events') or []:
            event = dict(source) if isinstance(source, Mapping) else source
            if (isinstance(event, dict)
                    and event.get('type') in TASK_REPLAY_TERMINAL_EVENT_TYPES):
                # Streaming deltas may already be reclaimed. The terminal
                # frame therefore carries the cumulative durable projection.
                if self.content:
                    event.setdefault('content', self.content)
                if self.thinking:
                    event.setdefault('thinking', self.thinking)
                if self.error is not None:
                    event.setdefault('error', self.error)
            events.append(event)
        enriched['events'] = events
        return enriched


def _snapshot_from_value(
    task_id: str,
    *,
    user_id: int,
    value: Mapping[str, Any],
    record_updated_at_ms: int,
    event_replay: dict[str, int],
    terminal_payload_loaded: bool,
) -> DurableChatTaskSnapshot:
    metadata = _json_object(
        value.get('metadata'), task_id=task_id, field='metadata')
    status = str(value.get('status') or 'running').lower()
    created_at_ms = _nonnegative_milliseconds(value.get('created_at'))
    completed_at_ms = _nonnegative_milliseconds(value.get('completed_at'))
    updated_at_ms = record_updated_at_ms or completed_at_ms or created_at_ms
    return DurableChatTaskSnapshot(
        task_id=task_id,
        user_id=int(user_id),
        status=status,
        conv_id=str(value.get('conv_id') or ''),
        content=(value.get('content')
                 if isinstance(value.get('content'), str) else ''),
        thinking=(value.get('thinking')
                  if isinstance(value.get('thinking'), str) else ''),
        error=error_from_json(value.get('error')),
        metadata=metadata,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        completed_at_ms=completed_at_ms,
        event_replay=dict(event_replay),
        terminal_payload_loaded=bool(terminal_payload_loaded),
    )


def load_durable_chat_task(
    task_id: str,
    *,
    user_id: int,
) -> DurableChatTaskSnapshot | None:
    """Load one owner-scoped chat snapshot and its exact event summary."""
    owned = _owned_task_replay_record(
        task_id,
        user_id=user_id,
        include_terminal_payload=True,
        include_metadata=True,
    )
    if owned is None:
        return None
    value, record_updated_at_ms = owned
    return _snapshot_from_value(
        task_id,
        user_id=user_id,
        value=value,
        record_updated_at_ms=record_updated_at_ms,
        event_replay=read_event_bounds(task_id),
        terminal_payload_loaded=True,
    )


def load_durable_chat_replay(
    task_id: str,
    *,
    user_id: int,
) -> DurableChatTaskSnapshot | None:
    """Load compact owner/status state for events/SSE without answer bytes."""
    owned = _owned_task_replay_record(task_id, user_id=user_id)
    if owned is None:
        return None
    value, record_updated_at_ms = owned
    return _snapshot_from_value(
        task_id,
        user_id=user_id,
        value=value,
        record_updated_at_ms=record_updated_at_ms,
        event_replay=read_event_bounds(task_id),
        terminal_payload_loaded=False,
    )


__all__ = [
    'DURABLE_CHAT_REPLAY_FETCH_LIMIT',
    'DurableChatTaskSnapshot',
    'load_durable_chat_replay',
    'load_durable_chat_task',
    'persisted_chat_task_owner_matches',
]
