"""lib/memory/user_profile — the rolling, bounded personal-preference profile.

This is a THIRD memory placement, distinct from the two that already exist:

  1. System-prefix injection (BP1–3, always-on) — cache-poison for anything
     that changes; the memory-count hint was ripped out for exactly this
     reason (see ``.tofu/memories/memory-count-hint-mutates-cached-system-prefix.md``).
  2. Per-turn BM25 prefetch (``<relevant_memories>`` in the tail) — cache-safe,
     but its cheap-LLM reranker is *designed to drop* anything without a
     concrete task step, so a standing preference ("always answer in Chinese")
     never survives.

A personal preference needs to be BOTH always-on AND cache-stable. The trick
(validated by Hermes Agent + our own CLAUDE.md placement): put it in the
prepended ``_isMeta`` user message — the BP4 5-min-TTL tail segment — NOT the
system prefix. When the profile changes, only the cheap tail re-writes once;
the expensive system+tools prefix stays cached. The injection helper lives in
``lib/tasks_pkg/system_context.py`` and calls ``notify_compaction`` so the
cache-tracker doesn't false-positive the mutation.

Design choices (locked by the user):
  * Hard-capped (~800 tokens ≈ 2.5 KB).
  * NOT part of the BM25 corpus — it is never "searched", it is always present.
  * SCOPED by identity. ``scope=''`` (open / private mode — one operator, no
    tenant binding) → the single global file ``<data>/memories/.tofu_user_profile.md``
    (BYTE-IDENTICAL to before, no migration). A multi-user tenant ``user_id``
    → a per-tenant file ``<data>/memories/profiles/<scope>/.tofu_user_profile.md``
    so one tenant's profile is never injected into another's prompt. Scope is
    resolved from the request's ``AuthContext`` via ``resolve_profile_scope``
    and captured onto the task at creation (the consolidation daemon has no
    request context). The ``.tofu`` prefix (per the artifact registry — see
    ``lib/agent_artifacts.py``) is preserved on the filename either way.
  * Bullet-list markdown under headers (Hermes/OpenClaw ``USER.md`` shape).
  * The hard cap is the forcing function for refinement: the consolidation
    pass (layer 3) must ``replace`` in place rather than append.

This package is storage + rendering ONLY. The propose-confirm capture loop
(layer 3) builds on ``load_profile`` / ``save_profile`` / ``profile_over_cap``.

No code lives in this file — it is a pure re-export facade. All implementations
live in the sub-modules (``_paths`` / ``_io`` / ``_render`` / ``_mutate`` /
``_pending``); importing them here keeps every historical
``from lib.memory.user_profile import X`` AND
``from lib.memory import user_profile as up`` working byte-identically.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Scope + path resolution (._paths) ──
from lib.memory.user_profile._paths import (  # noqa: E402,F401
    _pending_path,
    _sanitize_scope,
    _server_memories_dir,
    profile_path,
    resolve_profile_scope,
)

# ── Body persistence + markers + structured items (._io) ──
from lib.memory.user_profile._io import (  # noqa: E402,F401
    _CORE_HEADERS,
    _PROFILE_DETAIL_MARKER,
    _PROFILE_MARKER,
    USER_PROFILE_CHAR_CAP,
    load_profile,
    parse_items,
    profile_char_count,
    profile_over_cap,
    save_items,
    save_profile,
    serialize_items,
)

# ── Rendering + tiering (._render) ──
from lib.memory.user_profile._render import (  # noqa: E402,F401
    _select_detail_items,
    applied_profile_items,
    profile_summary_for_event,
    render_profile_block,
    render_profile_tiers,
    split_profile_tiers,
)

# ── Consolidation write primitives (._mutate) ──
from lib.memory.user_profile._mutate import (  # noqa: E402,F401
    _DEFAULT_HEADER,
    apply_new_preference,
    apply_reinforcement,
)

# ── Propose-then-confirm gate (._pending) ──
from lib.memory.user_profile._pending import (  # noqa: E402,F401
    load_pending,
    resolve_pending,
    stage_pending,
)

__all__ = [
    'USER_PROFILE_CHAR_CAP',
    'resolve_profile_scope',
    'profile_path',
    'load_profile',
    'save_profile',
    'profile_char_count',
    'profile_over_cap',
    'render_profile_block',
    'split_profile_tiers',
    'render_profile_tiers',
    'applied_profile_items',
    'profile_summary_for_event',
    'apply_reinforcement',
    'apply_new_preference',
    'parse_items',
    'serialize_items',
    'save_items',
    'load_pending',
    'stage_pending',
    'resolve_pending',
]
