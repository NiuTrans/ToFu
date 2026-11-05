"""Per-turn ``x-codex-turn-state`` sticky-routing tokens for the Codex backend.

Responsibility: hold the opaque routing token the ChatGPT Codex backend issues
on the first response of a turn, so later requests of the SAME turn replay it.

Contract (openai/codex ``core/src/client.rs`` ``ModelClientSession.turn_state``):
the server sends the token once per turn (HTTP response header, or the
WebSocket ``codex.response.metadata`` headers map); the client resends it
unchanged on every later request of that turn (tool-loop continuations,
retries) and never on a different turn. The first observed value wins.

A Tofu turn is one task (``body['_task_id']``). Entry points: :func:`turn_state`
(read for the next request), :func:`observe_turn_state` (record a response
value), :func:`release_turn_state` (task-lane teardown). No I/O.
"""

from __future__ import annotations

import threading

TURN_STATE_HEADER = 'x-codex-turn-state'

_lock = threading.Lock()
_turn_states: dict[str, str] = {}


def turn_state(task_id: object) -> str:
    """Return the turn's routing token, or ``''`` before the first response."""
    key = str(task_id or '')
    if not key:
        return ''
    with _lock:
        return _turn_states.get(key, '')


def observe_turn_state(task_id: object, value: object) -> None:
    """Record a server-issued token; the first value of a turn is kept."""
    key = str(task_id or '')
    token = value.strip() if isinstance(value, str) else ''
    if not key or not token:
        return
    with _lock:
        _turn_states.setdefault(key, token)


def observe_turn_state_headers(task_id: object, headers) -> None:
    """Record the token from an HTTP response's (case-insensitive) headers."""
    if not task_id or headers is None:
        return
    for name, value in headers.items():
        if str(name).lower() == TURN_STATE_HEADER:
            observe_turn_state(task_id, value)
            return


def release_turn_state(task_id: object) -> None:
    """Forget a finished turn so its token can never reach another turn."""
    key = str(task_id or '')
    if not key:
        return
    with _lock:
        _turn_states.pop(key, None)


__all__ = [
    'TURN_STATE_HEADER', 'observe_turn_state', 'observe_turn_state_headers',
    'release_turn_state', 'turn_state',
]
