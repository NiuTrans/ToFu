"""lib/agent_core_manifest.py — The core/plugin boundary, declared.

This module is **documentation that executes**.  It names which parts of
``lib/`` constitute the reusable *agent base* (orchestration, model dispatch,
swarm scheduling, context compaction, push, task runtime) versus which parts
are *plugins* (concrete tools, concrete provider dialects).

Manifest first, directory second
--------------------------------
The base is what makes Tofu reusable across projects: the run loop, the
Planner→Worker→Critic endpoint mode, swarm scheduling, compaction, the push
hub.  Plugins are the swappable bits: individual tools and provider body
dialects.  The *guarantee* that the base never reaches back into a concrete
plugin is what keeps it a clean foundation — and that guarantee is enforced by
the AST test, not by the folder layout.

The directory IS being migrated to mirror this manifest, in stages:
* **Stage 1 (done, 2026-06):** self-contained leaves with no core-sibling
  back-imports — ``push.py``, ``task_runtime.py``, ``profiles.py`` — physically
  moved into ``lib/agent_core/``.  Thin shims at the old paths
  (``lib.push``, ``lib.task_runtime``, ``lib.agent_profiles``) re-export from
  the new homes, so existing call sites are unaffected.
* **Later stages:** the cross-cutting members (orchestrator, model_config,
  endpoint, …) stay named-in-place for now — moving them naively would create
  ``agent_core → tasks_pkg`` back-imports and rewrite ~960 import sites.  They
  migrate only when their sibling coupling is untangled.

Either way a folder can't stop an import — what enforces the boundary is the
AST test ``tests/test_agent_core_boundary.py``, which reads THIS manifest and
asserts:

    No CORE_MODULES file imports a CONCRETE_PLUGIN_MODULE.

Core may only reach plugins through the *registry seams* — ``lib.tools.registry``
(ToolSpec) and ``lib.llm_dispatch.provider_registry`` (BodyDialect) — which are
themselves part of the base.  Add a tool/provider via those seams and the base
stays untouched.

How to evolve the boundary
--------------------------
* New base capability (a new orchestration concern) → add its module prefix to
  ``CORE_MODULES``.
* New concrete tool family that core must NOT import directly → add its module
  to ``CONCRETE_PLUGIN_MODULES``.
* Need core to use a plugin → DON'T.  Route it through a registry seam instead.
  That's the whole point.
"""

from __future__ import annotations

# ── The reusable agent base (module path prefixes, relative to repo root) ──
# A file is "core" if its dotted module path starts with any of these.
CORE_MODULES: tuple[str, ...] = (
    # Task orchestration + the run loop, endpoint mode, compaction, dispatch.
    'lib.tasks_pkg.orchestrator',
    'lib.tasks_pkg.model_config',
    'lib.tasks_pkg.endpoint',
    'lib.tasks_pkg.compaction',
    'lib.tasks_pkg.tool_dispatch',
    'lib.tasks_pkg.executor',
    # Model communication + routing / load balancing.
    'lib.llm',
    'lib.llm_dispatch.dispatcher',
    'lib.llm_dispatch.api',
    'lib.llm_dispatch.slot',
    'lib.llm_dispatch.factory',
    # Swarm scheduling (the orchestration engine, not the tool schemas).
    'lib.swarm.scheduler',
    'lib.swarm.master',
    'lib.swarm.agent',
    # Cross-cutting base infrastructure.
    'lib.task_runtime',
    'lib.push',
    'lib.agent_profiles',
    # The browsable facade package that mirrors this manifest (see
    # lib/agent_core/__init__.py).  It is itself part of the base and must
    # obey the no-concrete-plugin rule — it imports only core modules + the
    # registry seams.
    'lib.agent_core',
)

# ── The registry seams core IS allowed to import ──
# These are part of the base; they are how core reaches plugins indirectly.
REGISTRY_SEAMS: tuple[str, ...] = (
    'lib.tools.registry',
    'lib.llm_dispatch.provider_registry',
)

# ── Concrete plugins core must NOT import directly ──
# Importing any of these from a CORE module means the base has grown a hard
# dependency on a swappable plugin — exactly what the boundary forbids.
CONCRETE_PLUGIN_MODULES: tuple[str, ...] = (
    'lib.tools.search',
    'lib.tools.browser',
    'lib.tools.project',
    'lib.tools.code_exec',
    'lib.tools.conversation',
    'lib.tools.human_guidance',
    'lib.tools.image_gen',
    'lib.tools.meta',
)


# Package facades whose public symbols are all defined in CORE_MODULES
# submodules.  The facade re-exports them, so importing the bare package is a
# core import even though the package dir itself isn't a single CORE_MODULES leaf.
_CORE_PACKAGE_FACADES: frozenset[str] = frozenset({
    'lib.llm',
    'lib.llm_dispatch',
})


def is_core_module(dotted: str) -> bool:
    """True if *dotted* belongs to the agent base.

    The registry seams (:data:`REGISTRY_SEAMS`) count as core — they are the
    base's own bridge to plugins.  The ``lib.llm_dispatch`` / ``lib.llm``
    package facades count as core because their public symbols are defined in
    the core submodules listed in :data:`CORE_MODULES`.
    """
    if any(dotted == s or dotted.startswith(s + '.') for s in REGISTRY_SEAMS):
        return True
    if dotted in _CORE_PACKAGE_FACADES:
        return True
    return any(dotted == p or dotted.startswith(p + '.') for p in CORE_MODULES)


def is_concrete_plugin_import(dotted: str) -> bool:
    """True if *dotted* names a concrete plugin module (not a registry seam)."""
    if any(dotted == s or dotted.startswith(s + '.') for s in REGISTRY_SEAMS):
        return False
    return any(dotted == p or dotted.startswith(p + '.')
               for p in CONCRETE_PLUGIN_MODULES)


__all__ = [
    'CORE_MODULES',
    'REGISTRY_SEAMS',
    'CONCRETE_PLUGIN_MODULES',
    'is_core_module',
    'is_concrete_plugin_import',
]
