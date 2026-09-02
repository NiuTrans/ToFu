"""Normalize retired orchestration artifacts at read boundaries.

Responsibility: translate persisted messages written by the removed endpoint
runner into the canonical Flow marker vocabulary.  Current producers must
never write the legacy keys; consumers call :func:`normalize_flow_message`
before interpreting historical rows.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_LEGACY_FLOW_FIELD_MAP = {
    '_isEndpointPlanner': '_isFlowPlanner',
    '_isEndpointReview': '_isFlowReview',
    '_epIteration': '_flowIteration',
    '_epPlannerIteration': '_flowPlannerIteration',
    '_epApproved': '_flowApproved',
    '_epNextPhase': '_flowNextPhase',
    '_epStateChangingCount': '_flowStateChangingCount',
}
FLOW_EVENT_PREFIX = 'flow_'
LEGACY_FLOW_EVENT_PREFIX = 'endpoint_'
FLOW_EVENT_PREFIXES = (FLOW_EVENT_PREFIX, LEGACY_FLOW_EVENT_PREFIX)
_FLOW_TURN_KIND_PREFIXES = ('flow_', 'autopilot_', 'endpoint_')


def normalize_flow_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return one message using only canonical Flow marker names.

    Canonical values win if a partially migrated row contains both spellings.
    Legacy keys are consumed rather than echoed, keeping compatibility local to
    this module instead of extending the retired protocol through the runtime.
    """
    normalized = dict(message)
    for legacy_name, canonical_name in _LEGACY_FLOW_FIELD_MAP.items():
        if canonical_name not in normalized and legacy_name in normalized:
            normalized[canonical_name] = normalized[legacy_name]
        normalized.pop(legacy_name, None)
    return normalized


def is_flow_event_type(event_type: Any) -> bool:
    """Recognize current Flow events plus persisted events from the old loop."""
    value = str(event_type or '')
    return value.startswith(FLOW_EVENT_PREFIXES)


def is_flow_turn_kind(kind: Any) -> bool:
    """Recognize current Flow turn kinds plus persisted old-loop kinds."""
    return str(kind or '').startswith(_FLOW_TURN_KIND_PREFIXES)


__all__ = [
    'FLOW_EVENT_PREFIX',
    'FLOW_EVENT_PREFIXES',
    'LEGACY_FLOW_EVENT_PREFIX',
    'is_flow_event_type',
    'is_flow_turn_kind',
    'normalize_flow_message',
]
