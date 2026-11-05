"""Name the Turn range an L2 summary folds and the cursor that recalls it.

Entry point: ``summary_coverage``, called by ``_layer2._compact`` while it builds
the handoff carrier. The working view keeps ``_turnId`` on rows rebuilt from
Turns; rows produced live by a task carry none and belong to the reply Turn of
the nearest preceding identified input. The recall cursor is the 1-based
message number accepted by ``get_conversation(before=N)``: N ends the window at
the last covered Turn. Main-lane Turn ordinals are positions unless a Turn was
deleted, so N is exact or an upper bound and never hides covered history.
Dependencies: one owner-scoped ``turn_lifecycle.get_turn`` read; a failure only
omits the cursor.
"""
from __future__ import annotations

from lib.log import get_logger
from lib.tasks_pkg.compaction._handoff import HANDOFF_FIELD, is_compaction_handoff

logger = get_logger(__name__)


def _identified(message):
    value = message.get('_turnId') if isinstance(message, dict) else None
    return value if isinstance(value, str) and value else ''


def _live_execution(message):
    """A row the task produced itself: reply execution, not context or history."""
    if not isinstance(message, dict) or _identified(message):
        return False
    if message.get('_contextComposer') or is_compaction_handoff(message):
        return False
    return message.get('role') in ('assistant', 'tool') or bool(message.get('_isInboxInject'))


def summary_coverage(task, messages, folded):
    """Return ``{conversationId, firstTurnId, lastTurnId, throughReply, before?}`` or None."""
    if not isinstance(task, dict) or not task.get('convId') or not task.get('_turnId'):
        return None
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(task) or task.get('_transientRuntime'):
        return None
    positions = {id(message): index for index, message in enumerate(messages)}
    folded_positions = [positions[id(message)] for message in folded if id(message) in positions]
    if not folded_positions:
        return None
    first = ''
    for message in folded:
        prior = (message.get(HANDOFF_FIELD) or {}).get('coverage') \
            if is_compaction_handoff(message) else None
        first = (prior.get('firstTurnId') if isinstance(prior, dict) else '') or _identified(message)
        if first:
            break
    end = max(folded_positions)
    anchor = next((index for index in range(end, -1, -1) if _identified(messages[index])), None)
    if not first or anchor is None:
        return None
    last = _identified(messages[anchor])
    through_reply = any(_live_execution(message) for message in messages[anchor + 1:end + 1])
    coverage = {'conversationId': task['convId'], 'firstTurnId': first,
                'lastTurnId': last, 'throughReply': through_reply}
    try:
        from lib.tasks_pkg.manager import task_user_id
        from lib.turn_lifecycle import get_turn
        row = get_turn(task['convId'], last, user_id=task_user_id(task))
        ordinal = row.get('ordinal')
        if (row.get('laneId') or 'main') == 'main' and type(ordinal) is int and ordinal >= 0:
            # An input Turn's reply is the next ordinal of the same pair.
            reply = 1 if through_reply and messages[anchor].get('role') == 'user' else 0
            coverage['before'] = ordinal + reply + 2
    except Exception as exc:
        logger.debug('[Compact] coverage cursor unavailable: %s', type(exc).__name__)
    return coverage
