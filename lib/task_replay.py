"""Versioned cursor replay pages shared by task runtimes and durable logs.

Cursor ownership belongs to the producer.  Clients may reconnect with a stale,
negative, or even future cursor; the backend must clamp that request to the
authoritative log boundary so a bad cursor cannot permanently skip later
events.  This module owns the wire shape independently of Flask and storage.
"""

from __future__ import annotations

import copy
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:
    # Runtime access stays lazy through __getattr__ so importing replay helpers
    # does not initialize the orchestration package.  This declaration makes
    # the public re-export explicit to static analyzers as well.
    from lib.orchestration.wire_formats import TASK_REPLAY_FORMAT

from lib.log import get_logger
logger = get_logger(__name__)
TASK_REPLAY_NOT_FOUND = 'not_found'
TASK_REPLAY_EVENT_TYPE_FIELD = 'type'
TASK_REPLAY_EVENT_SEQUENCE_FIELD = 'seq'
TASK_REPLAY_EVENTS_FIELD = 'events'
TASK_REPLAY_EVENT_REQUIRED_FIELDS = (
    TASK_REPLAY_EVENT_TYPE_FIELD,
    TASK_REPLAY_EVENT_SEQUENCE_FIELD,
)
TASK_REPLAY_UNKNOWN_EVENT_TYPES = 'allow'
TASK_REPLAY_TERMINAL_EVENT_TYPES = ('done', 'error', 'aborted')
TASK_REPLAY_STATUS_FIELD = 'status'
TASK_REPLAY_NEXT_CURSOR_FIELD = 'next_cursor'
TASK_REPLAY_TERMINAL_FIELD = 'done'
TASK_REPLAY_CAUGHT_UP_FIELD = 'caught_up'
TASK_REPLAY_CURSOR_FIELD = 'cursor'
TASK_REPLAY_CURSOR_REQUESTED_FIELD = 'requested'
TASK_REPLAY_CURSOR_NEXT_FIELD = 'next'
TASK_REPLAY_CURSOR_RESET_FIELD = 'reset'
TASK_REPLAY_QUERY_FIELD = 'cursor'
TASK_REPLAY_QUERY_MINIMUM = 0
TASK_REPLAY_QUERY_DEFAULT = 0
TASK_REPLAY_PAGE_FIELDS = (
    'format',
    'ok',
    TASK_REPLAY_EVENTS_FIELD,
    TASK_REPLAY_NEXT_CURSOR_FIELD,
    TASK_REPLAY_STATUS_FIELD,
    TASK_REPLAY_TERMINAL_FIELD,
    TASK_REPLAY_CURSOR_FIELD,
)


def _task_replay_format() -> str:
    """Resolve the shared identifier without initializing orchestration early."""
    from lib.orchestration.wire_formats import TASK_REPLAY_FORMAT

    return TASK_REPLAY_FORMAT


def __getattr__(name: str):
    if name == 'TASK_REPLAY_FORMAT':
        return _task_replay_format()
    raise AttributeError(name)


def safe_replay_cursor(value: Any) -> int:
    try:
        return max(TASK_REPLAY_QUERY_MINIMUM, int(
            value or TASK_REPLAY_QUERY_DEFAULT))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[TaskReplay] invalid replay cursor %r: %s', value, exc)
        return TASK_REPLAY_QUERY_DEFAULT


def task_replay_request_contract() -> dict:
    """Publish the shared HTTP cursor query accepted by replay producers."""
    return {
        'queryField': TASK_REPLAY_QUERY_FIELD,
        'minimum': TASK_REPLAY_QUERY_MINIMUM,
        'default': TASK_REPLAY_QUERY_DEFAULT,
        'description': 'Producer-owned next event sequence.',
    }


@dataclass(frozen=True)
class TaskReplayPage:
    events: list[dict]
    next_cursor: int
    run_status: str
    done: bool
    requested_cursor: int = 0
    cursor_reset: bool = False
    ok: bool = True
    error: Any = None

    @property
    def status(self) -> str:
        return self.run_status

    @property
    def first_cursor(self) -> int:
        """Absolute sequence of the first event in this page.

        ``next_cursor`` is producer-owned and points one past the final event;
        deriving the page start from it keeps adapters independent from the
        physical offset of a bounded/rolling in-memory buffer.
        """
        return max(0, int(self.next_cursor) - len(self.events))

    @property
    def frames(self) -> list[tuple[int, dict]]:
        """Events paired with their absolute producer sequence."""
        return [
            (self.first_cursor + offset, event)
            for offset, event in enumerate(self.events)
        ]

    def payload(self, extras: dict | None = None) -> dict:
        payload = {
            'format': _task_replay_format(),
            'ok': bool(self.ok),
            TASK_REPLAY_EVENTS_FIELD: self.events,
            TASK_REPLAY_NEXT_CURSOR_FIELD: max(0, int(self.next_cursor)),
            TASK_REPLAY_STATUS_FIELD: str(self.run_status or ''),
            TASK_REPLAY_TERMINAL_FIELD: bool(self.done),
            TASK_REPLAY_CURSOR_FIELD: {
                TASK_REPLAY_CURSOR_REQUESTED_FIELD: max(
                    0, int(self.requested_cursor)),
                TASK_REPLAY_CURSOR_NEXT_FIELD: max(0, int(self.next_cursor)),
                TASK_REPLAY_CURSOR_RESET_FIELD: bool(self.cursor_reset),
            },
        }
        if self.error is not None:
            payload['error'] = self.error
        if extras:
            payload.update(extras)
        return payload


