"""Search-addendum injection (legacy no-op) + user-message timestamp cleanup.

Extracted from ``lib.tasks_pkg.system_context`` (facade-preserving split).
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.tasks_pkg.system_context._reminders import (
    _TIMESTAMP_PREFIX,
    _strip_old_timestamp,
)


def inject_search_addendum_to_user(messages: list, search_enabled: bool,
                                    round_num: int = 0):
    """Legacy no-op — timestamp moved to system prompt as date-only.

    Previously injected "Current date and time: ..." into the last user
    message.  A/B testing showed this killed cache (Arm A: 77.9% cache,
    $0.49 vs Arm C date-only in system: 85.7%, $0.36).

    The date is now injected in _inject_system_contexts() step 4.5 as
    date-only format (changes once per UTC day → cache-stable).

    This function is kept for backward compatibility but does nothing.
    It still strips old timestamps from user messages to clean up
    conversations that had them injected previously.

    Args:
        messages: The messages list (may be cleaned in-place).
        search_enabled: Ignored (was: whether search/tools are enabled).
        round_num: Ignored (was: current round within the task).
    """
    # Strip old timestamps from user messages for clean cache prefix
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get('role') == 'user':
            content = messages[i].get('content', '')
            if isinstance(content, str) and _TIMESTAMP_PREFIX in content:
                messages[i]['content'] = _strip_old_timestamp(content)
            elif isinstance(content, list):
                _new = [b for b in content
                        if not (isinstance(b, dict) and b.get('type') == 'text'
                                and b.get('text', '').strip().startswith(_TIMESTAMP_PREFIX))]
                if len(_new) != len(content):
                    messages[i]['content'] = _new
            break  # only check the last user message
