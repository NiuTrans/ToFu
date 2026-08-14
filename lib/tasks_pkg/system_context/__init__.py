"""Compatibility surface for conversational context.

``lib.tasks_pkg.context_composer`` is the only active assembly path. This
package keeps older imports working while routing ``_inject_system_contexts``
to that Composer. The reminder/profile helpers below are retained as narrow
utilities for display, migration, and tests; they are not independent ambient
context producers.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Reminders / system-message primitives ─────────────────────────────────
from lib.tasks_pkg.system_context._reminders import (  # noqa: E402,F401
    _TIMESTAMP_PREFIX,
    _strip_old_timestamp,
    _wrap_system_reminder,
    _append_to_system_message,
    _system_text,
)

# ── Profile / user-context placement ───────────────────────────────────────
from lib.tasks_pkg.system_context._profile import (  # noqa: E402,F401
    _PROFILE_MARKER,
    _PROFILE_DETAIL_MARKER,
    _insert_user_context_message,
    _append_user_profile_block,
    _refresh_detail_block,
)

# ── Search addendum (legacy no-op) ─────────────────────────────────────────
from lib.tasks_pkg.system_context._search import (  # noqa: E402,F401
    inject_search_addendum_to_user,
)

# ── Injection orchestrator ─────────────────────────────────────────────────
from lib.tasks_pkg.system_context._inject import (  # noqa: E402,F401
    _CC_STATIC_MARKER,
    _disabled_prompt_blocks,
    _inject_system_contexts,
    _extract_last_user_text,
)

__all__ = [
    # constants
    '_TIMESTAMP_PREFIX',
    '_PROFILE_MARKER',
    '_PROFILE_DETAIL_MARKER',
    '_CC_STATIC_MARKER',
    # reminders / system-message primitives
    '_strip_old_timestamp',
    '_wrap_system_reminder',
    '_append_to_system_message',
    '_system_text',
    # profile / user-context placement
    '_insert_user_context_message',
    '_append_user_profile_block',
    '_refresh_detail_block',
    # search addendum
    'inject_search_addendum_to_user',
    # injection orchestrator
    '_disabled_prompt_blocks',
    '_inject_system_contexts',
    '_extract_last_user_text',
]