def memory_replay_page(
    events: Iterable[dict],
    cursor: Any,
    *,
    status: str,
    done: bool,
    base_cursor: int = 0,
) -> TaskReplayPage:
    snapshot = list(events)
    requested = safe_replay_cursor(cursor)
    base = max(0, int(base_cursor or 0))
    boundary = base + len(snapshot)
    effective = max(base, min(requested, boundary))
    return TaskReplayPage(
        events=snapshot[effective - base:],
        next_cursor=boundary,
        run_status=str(status or ''),
        done=bool(done),
        requested_cursor=requested,
        cursor_reset=effective != requested,
    )


def task_event_base_cursor(task: dict, events: Iterable[dict] | None = None) -> int:
    """Return the absolute sequence of a task's first retained event.

    New :class:`TaskRuntime` tasks carry ``_eventBaseSeq`` explicitly.  The
    event's own ``seq`` is preferred when present because it is the wire-level
    source of truth and also lets older/recovered task-shaped mappings join the
    absolute-cursor protocol without a migration write.  Empty buffers use the
    producer's next/base hint; legacy unsequenced buffers start at zero.

    Callers that also need the event snapshot should pass the list they read
    while holding ``events_lock`` so the base and length describe one atomic
    window.
    """
    source = (task.get('events') or []) if events is None else events
    snapshot = source if isinstance(source, (list, tuple)) else list(source)
    if snapshot:
        first = snapshot[0]
        if isinstance(first, dict):
            try:
                first_seq = int(first.get(TASK_REPLAY_EVENT_SEQUENCE_FIELD))
                if first_seq >= 0:
                    return first_seq
            except (TypeError, ValueError, OverflowError) as exc:
                logger.debug('invalid retained replay event sequence: %s', exc)
                pass
    for field in ('_eventBaseSeq', '_eventNextSeq'):
        try:
            value = int(task.get(field))
            if value >= 0:
                if field == '_eventNextSeq' and snapshot:
                    return max(0, value - len(snapshot))
                return value
        except (TypeError, ValueError, OverflowError) as exc:
            logger.debug('invalid replay cursor hint %s: %s', field, exc)
            continue
    return 0


def task_memory_replay_page(task: dict, cursor: Any) -> TaskReplayPage:
    """Atomically read one absolute-cursor page from a task-shaped mapping.

    This is the sole read seam for in-process stream adapters.  In particular,
    consumers must never use ``task['events'][cursor:]``: ``cursor`` is an
    absolute next-event sequence while ``events`` is a bounded rolling list
    whose physical index restarts at zero after every eviction.
    """
    lock = task.get('events_lock')
    with (lock if lock is not None else nullcontext()):
        events = task.get('events') or []
        base = task_event_base_cursor(task, events)
        status = str(task.get('status') or '')
        terminal = status in TASK_REPLAY_TERMINAL_EVENT_TYPES
        return memory_replay_page(
            events, cursor, status=status, done=terminal, base_cursor=base)


