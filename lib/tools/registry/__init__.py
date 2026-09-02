"""lib/tools/registry — Declarative tool-assembly registry (facade package).

This package is the **single seam** through which both built-in and
third-party tools declare *what schema the LLM sees* and *when that tool
is active*.  It exists to collapse the hand-maintained ``if feature: …``
ladder that used to live in ``lib.tasks_pkg.model_config._assemble_tool_list``
into a list of self-describing :class:`ToolSpec` objects.

Why this matters
----------------
Before this module, adding a native tool meant editing a core
orchestration file (``model_config.py``) — a hardcoded if-branch per
feature.  Now a tool author registers a :class:`ToolSpec` (schema + gate)
once, in their own file, and the orchestrator picks it up with **zero**
core edits.  Third-party packages can contribute tools via the
``tofu.tools`` entry-point group (see :func:`discover_plugin_specs`).

Design contract (DO NOT BREAK)
------------------------------
1. **Ordering is prompt-cache-critical.**  Specs are emitted in
   registration order within their phase.  The built-in registration
   order reproduces the A/B-validated layout exactly:
   search → fetch → read_files → project|code_exec → browser → desktop →
   image_gen → conv_ref → human_guidance → ⟨base/capability boundary⟩ →
   memory → scheduler → swarm → mcp.
2. **Two phases.**  ``phase='base'`` specs are emitted first and counted
   toward ``has_base_tools`` (the value the orchestrator calls
   ``has_real_tools``).  ``phase='capability'`` specs are emitted after,
   and may read :attr:`ToolContext.has_base_tools` to self-gate.
3. **Lazy imports.**  A spec's ``build(ctx)`` is called at request time, so
   heavy schema imports (browser, swarm, mcp, …) stay out of startup.
4. **Side-effect gates are allowed.**  ``build()`` may log (e.g.
   "browser requested but extension not connected") and may return an
   empty list — exactly mirroring the legacy behaviour.

Plugin isolation (multi-tenant)
-------------------------------
``discover_plugin_specs()`` loads third-party ``tofu.tools`` entry points into
the SAME process-global ``_TOOL_SPECS`` list as the built-ins.  On a shared,
multi-tenant server (e.g. the headless ``/api/v1/agent/run`` API) that means a
plugin installed for one caller would otherwise be visible to EVERY caller —
its tool schema (and any imperative "always call me first" text in that schema)
silently pollutes unrelated requests.

To prevent this, every spec carries a :attr:`ToolSpec.source`
(``'builtin'`` | ``'plugin'``) tag and plugins additionally carry a
:attr:`ToolSpec.plugin_name`.  :func:`assemble_tool_list` consults the
per-request :attr:`ToolContext.enabled_plugins` allow-list:

* built-in specs are ALWAYS evaluated;
* a plugin spec is evaluated only when its ``plugin_name`` is allow-listed.

The allow-list is resolved per request by :func:`resolve_enabled_plugins` from
``cfg['plugins']`` (request-scoped) falling back to the
``TOFU_DEFAULT_TOOL_PLUGINS`` env var (deployment-wide default).  The default
when neither is set is **fail-closed**: NO third-party plugins are visible.  A
dedicated single-tenant deployment that wants the old "everything I installed
is on" behaviour sets ``TOFU_DEFAULT_TOOL_PLUGINS=*`` (or passes
``cfg['plugins']='*'``), which maps to ``enabled_plugins=None`` (gate fully
open).  This isolation is a VISIBILITY boundary (the LLM never sees the schema),
not a security sandbox — a plugin's handler code still lives in-process.

Package layout (FACADE)
-----------------------
This facade is split into cohesive sub-modules; ``__init__`` re-exports every
public compatibility symbol so
``from lib.tools.registry import X`` (and ``registry.X`` attribute access)
keeps working byte-identically. The import path is unchanged.

  * :mod:`._spec`    — ``ToolContext`` / ``ToolSpec``, the single-home
    ``_TOOL_SPECS`` / ``_REGISTERED_KEYS`` registry, ``register_tool_spec`` /
    ``all_specs`` / handler sync, and ``assemble_tool_list``.
  * :mod:`._build`   — the 15 built-in ``_build_*`` builders + ``_register_builtins``.
  * :mod:`._plugins` — entry-point discovery + allow-list resolution.

**Import-time side-effect (single-home + populate-on-import):** at the bottom
of this module ``_register_builtins()`` then ``discover_plugin_specs()`` run
exactly once — the same behaviour the monolith had — so ``all_specs()`` /
``assemble_tool_list`` see a populated ``_TOOL_SPECS`` the moment
``lib.tools.registry`` is imported.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


# ── ToolContext / ToolSpec + the single-home spec registry ────────────
from lib.tools.registry._spec import (  # noqa: E402,F401
    ToolContext,
    ToolHandlerFn,
    ToolResultMetaBuilder,
    ToolSpec,
    _REGISTERED_KEYS,
    _TOOL_SPECS,
    _sync_one,
    all_specs,
    assemble_tool_list,
    build_tool_result_meta,
    register_tool_spec,
    sync_spec_handlers,
)


# ── Built-in spec builders + registration ─────────────────────────────
from lib.tools.registry._build import (  # noqa: E402,F401
    _build_browser,
    _build_conv_ref,
    _build_custom,
    _build_desktop,
    _build_fetch,
    _build_human_guidance,
    _build_image_gen,
    _build_inspect_image,
    _build_knowledge,
    _build_mcp,
    _build_memory,
    _build_project_or_code_exec,
    _build_read_files,
    _build_scheduler,
    _build_search,
    _build_swarm,
    _build_todo,
    canonical_human_guidance_schema,
    _register_builtins,
)


# ── Third-party plugin discovery + allow-list resolution ──────────────
from lib.tools.registry._plugins import (  # noqa: E402,F401
    _DEFAULT_PLUGINS_ENV,
    _parse_plugin_spec,
    available_plugins,
    discover_plugin_specs,
    resolve_enabled_plugins,
)


# ── ``_dispatch_registry`` is late-bound by the executor via
#    sync_spec_handlers(); expose the *current* value as a package attribute
#    for introspection. (The authoritative binding lives on _spec; reads
#    through the facade should use sync_spec_handlers / the _spec module.)
from lib.tools.registry import _spec  # noqa: E402,F401


__all__ = [
    # dataclasses
    'ToolContext', 'ToolSpec', 'ToolHandlerFn', 'ToolResultMetaBuilder',
    # registry core
    'register_tool_spec', 'all_specs', 'sync_spec_handlers',
    'assemble_tool_list', 'build_tool_result_meta',
    'canonical_human_guidance_schema',
    # plugin discovery + allow-list
    'discover_plugin_specs', 'available_plugins', 'resolve_enabled_plugins',
]


# ══════════════════════════════════════════════════════════
#  Import-time side-effects — register built-ins + discover plugins.
#  MUST fire on import so all_specs()/assemble_tool_list see a populated
#  _TOOL_SPECS (identical to the pre-split monolith).
# ══════════════════════════════════════════════════════════
_register_builtins()
discover_plugin_specs()
