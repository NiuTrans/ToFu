"""System context injection — append/prepend helpers and context layering.

Extracted from orchestrator.py to isolate the system-message manipulation
logic (project context, memory, swarm prompt, search addendum).

Includes delta attachment tracking (inspired by Claude Code): context strings
are hashed, and when the content is unchanged between successive tasks in the
same conversation, we **skip the expensive load** (FUSE I/O) but still inject
the text.  This is necessary because each task receives a *fresh* message list
from the frontend — the system message does NOT carry over project/memory
context from the previous task.

**Claude-Code-style layout** (the only layout — no kill switch).

Static prompt sections (``# System``, ``# Doing tasks``, ``# Executing actions
with care``, ``# Using your tools``, ``# Tone and style``, ``# Output
efficiency``, ``# Environment``, etc.) are assembled by
``lib.tasks_pkg.system_prompt_cc.build_static_prompt`` as ONE cache-stable
block in the system message.  CLAUDE.md / project-intelligence content is
**NOT** placed in the system message — it goes into a prepended user message
with ``_isMeta: True`` wrapped in ``<system-reminder>`` tags (mirroring
Claude Code's ``prependUserContext`` in ``utils/api.ts:449``).  A/B-validated
to save 18% cost / +49% cache hit — see
``.tofu/skills/claudemd-placement-ab-test-results.md``.

──────────────────────────────────────────────────────────────────────────
This module is a **facade-preserving package** (split from the original
~1088-line ``system_context.py``). Every public and private symbol is
re-exported here so all existing ``from lib.tasks_pkg.system_context import X``
call sites keep working byte-identically. Implementations live in:

  * ``._reminders`` — ``_strip_old_timestamp`` / ``_wrap_system_reminder`` /
    ``_append_to_system_message`` / ``_system_text`` + ``_TIMESTAMP_PREFIX``
  * ``._profile``   — ``_insert_user_context_message`` /
    ``_append_user_profile_block`` / ``_refresh_detail_block`` +
    ``_PROFILE_MARKER`` / ``_PROFILE_DETAIL_MARKER``
  * ``._search``    — ``inject_search_addendum_to_user``
  * ``._inject``    — ``_disabled_prompt_blocks`` / ``_inject_system_contexts``
    / ``_extract_last_user_text`` + ``_CC_STATIC_MARKER``
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