def sse_last_event_id_to_cursor(value: Any) -> int | None:
    """Translate SSE ``Last-Event-ID`` into an absolute next-event cursor.

    SSE carries the id of the last frame the client received, whereas the task
    replay protocol carries the sequence of the *next* event to send.  Keeping
    this off-by-one conversion in the replay owner prevents each transport
    adapter from inventing its own cursor convention.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        last_event_id = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('invalid SSE Last-Event-ID %r: %s', value, exc)
        return None
    if last_event_id < 0:
        return None
    return last_event_id + 1


def sse_resume_serviceable(
    last_event_id: Any,
    *,
    base_cursor: int,
    next_cursor: int,
) -> bool:
    """Whether a Last-Event-ID can be replayed from one retained window."""
    requested = sse_last_event_id_to_cursor(last_event_id)
    if requested is None:
        return False
    return max(0, int(base_cursor)) <= requested <= max(0, int(next_cursor))


def missing_replay_page(cursor: Any, *, error: Any = TASK_REPLAY_NOT_FOUND) \
        -> TaskReplayPage:
    requested = safe_replay_cursor(cursor)
    return TaskReplayPage(
        events=[],
        next_cursor=requested,
        run_status='',
        done=True,
        requested_cursor=requested,
        ok=False,
        error=error,
    )


def task_replay_http_status(payload: Any) -> int:
    """Project one replay payload onto its canonical HTTP status.

    Both in-memory ``TaskRuntime`` polling and durable orchestration replay
    expose the same wire protocol. Keep the transport mapping here as part of
    that protocol so route adapters cannot disagree about missing/error pages.
    """
    if not isinstance(payload, dict):
        return 500
    if payload.get('ok') is True:
        return 200
    if payload.get('error') == TASK_REPLAY_NOT_FOUND:
        return 404
    return 500


def task_terminal_event_type(status: Any) -> str:
    """Return the terminal replay event type for one lifecycle status.

    Terminal task statuses and their guaranteed final replay frames are the
    same protocol fact.  Keeping the validation here prevents runtimes and
    published contracts from drifting onto different vocabularies.
    """
    normalized = str(status or '')
    if normalized not in TASK_REPLAY_TERMINAL_EVENT_TYPES:
        raise ValueError(f'unsupported terminal task status: {normalized!r}')
    return normalized


def task_replay_contract() -> dict:
    return {
        'format': _task_replay_format(),
        'httpStatuses': {
            'success': 200,
            'notFound': 404,
            'failure': 500,
        },
        'notFoundReason': TASK_REPLAY_NOT_FOUND,
        'statusField': TASK_REPLAY_STATUS_FIELD,
        'nextCursorField': TASK_REPLAY_NEXT_CURSOR_FIELD,
        'pageFields': list(TASK_REPLAY_PAGE_FIELDS),
        'cursor': {
            **task_replay_request_contract(),
            'field': TASK_REPLAY_CURSOR_FIELD,
            'requestedField': TASK_REPLAY_CURSOR_REQUESTED_FIELD,
            'nextField': TASK_REPLAY_CURSOR_NEXT_FIELD,
            'resetField': TASK_REPLAY_CURSOR_RESET_FIELD,
            'unit': 'next event sequence',
            'producerOwned': True,
            'futureCursorReset': True,
        },
        'terminalField': TASK_REPLAY_TERMINAL_FIELD,
        'caughtUpField': TASK_REPLAY_CAUGHT_UP_FIELD,
        'eventsField': TASK_REPLAY_EVENTS_FIELD,
        'eventTypeField': TASK_REPLAY_EVENT_TYPE_FIELD,
        'eventSequenceField': TASK_REPLAY_EVENT_SEQUENCE_FIELD,
        'eventRequiredFields': list(TASK_REPLAY_EVENT_REQUIRED_FIELDS),
        'unknownEventTypes': TASK_REPLAY_UNKNOWN_EVENT_TYPES,
        'terminalEventTypes': list(TASK_REPLAY_TERMINAL_EVENT_TYPES),
        'terminalSnapshot': {
            'field': 'run',
            'when': {'field': 'done', 'equals': True},
            'optional': True,
        },
    }


def task_replay_contract_schema() -> dict:
    """Describe replay cursor policy from its executable contract."""
    from lib.orchestration.contract_schema import contract_snapshot_schema

    return contract_snapshot_schema(task_replay_contract())


def task_replay_cursor_schema() -> dict:
    """Describe the producer-owned cursor carried by every replay page."""
    cursor = task_replay_contract()['cursor']
    return {
        'type': 'object',
        'required': [
            cursor['requestedField'], cursor['nextField'],
            cursor['resetField'],
        ],
        'properties': {
            cursor['requestedField']: {'type': 'integer', 'minimum': 0},
            cursor['nextField']: {'type': 'integer', 'minimum': 0},
            cursor['resetField']: {'type': 'boolean'},
        },
    }


def task_replay_event_schema() -> dict:
    """Describe open replay events from the engine and terminal registries."""
    from lib.orchestration.events import runtime_event_contract

    event_contract = runtime_event_contract()
    replay_contract = task_replay_contract()
    type_field = replay_contract['eventTypeField']
    sequence_field = replay_contract['eventSequenceField']
    known_types = list(dict.fromkeys([
        *event_contract['types'],
        *replay_contract['terminalEventTypes'],
    ]))
    return {
        'type': 'object',
        'required': replay_contract['eventRequiredFields'],
        'properties': {
            type_field: {
                'type': 'string',
                'minLength': 1,
                'x-knownValues': known_types,
                'x-unknownValuePolicy': replay_contract['unknownEventTypes'],
            },
            sequence_field: {'type': 'integer', 'minimum': 0},
        },
        'additionalProperties': True,
        'x-eventSchema': event_contract['schema'],
    }


def task_replay_response_schema(
    *,
    missing: bool = False,
    snapshot_schema: dict | None = None,
    extra_properties: dict | None = None,
    extra_required: tuple[str, ...] = (),
    message_required: bool = False,
) -> dict:
    """Project a replay envelope with adapter-specific optional extras."""
    from lib.orchestration.run_status import run_status_contract

    contract = task_replay_contract()
    terminal_field = contract['terminalField']
    events_field = contract['eventsField']
    status_field = contract['statusField']
    next_cursor_field = contract['nextCursorField']
    cursor_field = contract['cursor']['field']
    properties = {
        'format': {'type': 'string', 'enum': [contract['format']]},
        'ok': {'type': 'boolean', 'const': not missing},
        events_field: {
            'type': 'array',
            'items': task_replay_event_schema(),
        },
        next_cursor_field: {'type': 'integer', 'minimum': 0},
        status_field: (
            {'type': 'string', 'const': ''} if missing else {
                'type': 'string',
                'enum': run_status_contract()['statuses'],
            }
        ),
        terminal_field: {
            'type': 'boolean',
            **({'const': True} if missing else {}),
        },
        cursor_field: task_replay_cursor_schema(),
    }
    required = list(contract['pageFields'])
    if missing:
        properties.update({
            'error': {
                'type': 'string', 'enum': [contract['notFoundReason']],
            },
            'message': {'type': 'string'},
        })
        required.append('error')
        if message_required:
            required.append('message')
    else:
        snapshot_field = contract['terminalSnapshot']['field']
        if snapshot_schema is not None:
            properties[snapshot_field] = copy.deepcopy(snapshot_schema)
        properties.update(copy.deepcopy(extra_properties or {}))
        required.extend(extra_required)
    return {
        'type': 'object',
        'required': required,
        'properties': properties,
    }


def live_task_replay_response_schema(*, missing: bool = False) -> dict:
    """Describe the in-memory TaskRuntime replay envelope.

    These clock and terminal-result fields are emitted by every live task
    runtime, independently of whichever HTTP route exposes the page.
    """
    if missing:
        return task_replay_response_schema(missing=True)
    return task_replay_response_schema(
        extra_properties={
            'taskId': {'type': 'string', 'minLength': 1},
            'requestId': {'type': 'string', 'minLength': 1},
            'createdAt': {'type': ['integer', 'null'], 'minimum': 0},
            'updatedAt': {'type': ['integer', 'null'], 'minimum': 0},
            'finishedAt': {'type': ['integer', 'null'], 'minimum': 0},
            'model': {'type': 'string'},
            'artifact_quality': {'type': 'object'},
            'result': {},
            'error': {'type': 'object'},
        },
        extra_required=('taskId', 'createdAt', 'updatedAt'),
    )


__all__ = [
    'TASK_REPLAY_FORMAT', 'TASK_REPLAY_NOT_FOUND',
    'TASK_REPLAY_EVENT_TYPE_FIELD', 'TASK_REPLAY_EVENT_SEQUENCE_FIELD',
    'TASK_REPLAY_EVENTS_FIELD',
    'TASK_REPLAY_EVENT_REQUIRED_FIELDS', 'TASK_REPLAY_UNKNOWN_EVENT_TYPES',
    'TASK_REPLAY_TERMINAL_EVENT_TYPES', 'TaskReplayPage',
    'TASK_REPLAY_STATUS_FIELD', 'TASK_REPLAY_NEXT_CURSOR_FIELD',
    'TASK_REPLAY_TERMINAL_FIELD', 'TASK_REPLAY_CURSOR_FIELD',
    'TASK_REPLAY_CAUGHT_UP_FIELD',
    'TASK_REPLAY_CURSOR_REQUESTED_FIELD', 'TASK_REPLAY_CURSOR_NEXT_FIELD',
    'TASK_REPLAY_CURSOR_RESET_FIELD', 'TASK_REPLAY_PAGE_FIELDS',
    'TASK_REPLAY_QUERY_FIELD', 'TASK_REPLAY_QUERY_MINIMUM',
    'TASK_REPLAY_QUERY_DEFAULT',
    'safe_replay_cursor', 'memory_replay_page', 'missing_replay_page',
    'task_event_base_cursor', 'task_memory_replay_page',
    'sse_last_event_id_to_cursor', 'sse_resume_serviceable',
    'task_replay_http_status', 'task_terminal_event_type',
    'task_replay_request_contract',
    'task_replay_contract', 'task_replay_contract_schema',
    'task_replay_cursor_schema', 'task_replay_event_schema',
    'task_replay_response_schema', 'live_task_replay_response_schema',
]
