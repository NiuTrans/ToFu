"""Task-local lifecycle for serial-read gateway escalation.

Responsibility
--------------
Turn a proven sequence of successful, reviewed read-only calls into one
single-request local gateway trial.  This module owns only the transient latch
and bounded diagnostics.  Tool visibility is projected later by
``lib.tools.gateway``; execution authority, schema validation, approval, and
settlement stay in the ordinary tool pipeline.

Entry points
------------
``activate_serial_gateway`` is called after authoritative tool receipts exist.
``resolve_programmatic_exposure`` is called before each provider request.

Dependencies
------------
The provider-neutral user-message classifier in
``tool_orchestration_policy``.  No provider, route, repository, or storage
module is imported here.
"""

from __future__ import annotations

from typing import Any

from lib.tasks_pkg.tool_orchestration_policy import genuine_user_message_count


PROGRAMMATIC_EXPOSURE_ADDITIVE = 'additive'
PROGRAMMATIC_EXPOSURE_SERIAL_GATEWAY = 'serial_gateway'
PROGRAMMATIC_WIRE_EXPOSURE_GATEWAY_ONLY = 'gateway_only'

_STATE_KEY = '_programmaticSerialGateway'
_EVENTS_KEY = '_programmaticSerialGatewayEvents'
_MAX_EVENTS = 8


def _append_event(task: dict[str, Any], event: dict[str, Any]) -> None:
    rows = task.get(_EVENTS_KEY)
    if not isinstance(rows, list):
        rows = []
        task[_EVENTS_KEY] = rows
    rows.append(event)
    del rows[:-_MAX_EVENTS]


def activate_serial_gateway(
    task: dict[str, Any], messages: Any, *, round_num: int,
    chain: list[str],
) -> bool:
    """Latch gateway-only exposure after one proven serial read chain.

    ``round_num`` is zero-based and the caller must already have verified one
    successful authoritative receipt for every name in ``chain``. Repeated
    activation is idempotent while the trial is pending, so post-dispatch
    replay cannot create another wire epoch.
    """
    if task.get('_programmaticExposurePolicy') \
            != PROGRAMMATIC_EXPOSURE_SERIAL_GATEWAY:
        return False
    existing = task.get(_STATE_KEY)
    if isinstance(existing, dict) and existing.get('status') in {
            'pending', 'served'}:
        return False
    bounded_chain = [str(name)[:128] for name in chain[-6:] if str(name)]
    if not bounded_chain:
        return False
    state = {
        'status': 'pending',
        'activatedAfterRound': max(1, int(round_num) + 1),
        'targetRound': max(1, int(round_num) + 2),
        'messageCount': len(messages) if isinstance(messages, list) else 0,
        'genuineUserMessageCount': genuine_user_message_count(messages),
        'chainLength': len(bounded_chain),
        'tools': bounded_chain,
    }
    task[_STATE_KEY] = state
    _append_event(task, {
        'kind': 'activated',
        'afterRound': state['activatedAfterRound'],
        'targetRound': state['targetRound'],
        'chainLength': state['chainLength'],
        'tools': list(bounded_chain),
    })
    return True


def resolve_programmatic_exposure(
    task: dict[str, Any], messages: Any, *, round_num: int,
    requested_policy: str, programmatic_active: bool,
) -> tuple[str, str]:
    """Return ``(wire exposure, reason)`` and enforce reset boundaries."""
    normalized_policy = str(requested_policy or '').strip().lower()
    task['_programmaticExposurePolicy'] = normalized_policy
    if not programmatic_active:
        return PROGRAMMATIC_EXPOSURE_ADDITIVE, 'programmatic_inactive'
    if normalized_policy != PROGRAMMATIC_EXPOSURE_SERIAL_GATEWAY:
        task.pop(_STATE_KEY, None)
        return PROGRAMMATIC_EXPOSURE_ADDITIVE, 'additive_policy'

    state = task.get(_STATE_KEY)
    if not isinstance(state, dict) or state.get('status') not in {
            'pending', 'served'}:
        return PROGRAMMATIC_EXPOSURE_ADDITIVE, 'awaiting_serial_chain'

    activated_user_count = state.get('genuineUserMessageCount')
    try:
        activated_user_count = max(0, int(activated_user_count))
    except (TypeError, ValueError, OverflowError):
        activated_user_count = 0
    activated_message_count = state.get('messageCount')
    try:
        activated_message_count = max(0, int(activated_message_count))
    except (TypeError, ValueError, OverflowError):
        activated_message_count = 0
    if (isinstance(messages, list)
            and len(messages) >= activated_message_count):
        # The history grows monotonically during the ordinary task loop. Scan
        # only the suffix since activation so a very long tool run does not
        # repeatedly pay O(full history) just to detect a steering boundary.
        has_new_user_message = bool(genuine_user_message_count(
            messages[activated_message_count:]))
    else:
        # Compaction/history replacement may shorten the list; in that rare
        # case use the durable activation count as the conservative fallback.
        has_new_user_message = (
            genuine_user_message_count(messages) > activated_user_count)
    if has_new_user_message:
        _append_event(task, {
            'kind': 'reset',
            'reason': 'genuine_user_steering',
            'beforeRound': max(1, int(round_num) + 1),
        })
        task.pop(_STATE_KEY, None)
        return PROGRAMMATIC_EXPOSURE_ADDITIVE, 'genuine_user_steering'

    target_round = state.get('targetRound')
    try:
        target_round = max(1, int(target_round))
    except (TypeError, ValueError, OverflowError):
        target_round = max(1, int(round_num) + 1)
    current_round = max(1, int(round_num) + 1)
    if current_round == target_round and state.get('status') == 'pending':
        state['status'] = 'served'
        state['servedRound'] = current_round
        _append_event(task, {
            'kind': 'served',
            'round': current_round,
            'reason': 'single_request_gateway_trial',
        })
        return (
            PROGRAMMATIC_WIRE_EXPOSURE_GATEWAY_ONLY,
            'serial_chain_one_shot',
        )

    # Real Kimi trajectories proved that a sticky gateway-only epoch can be
    # bypassed through command/skill tools and become substantially more
    # expensive. Restore the complete direct surface after exactly one request
    # whether the model adopted execute_tools, acted substantively, or stopped.
    if current_round > target_round or state.get('status') == 'served':
        _append_event(task, {
            'kind': 'reset',
            'reason': 'gateway_trial_consumed',
            'beforeRound': current_round,
        })
        task.pop(_STATE_KEY, None)
        return PROGRAMMATIC_EXPOSURE_ADDITIVE, 'gateway_trial_consumed'
    return PROGRAMMATIC_EXPOSURE_ADDITIVE, 'awaiting_target_round'


__all__ = [
    'PROGRAMMATIC_EXPOSURE_ADDITIVE',
    'PROGRAMMATIC_EXPOSURE_SERIAL_GATEWAY',
    'PROGRAMMATIC_WIRE_EXPOSURE_GATEWAY_ONLY',
    'activate_serial_gateway',
    'resolve_programmatic_exposure',
]
