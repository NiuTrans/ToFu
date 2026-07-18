"""lib.agent_core — The reusable agent base, as one browsable package.

What this is
------------
``lib/agent_core/`` is the home + browsable surface of Tofu's reusable
foundation: the run loop, model dispatch, swarm scheduling, the
Planner→Worker→Critic endpoint loop, context compaction, the push hub, the
background-task runtime, and capability profiles — together with the two
registry seams through which the base reaches plugins.

Two kinds of member
-------------------
1. **Relocated leaves** — self-contained base modules that physically live
   here now: ``push.py``, ``task_runtime.py``, ``profiles.py``.  The old
   ``lib.push`` / ``lib.task_runtime`` paths still work via thin compatibility
   shims that re-export from here; the ``lib.agent_profiles`` shim was removed
   (2026-06) once it had no remaining importer — use ``lib.agent_core.profiles``
   (or this facade) instead.
2. **Named-in-place members** — the rest of the base is a *cross-cutting
   subset* of code inside ``tasks_pkg`` / ``llm_dispatch`` / ``swarm`` that
   would create back-imports (and ~960 import-site rewrites) if force-moved.
   Those stay where they are; this facade *names* them via re-export.

Lazy by design
--------------
Imports are resolved lazily through :pep:`562` ``__getattr__`` against
``CORE_MEMBERS``.  This matters: importing a relocated leaf submodule —
``from lib.agent_core.push import hub`` — must NOT drag in the heavy
``orchestrator → manager → ...`` chain.  With lazy resolution, ``import
lib.agent_core`` is cheap, and ``lib.agent_core.run_task`` only pulls the
orchestrator when first accessed.

Membership / boundary
---------------------
``CORE_MEMBERS`` maps each public symbol → its defining module.  It is kept
consistent with :data:`lib.agent_core_manifest.CORE_MODULES` and verified by
``tests/test_agent_core_boundary.py`` (which also enforces that no core module
imports a concrete plugin).

Usage::

    from lib.agent_core import run_task, dispatch_chat, apply_profile
    from lib.agent_core.push import hub          # cheap — no orchestrator pulled
"""

from __future__ import annotations

import importlib
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# ── Introspectable membership map (symbol → defining module) ──
# Kept in lockstep with lib.agent_core_manifest.CORE_MODULES by the boundary
# test.  Relocated leaves point at their new in-package homes; named-in-place
# members point at their existing modules.
CORE_MEMBERS: dict[str, str] = {
    # Orchestration: run loop + endpoint (Planner→Worker→Critic) + compaction
    'run_task':                'lib.tasks_pkg.orchestrator',
    '_assemble_tool_list':     'lib.tasks_pkg.model_config',
    'run_compaction_pipeline': 'lib.tasks_pkg.compaction',
    # Persistence + activity-feed seams (host-overridable accessors)
    'get_conversation_store':  'lib.agent_core.store',
    'set_conversation_store':  'lib.agent_core.store',
    'emit_activity_event':     'lib.agent_core.activity',
    'set_activity_sink':       'lib.agent_core.activity',
    # Model communication + routing / load balancing
    'build_body':              'lib.llm',
    'chat':                    'lib.llm',
    'stream_chat':             'lib.llm',
    'dispatch_chat':           'lib.llm_dispatch',
    'dispatch_stream':         'lib.llm_dispatch',
    'get_dispatcher':          'lib.llm_dispatch',
    'reset_dispatcher':        'lib.llm_dispatch',
    # Relocated leaves — now physically in this package
    'TaskRuntime':             'lib.agent_core.task_runtime',
    'hub':                     'lib.agent_core.push',
    'push_event':              'lib.agent_core.push',
    'broadcast':               'lib.agent_core.push',
    'apply_profile':           'lib.agent_core.profiles',
    'get_profile':             'lib.agent_core.profiles',
    'list_profiles':           'lib.agent_core.profiles',
    'resolve_profile_name':    'lib.agent_core.profiles',
    # Streaming-event contract (the frontend↔backend sync interface)
    'EventType':               'lib.agent_core.events',
    'EventSpec':               'lib.agent_core.events',
    'EventCategory':           'lib.agent_core.events',
    'event_types':             'lib.agent_core.events',
    'get_event_spec':          'lib.agent_core.events',
    'all_event_specs':         'lib.agent_core.events',
    'EVENT_CONTRACT_VERSION':  'lib.agent_core.events',
    # Registry seams (the only bridge core uses to reach plugins)
    'ToolSpec':                'lib.tools.registry',
    'ToolContext':             'lib.tools.registry',
    'assemble_tool_list':      'lib.tools.registry',
    'register_tool_spec':      'lib.tools.registry',
    'BodyDialect':             'lib.llm_dispatch.provider_registry',
    'register_dialect':        'lib.llm_dispatch.provider_registry',
}

__all__ = list(CORE_MEMBERS.keys())


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolution against CORE_MEMBERS.

    Resolves a facade symbol to the real object only on first access, so
    ``import lib.agent_core`` (and importing relocated leaf submodules) stays
    cheap and avoids eagerly pulling the orchestrator dependency chain.
    """
    module_path = CORE_MEMBERS.get(name)
    if module_path is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    try:
        module = importlib.import_module(module_path)
        value = getattr(module, name)
    except Exception as e:
        logger.error('[agent_core] failed to resolve %s from %s: %s',
                     name, module_path, e, exc_info=True)
        raise
    globals()[name] = value  # cache so subsequent access skips __getattr__
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(CORE_MEMBERS))
