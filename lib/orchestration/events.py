"""Canonical runtime-event contract for orchestration producers/consumers.

The engine emits plain dictionaries because they cross live, durable and chat
adapters.  This registry keeps the protocol decisions that used to be spread
across those adapters in one framework-free place: whether an event is durable,
whether clients must reduce it into state, whether it belongs in a timeline,
and which run-header status transition it implies.
"""

from __future__ import annotations

import copy

from lib.orchestration.contract_schema import contract_snapshot_schema
from lib.orchestration.wire_formats import EVENTS_FORMAT as EVENT_SCHEMA

# The cross-process lifecycle contract owns election/status vocabulary.
from lib.orchestration.run_status import (
    CONTINUITY_EVENT_TYPES as CONTINUITY_EVENT_TYPES, CONTINUITY_ELECTION_CLOSED_EVENT_TYPES as CONTINUITY_ELECTION_CLOSED_EVENT_TYPES,
    CONTINUITY_RESTART_ERROR as CONTINUITY_RESTART_ERROR,
)

EXECUTION_JOURNAL_EVENT_TYPES = (
    'execution_journal_start', 'execution_journal_ready',
    'execution_effect_prepared', 'execution_effect_started', 'execution_effect_completed',
    'execution_account_frontier',
)

EVENT_PREVIEW_CHARS = 200
EVENT_TIMELINE_PREVIEW_CHARS = 120

# Dict insertion order is the documented protocol order: lifecycle first,
# node execution, control-flow signals, human gates, then transient/detail
# frames.  Unknown future events fail open for persistence in ``is_durable``.
_EVENT_SPECS: dict[str, dict] = {
    **{name: {'durable': True, 'reduce': False, 'timeline': False}
       for name in (*EXECUTION_JOURNAL_EVENT_TYPES, 'execution_recovery_claim', 'execution_recovery_consumed')},
    **{name: {'durable': True, 'reduce': False, 'timeline': False}
       for name in CONTINUITY_EVENT_TYPES},
    'fanout_expanded': {'durable': True, 'reduce': True, 'timeline': True},
    'fanout_complete': {'durable': True, 'reduce': True, 'timeline': True},
    'flow_start': {
        'durable': True, 'reduce': True, 'timeline': True,
        'runStatus': 'running',
    },
    'flow_complete': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'step_start': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'step_complete': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'error': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'loop_start': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'loop_iteration': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'zero_deliverable_guard': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'goal_completion_evidence_missing': {
        # Durable audit evidence for GoalRun classification. It does not
        # mutate generic Studio graph state or require a presentation string.
        'durable': True, 'reduce': False, 'timeline': False,
    },
    'goal_stop_rejected': {
        # A model-requested stop is only a proposal. Preserve why the runtime
        # continued, but keep this GoalRun audit fact out of generic Studio
        # reduction and the end-user timeline.
        'durable': True, 'reduce': False, 'timeline': False,
    },
    'goal_budget_checkpoint': {
        # A Goal budget window closed and was extended (or the stall breaker
        # fired). Durable audit evidence that budget alone never stopped the
        # run; the live status chip surfaces it, generic Studio reduction and
        # the end-user timeline do not.
        'durable': True, 'reduce': False, 'timeline': False,
    },
    'goal_blocked_rejected': {
        # A VU [VERDICT: BLOCKED] claim failed one acceptance gate (receipt,
        # remaining, justification, challenge). Preserve why the runtime
        # continued; same audit-only treatment as goal_stop_rejected.
        'durable': True, 'reduce': False, 'timeline': False,
    },
    'stuck_detected': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'no_progress': {
        'durable': True, 'reduce': True, 'timeline': True,
    },
    'replan': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'parallel_start': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'branch_pick': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'artifact_declared': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'human_notify': {
        'durable': True, 'reduce': False, 'timeline': True,
    },
    'human_request': {
        'durable': True, 'reduce': True, 'timeline': True,
        'runStatus': 'paused', 'gateEffect': 'open',
    },
    'human_resolved': {
        'durable': True, 'reduce': True, 'timeline': True,
        'runStatus': 'running', 'gateEffect': 'close',
    },
    # Token and in-flight phase frames are useful only on the live surface.
    # Persisting either creates replay noise; step_complete/step_trace carry
    # the self-contained durable result.
    'step_phase': {
        'durable': False, 'reduce': True, 'timeline': False,
    },
    'step_delta': {
        'durable': False, 'reduce': True, 'timeline': False,
    },
    'step_event': {
        # Ordinary invocation checkpoints and round/reset boundaries. The
        # chat Turn persists them; Studio replays step_complete instead.
        'durable': False, 'reduce': False, 'timeline': False,
    },
    'step_tool_event': {
        # Live leaf-tool frames are projected onto the current chat turn.
        # Persisting them here would duplicate the bounded, self-contained
        # tool log carried by step_trace/step_complete during replay.
        'durable': False, 'reduce': True, 'timeline': False,
    },
    'step_trace': {
        'durable': True, 'reduce': True, 'timeline': False,
    },
}


def runtime_event_contract() -> dict:
    """Return a detached, transport-safe snapshot of the event protocol."""
    return {
        'schema': EVENT_SCHEMA,
        'previewLimits': {
            'wire': EVENT_PREVIEW_CHARS,
            'timeline': EVENT_TIMELINE_PREVIEW_CHARS,
        },
        'types': copy.deepcopy(_EVENT_SPECS),
    }


def runtime_event_contract_schema() -> dict:
    """Return an OpenAPI-compatible schema derived from the event registry."""
    return contract_snapshot_schema(
        runtime_event_contract(), open_object_paths=[('types',)])


def event_preview(value: object) -> str:
    """Project bounded event preview copy through the public wire limit."""
    return str(value or '')[:EVENT_PREVIEW_CHARS]


def event_spec(event_type: str) -> dict | None:
    """Return the immutable internal spec for one type, if registered."""
    return _EVENT_SPECS.get(str(event_type or ''))


def is_durable_event(event_type: str) -> bool:
    """Whether an event belongs in replay storage.

    Unknown types deliberately fail open: a rolling backend must not destroy
    a new event merely because an older registry missed it.  Frontend clients
    still render an explicit unknown-event row rather than dropping it.
    """
    spec = event_spec(event_type)
    return True if spec is None else bool(spec['durable'])


def event_run_status(event_type: str) -> str:
    """Return the run-header lifecycle transition implied by an event."""
    spec = event_spec(event_type)
    return str((spec or {}).get('runStatus') or '')


def event_gate_effect(event_type: str) -> str:
    """Return ``open``/``close`` for one gate-presence event type."""
    effect = str((event_spec(event_type) or {}).get('gateEffect') or '')
    return effect if effect in {'open', 'close'} else ''


def live_event_projection(event: dict) -> dict:
    """Keep recovery bodies durable without duplicating them in live buffers/push."""
    if event.get('type') == 'execution_effect_completed':
        return {key: value for key, value in event.items() if key != 'result'}
    if event.get('type') == 'execution_account_frontier':
        return {key: value for key, value in event.items() if key != 'frontier'}
    return event
