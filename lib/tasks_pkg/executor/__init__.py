# HOT_PATH
"""Tool execution — unified dispatch for all tool types + tool summary generation.

──────────────────────────────────────────────────────────────────────────
This module is a **facade-preserving package** (split from the original
~729-line ``executor.py``). Every public and private symbol is re-exported
here so all existing ``from lib.tasks_pkg.executor import X`` call sites keep
working byte-identically — the import path is UNCHANGED.

Implementations live in:

  * ``._registry``    — ``ToolRegistry`` (kept WHOLE) + the ``tool_registry``
    module-level singleton
  * ``._finalize``    — ``_finalize_tool_round`` / ``_build_simple_meta``
    (MONKEYPATCH TARGETS: patched via the handlers.misc facade + tests)
  * ``._content_ref`` — ``_resolve_content_ref``
  * ``._summary``     — ``_generate_tool_summary`` / ``_prefetch_user_urls``
  * ``._execute``     — ``_execute_tool_one`` (the single-tool dispatch entry)

MODULE-LEVEL SIDE EFFECTS (preserved from the original, at the END of this
file, AFTER all symbols are bound so handler registration can import them):
  1. ``import lib.tasks_pkg.handlers`` — triggers @tool_registry registration.
  2. ``sync_spec_handlers(tool_registry)`` — binds ToolSpec-attached handlers.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# NOTE: Do NOT re-export _lib.FETCH_* as module-level copies here.
# Module-level copies become stale after reload_config() — always read
# from _lib.<VAR> at call time to pick up hot-reloaded values.

# ── Re-export for tool_dispatch / tool_display (kept as a facade symbol) ──
from lib.swarm.tools import SWARM_TOOL_NAMES  # noqa: E402,F401

# ── ToolRegistry class + module-level singleton (._registry) ──────────
from lib.tasks_pkg.executor._registry import (  # noqa: E402,F401
    ToolRegistry,
    tool_registry,
)

# ── Shared finalization / meta helpers (._finalize) — MONKEYPATCH TARGETS ──
from lib.tasks_pkg.executor._finalize import (  # noqa: E402,F401
    _build_simple_meta,
    _finalize_tool_round,
)

# ── Content-ref resolver (._content_ref) ──────────────────────────────
from lib.tasks_pkg.executor._content_ref import (  # noqa: E402,F401
    _resolve_content_ref,
)

# ── Tool summary + user-URL prefetch (._summary) ──────────────────────
from lib.tasks_pkg.executor._summary import (  # noqa: E402,F401
    _generate_tool_summary,
    _prefetch_user_urls,
)

# ══════════════════════════════════════════════════════════
#  Tool handlers — extracted to lib/tasks_pkg/handlers/
#  Importing the handlers package triggers @tool_registry registration.
#  This MUST come after tool_registry / _finalize_tool_round / _build_simple_meta
#  are bound above, since the handler modules import them from this facade.
# ══════════════════════════════════════════════════════════
import lib.tasks_pkg.handlers  # noqa: F401, E402 — triggers all handler registrations

# ── Sync handlers attached to ToolSpec plugins into the dispatch registry ──
# Built-in handlers register above via @tool_registry decorators.  Third-party
# tools loaded through the ``tofu.tools`` entry point can ship schema + gate +
# handler from ONE external package by attaching ``handler=`` to their
# ToolSpec; this call binds those handlers into tool_registry.  Late-loaded
# plugins (registered after startup) self-sync via register_tool_spec.
try:
    from lib.tools.registry import sync_spec_handlers as _sync_spec_handlers
    _n_synced = _sync_spec_handlers(tool_registry)
    if _n_synced:
        logger.info('[Executor] synced %d ToolSpec-attached handler(s) into '
                    'tool_registry', _n_synced)
except Exception as _sync_err:
    logger.error('[Executor] ToolSpec handler sync failed: %s', _sync_err,
                 exc_info=True)

# ── Single-tool dispatch entry (._execute) ────────────────────────────
# Imported LAST so the handler registrations + spec-sync above are complete
# before this dispatch entry point becomes reachable.
from lib.tasks_pkg.executor._execute import (  # noqa: E402,F401
    _execute_tool_one,
)

__all__ = [
    # registry
    'ToolRegistry',
    'tool_registry',
    # finalization / meta helpers (monkeypatch targets)
    '_finalize_tool_round',
    '_build_simple_meta',
    # content-ref resolver
    '_resolve_content_ref',
    # summary + prefetch
    '_generate_tool_summary',
    '_prefetch_user_urls',
    # single-tool dispatch entry
    '_execute_tool_one',
    # re-exported for tool_dispatch / tool_display
    'SWARM_TOOL_NAMES',
]
