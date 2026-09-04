"""Authoritative objective extraction for GoalRun launches.

The accepted conversation command may stamp the exact human input on the
immutable task config.  History scanning is a fallback for stored/built-in
Flow launches and deliberately ignores synthetic user-side projections.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from lib.goal_runs.contract import MAX_GOAL_OBJECTIVE_CHARS


_SYNTHETIC_USER_MARKERS = (
    '_isVirtualUser', '_isFlowReview', '_isVuDirective', '_isMeta',
    '_goalContinuation',
)


def projection_text(value: Any) -> str:
    """Extract bounded text from a turn/message projection."""
    if isinstance(value, str):
        return value.strip()[:MAX_GOAL_OBJECTIVE_CHARS]
    if not isinstance(value, Mapping):
        return ''
    content = value.get('content')
    if content is None:
        content = value.get('text')
    if isinstance(content, str):
        return content.strip()[:MAX_GOAL_OBJECTIVE_CHARS]
    if isinstance(content, list):
        text = '\n'.join(
            str(block.get('text') or '')
            for block in content
            if isinstance(block, Mapping) and block.get('type') == 'text'
        ).strip()
        return text[:MAX_GOAL_OBJECTIVE_CHARS]
    return ''


def is_real_human_projection(message: Any) -> bool:
    """Return whether a user-side message is eligible to own an objective."""
    if not isinstance(message, Mapping) or message.get('role') != 'user':
        return False
    return not any(bool(message.get(marker)) for marker in _SYNTHETIC_USER_MARKERS)


def objective_from_task(task: Mapping[str, Any]) -> str:
    """Resolve the exact accepted objective, never the first chat message."""
    config = task.get('config')
    if isinstance(config, Mapping):
        stamped = config.get('_goalObjective')
        if isinstance(stamped, str) and stamped.strip():
            return stamped.strip()[:MAX_GOAL_OBJECTIVE_CHARS]
    for message in reversed(task.get('messages') or []):
        if is_real_human_projection(message):
            text = projection_text(message)
            if text:
                return text
    return ''


__all__ = [
    'is_real_human_projection',
    'objective_from_task',
    'projection_text',
]

