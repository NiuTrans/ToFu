"""lib/tools/registry.py — Declarative tool-assembly registry.

This module is the **single seam** through which both built-in and
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
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# A dispatch handler — same signature as lib.protocols.ToolHandler.  Typed
# loosely here to avoid importing the protocol into this low-level module.
ToolHandlerFn = Callable[..., tuple[str, str, bool]]


# ══════════════════════════════════════════════════════════
#  ToolContext — the inputs every gate/build decision needs
# ══════════════════════════════════════════════════════════

@dataclass
class ToolContext:
    """Everything a :class:`ToolSpec` needs to decide whether to contribute.

    Built once per task by :func:`assemble_tool_list` from the resolved
    model config.  Two fields are mutated by the assembler *between*
    phases so capability-phase specs can self-gate:

    - :attr:`current_count` — number of tools accumulated so far (lets a
      base-phase spec like conv-ref require that *some* tool already exists).
    - :attr:`has_base_tools` — set ``True`` once the base phase produced ≥1
      tool; read by capability-phase specs (memory, scheduler).
    """

    cfg: dict[str, Any]
    task_id: str
    project_path: str
    project_enabled: bool
    search_mode: str
    search_enabled: bool
    fetch_enabled: bool
    code_exec_enabled: bool
    browser_enabled: bool
    desktop_enabled: bool
    swarm_enabled: bool
    image_gen_enabled: bool = False
    human_guidance_enabled: bool = False
    scheduler_enabled: bool = False
    messages: list[dict[str, Any]] | None = None

    # ── Mutated by the assembler between phases ──
    current_count: int = 0
    has_base_tools: bool = False

    @property
    def tid(self) -> str:
        """Short task-id prefix for log lines."""
        return (self.task_id or '')[:8]

    @property
    def multiroot_active(self) -> bool:
        """True when more than one workspace root is configured for this task.

        Read from ``cfg['projectPaths']`` (the full root list the frontend
        sends; element 0 is the primary, the rest are extras). Used to decide
        whether path-taking tool schemas should carry the ``rootname:`` prefix
        hint — single-root sessions keep the cache-stable default schema.
        """
        paths = self.cfg.get('projectPaths') or []
        if not isinstance(paths, (list, tuple)):
            return False
        distinct = {p for p in paths if p}
        return len(distinct) > 1

    @property
    def has_conv_ref(self) -> bool:
        """True if any message carries a ``[REFERENCED_CONVERSATION`` marker.

        Mirrors the legacy detection in ``_assemble_tool_list``: a
        conversation @-mention injects this marker, which enables the
        conversation-reference tools.
        """
        if not self.messages:
            return False
        for m in self.messages:
            c = m.get('content', '')
            if isinstance(c, str) and '[REFERENCED_CONVERSATION' in c:
                return True
        return False


# ══════════════════════════════════════════════════════════
#  ToolSpec — one self-describing tool (or tool family)
# ══════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ToolSpec:
    """A declarative tool contribution.

    Parameters
    ----------
    key:
        Unique feature key (e.g. ``'search'``, ``'project'``, ``'memory'``).
        Used for de-dup, introspection, and log lines — NOT shown to the LLM.
    build:
        ``Callable[[ToolContext], list[dict]]`` returning the OpenAI-style
        function schemas to add (possibly empty).  Called at request time,
        so do lazy imports here.  May log and may inspect
        :attr:`ToolContext.current_count` / :attr:`~ToolContext.has_base_tools`.
    phase:
        ``'base'`` (counted toward ``has_real_tools``, emitted first) or
        ``'capability'`` (emitted after the base/capability boundary).
    provides:
        Tool names this spec can contribute.  Used for introspection and to
        derive write/idempotent partitioning downstream.  Optional — purely
        informational; the schemas returned by ``build`` are authoritative.
    write_tools / idempotent_tools:
        Subsets of :attr:`provides` that mutate state / are safe to cache.
        Consumed by ``tool_dispatch`` to keep its concurrency + dedup
        partitions in sync without a second hand-maintained list.
    category / description:
        Human-readable metadata for tooling and docs.
    handler:
        Optional :data:`ToolHandlerFn` that executes this tool's calls.  When
        set, it is auto-synced into the dispatch ``tool_registry`` for every
        name in :attr:`provides` (or :attr:`handler_names` if given).  This is
        what lets an EXTERNAL plugin ship schema + gate + executor from a
        single ``tofu.tools`` entry point — no separate handler-registration
        step in core.  Built-in tools leave this ``None`` and keep registering
        handlers via ``@tool_registry`` decorators in
        ``lib/tasks_pkg/handlers/`` (unchanged).
    handler_names:
        Override the set of tool names :attr:`handler` is bound to.  Defaults
        to :attr:`provides`.  Use this when the handler serves names not listed
        in ``provides`` (rare).
    handler_special:
        If set (e.g. ``'__code_exec__'``), register :attr:`handler` as a
        *special* dispatch key instead of by exact name.
    """

    key: str
    build: Callable[[ToolContext], list[dict]]
    phase: str = 'base'
    provides: frozenset[str] = field(default_factory=frozenset)
    write_tools: frozenset[str] = field(default_factory=frozenset)
    idempotent_tools: frozenset[str] = field(default_factory=frozenset)
    category: str = ''
    description: str = ''
    handler: ToolHandlerFn | None = None
    handler_names: frozenset[str] = field(default_factory=frozenset)
    handler_special: str = ''


# ── Module-level registry ─────────────────────────────────
_TOOL_SPECS: list[ToolSpec] = []
_REGISTERED_KEYS: set[str] = set()


def register_tool_spec(spec: ToolSpec, *, replace: bool = False) -> None:
    """Register a :class:`ToolSpec`.

    Built-ins register at import time (preserving the canonical order);
    plugins register via the ``tofu.tools`` entry point.

    Args:
        spec: The spec to add.
        replace: If ``True`` and a spec with the same ``key`` exists, replace
            it in place (preserving position).  Otherwise a duplicate key is
            rejected with a warning so a misbehaving plugin can't silently
            shadow a built-in.
    """
    if spec.key in _REGISTERED_KEYS:
        if replace:
            for i, existing in enumerate(_TOOL_SPECS):
                if existing.key == spec.key:
                    _TOOL_SPECS[i] = spec
                    logger.info('[ToolRegistry] replaced spec key=%s', spec.key)
                    _sync_one(spec, _dispatch_registry)
                    return
        logger.warning('[ToolRegistry] duplicate spec key=%s ignored '
                       '(pass replace=True to override)', spec.key)
        return
    if spec.phase not in ('base', 'capability'):
        logger.warning('[ToolRegistry] spec key=%s has unknown phase=%r; '
                       'treating as capability', spec.key, spec.phase)
    _TOOL_SPECS.append(spec)
    _REGISTERED_KEYS.add(spec.key)
    # If the dispatch registry already exists (late registration, e.g. a plugin
    # loaded after startup), sync this spec's handler immediately.  At import
    # time _dispatch_registry is None and the executor's startup
    # sync_spec_handlers() picks everything up.
    _sync_one(spec, _dispatch_registry)


def all_specs() -> list[ToolSpec]:
    """Return the registered specs in registration order (a shallow copy)."""
    return list(_TOOL_SPECS)


# ── Handler sync: push spec-attached handlers into the dispatch registry ──
# The dispatch registry (``lib.tasks_pkg.executor.tool_registry``) is created
# AFTER this module is imported, so we can't bind at module-load time.  The
# executor calls :func:`sync_spec_handlers` once at startup; thereafter
# :func:`register_tool_spec` syncs each late-registered spec on its own.
_dispatch_registry: Any = None


def _sync_one(spec: ToolSpec, registry: Any) -> None:
    """Register *spec*'s handler (if any) into the dispatch *registry*."""
    if spec.handler is None or registry is None:
        return
    try:
        if spec.handler_special:
            registry.register_special(
                spec.handler_special, spec.handler,
                category=spec.category, description=spec.description)
            logger.info('[ToolRegistry] synced handler for special key=%s '
                        '(spec=%s)', spec.handler_special, spec.key)
            return
        names = spec.handler_names or spec.provides
        if not names:
            logger.warning('[ToolRegistry] spec key=%s has a handler but no '
                           'provides/handler_names to bind it to — skipped',
                           spec.key)
            return
        registry.register(
            set(names), spec.handler,
            category=spec.category, description=spec.description)
        logger.info('[ToolRegistry] synced handler for %s (spec=%s)',
                    sorted(names), spec.key)
    except Exception as e:
        logger.error('[ToolRegistry] failed to sync handler for spec=%s: %s',
                     spec.key, e, exc_info=True)


def sync_spec_handlers(registry: Any) -> int:
    """Bind every spec-attached handler into *registry*; remember it.

    Called once by the executor at startup (after ``tool_registry`` exists and
    the built-in ``@tool_registry`` decorators have run).  Idempotent —
    re-registering the same name is a harmless overwrite.

    Returns:
        Count of specs whose handler was synced.
    """
    global _dispatch_registry
    _dispatch_registry = registry
    count = 0
    for spec in _TOOL_SPECS:
        if spec.handler is not None:
            _sync_one(spec, registry)
            count += 1
    return count


def assemble_tool_list(ctx: ToolContext) -> tuple[list[dict], bool]:
    """Build the active tool list from registered specs.

    Emits ``phase='base'`` specs first (counted toward ``has_base_tools``),
    then ``phase='capability'`` specs.  The running count and the
    ``has_base_tools`` flag are exposed on *ctx* between phases so specs can
    self-gate.

    Returns:
        ``(tool_list, has_base_tools)``.  ``tool_list`` may be empty.
    """
    tool_list: list[dict] = []

    # ── Base phase ──
    for spec in _TOOL_SPECS:
        if spec.phase != 'base':
            continue
        ctx.current_count = len(tool_list)
        try:
            contributed = spec.build(ctx) or []
        except Exception as e:
            logger.error('[ToolRegistry] spec %s build failed: %s',
                         spec.key, e, exc_info=True)
            contributed = []
        tool_list.extend(contributed)

    ctx.has_base_tools = len(tool_list) > 0

    # ── Capability phase ──
    for spec in _TOOL_SPECS:
        if spec.phase != 'capability':
            continue
        ctx.current_count = len(tool_list)
        try:
            contributed = spec.build(ctx) or []
        except Exception as e:
            logger.error('[ToolRegistry] spec %s build failed: %s',
                         spec.key, e, exc_info=True)
            contributed = []
        tool_list.extend(contributed)

    return tool_list, ctx.has_base_tools


# ══════════════════════════════════════════════════════════
#  Built-in spec builders — each reproduces one legacy branch
#  exactly (including its logging).  Heavy imports stay lazy.
# ══════════════════════════════════════════════════════════

def _build_search(ctx: ToolContext) -> list[dict]:
    # 'single' is a retired mode kept as a legacy alias for old conversations
    # — it now behaves like 'multi' (the one-shot SEARCH_TOOL_SINGLE schema
    # was removed). Only 'off' yields no search tool.
    from lib.tools import SEARCH_TOOL_MULTI
    if ctx.search_mode in ('single', 'multi'):
        return [SEARCH_TOOL_MULTI]
    return []


def _build_fetch(ctx: ToolContext) -> list[dict]:
    from lib.tools import FETCH_URL_TOOL
    if ctx.fetch_enabled or ctx.search_enabled:
        return [FETCH_URL_TOOL]
    return []


def _build_read_files(ctx: ToolContext) -> list[dict]:
    # read_files is ALWAYS on — handles project-relative AND absolute local
    # paths (images, PDFs, Office docs, text), so the model can read local
    # content even with no project attached.
    from lib.tools import READ_FILES_TOOL
    if ctx.project_enabled and ctx.multiroot_active:
        from lib.tools.project import with_multiroot_hint
        return with_multiroot_hint([READ_FILES_TOOL])
    return [READ_FILES_TOOL]


def _build_project_or_code_exec(ctx: ToolContext) -> list[dict]:
    from lib.tools import CODE_EXEC_TOOL, PROJECT_TOOLS
    if ctx.project_enabled:
        if ctx.multiroot_active:
            from lib.tools.project import with_multiroot_hint
            return with_multiroot_hint(PROJECT_TOOLS)
        return list(PROJECT_TOOLS)
    if ctx.code_exec_enabled:
        return [CODE_EXEC_TOOL]
    return []


def _build_browser(ctx: ToolContext) -> list[dict]:
    if not ctx.browser_enabled:
        return []
    from lib.browser import is_extension_connected
    if is_extension_connected():
        from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
        from lib.tools import BROWSER_TOOLS
        tools = list(BROWSER_TOOLS) + list(ADVANCED_BROWSER_TOOLS)
        logger.debug('[Task %s] Browser extension connected — browser tools '
                     'enabled (%d tools)', ctx.tid, len(tools))
        return tools
    logger.warning('[Task %s] Browser requested but extension not connected',
                   ctx.tid)
    return []


def _build_desktop(ctx: ToolContext) -> list[dict]:
    if not ctx.desktop_enabled:
        return []
    from lib.desktop import is_desktop_agent_connected
    if is_desktop_agent_connected():
        from lib.desktop_tools import DESKTOP_TOOLS
        logger.debug('[Task %s] 🖥️ Desktop agent connected — %d desktop tools '
                     'enabled', ctx.tid, len(DESKTOP_TOOLS))
        return list(DESKTOP_TOOLS)
    logger.warning('[Task %s] Desktop requested but agent not connected',
                   ctx.tid)
    return []


def _build_image_gen(ctx: ToolContext) -> list[dict]:
    if not ctx.image_gen_enabled:
        return []
    from lib.tools.image_gen import GENERATE_IMAGE_TOOL
    logger.debug('[Task %s] 🎨 Image generation tool enabled', ctx.tid)
    return [GENERATE_IMAGE_TOOL]


def _build_conv_ref(ctx: ToolContext) -> list[dict]:
    # Only when the user @-mentioned a conversation AND some base tool exists.
    if ctx.has_conv_ref and ctx.current_count > 0:
        from lib.tools import CONV_REF_TOOLS
        logger.debug('[Task %s] 💬 Conversation @mention detected — conv_ref '
                     'tools enabled', ctx.tid)
        return list(CONV_REF_TOOLS)
    return []


def _build_human_guidance(ctx: ToolContext) -> list[dict]:
    if ctx.human_guidance_enabled and ctx.current_count > 0:
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        logger.info('[Task %s] 🙋 Human guidance (ask_human) tool enabled',
                    ctx.tid)
        return [ASK_HUMAN_TOOL]
    if ctx.human_guidance_enabled:
        logger.debug('[Task %s] 🙋 Human guidance requested but no base tools '
                     '— skipped', ctx.tid)
    return []


def _build_memory(ctx: ToolContext) -> list[dict]:
    # Memory tools attach whenever ANY real tool exists.  Note: this is gated
    # on has_base_tools, NOT on memoryEnabled — the memoryEnabled flag only
    # controls the system-prompt memory instructions (see system_context.py).
    if not ctx.has_base_tools:
        return []
    from lib.memory import ALL_MEMORY_TOOLS
    return list(ALL_MEMORY_TOOLS)


def _build_scheduler(ctx: ToolContext) -> list[dict]:
    if ctx.scheduler_enabled and ctx.has_base_tools:
        from lib.scheduler.tool_defs import SCHEDULER_TOOLS
        logger.debug('[Task %s] ⏰ Scheduler tools enabled (%d tools)',
                     ctx.tid, len(SCHEDULER_TOOLS))
        return list(SCHEDULER_TOOLS)
    return []


def _build_swarm(ctx: ToolContext) -> list[dict]:
    # NOT gated on has_base_tools — a bare-conversation research swarm is a
    # valid use case (mirrors the read_files decoupling).
    if not ctx.swarm_enabled:
        return []
    from lib.swarm.tools import (
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
        SPAWN_AGENTS_TOOL,
    )
    logger.debug('[Task %s] 🐝 Async swarm enabled — spawn_agents / '
                 'await_agents / get_agent_result (project_enabled=%s)',
                 ctx.tid, ctx.project_enabled)
    return [SPAWN_AGENTS_TOOL, AWAIT_AGENTS_TOOL, GET_AGENT_RESULT_TOOL]


def _build_mcp(ctx: ToolContext) -> list[dict]:
    # Bridge to external MCP servers — schemas fetched dynamically at request
    # time.  Default: enabled.  Benchmarks may pass mcpEnabled=False.
    if not ctx.cfg.get('mcpEnabled', True):
        logger.debug('[Task %s] MCP disabled via mcpEnabled=false', ctx.tid)
        return []
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if bridge.connected:
            mcp_tools = bridge.get_openai_tool_defs()
            if mcp_tools:
                logger.info('[Task %s] 🔌 MCP tools loaded: %d from %d servers',
                            ctx.tid, len(mcp_tools), bridge.server_count)
                return list(mcp_tools)
    except Exception as e:
        logger.debug('[Task %s] MCP bridge not available: %s', ctx.tid, e)
    return []


def _register_builtins() -> None:
    """Register the built-in tool specs in canonical (cache-stable) order."""
    builtins = [
        # ── base phase (counted toward has_real_tools) ──
        ToolSpec('search', _build_search, phase='base',
                 provides=frozenset({'web_search'}),
                 idempotent_tools=frozenset({'web_search'}),
                 category='search', description='Web search'),
        ToolSpec('fetch', _build_fetch, phase='base',
                 provides=frozenset({'fetch_url'}),
                 idempotent_tools=frozenset({'fetch_url'}),
                 category='search', description='Fetch a URL'),
        ToolSpec('read_files', _build_read_files, phase='base',
                 provides=frozenset({'read_files'}),
                 idempotent_tools=frozenset({'read_files'}),
                 category='project', description='Read local files'),
        ToolSpec('project', _build_project_or_code_exec, phase='base',
                 provides=frozenset({
                     'list_dir', 'grep_search', 'find_files',
                     'write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'create_project', 'run_command',
                 }),
                 write_tools=frozenset({
                     'write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents',
                     'create_project', 'run_command',
                 }),
                 idempotent_tools=frozenset({
                     'list_dir', 'grep_search', 'find_files',
                 }),
                 category='project', description='Project file tools / code exec'),
        ToolSpec('browser', _build_browser, phase='base',
                 category='browser', description='Browser automation tools'),
        ToolSpec('desktop', _build_desktop, phase='base',
                 category='desktop', description='Desktop agent tools'),
        ToolSpec('image_gen', _build_image_gen, phase='base',
                 provides=frozenset({'generate_image'}),
                 category='image', description='Image generation'),
        ToolSpec('conv_ref', _build_conv_ref, phase='base',
                 provides=frozenset({'list_conversations', 'get_conversation'}),
                 idempotent_tools=frozenset({'list_conversations', 'get_conversation'}),
                 category='conversation', description='Conversation reference tools'),
        ToolSpec('human_guidance', _build_human_guidance, phase='base',
                 provides=frozenset({'ask_human'}),
                 category='human', description='Ask the human for guidance'),
        # ── capability phase ──
        ToolSpec('memory', _build_memory, phase='capability',
                 write_tools=frozenset({
                     'create_memory', 'update_memory',
                     'delete_memory', 'merge_memories',
                 }),
                 category='memory', description='Memory CRUD tools'),
        ToolSpec('scheduler', _build_scheduler, phase='capability',
                 category='scheduler', description='Scheduler / proactive agent tools'),
        ToolSpec('swarm', _build_swarm, phase='capability',
                 provides=frozenset({
                     'spawn_agents', 'await_agents', 'get_agent_result',
                 }),
                 category='swarm', description='Async multi-agent swarm'),
        ToolSpec('mcp', _build_mcp, phase='capability',
                 category='mcp', description='External MCP-server tools'),
    ]
    for spec in builtins:
        register_tool_spec(spec)


# ══════════════════════════════════════════════════════════
#  Plugin discovery — external packages contribute via entry points
# ══════════════════════════════════════════════════════════

def discover_plugin_specs() -> int:
    """Load third-party tool specs from the ``tofu.tools`` entry-point group.

    A plugin package declares in its ``pyproject.toml``::

        [project.entry-points."tofu.tools"]
        weather = "my_pkg.weather:register"

    where ``register`` is a callable that receives :func:`register_tool_spec`
    and uses it to add one or more :class:`ToolSpec` objects::

        def register(register_tool_spec):
            register_tool_spec(ToolSpec('weather', _build_weather, ...))

    Failures in any single plugin are logged and skipped — a broken plugin
    never takes down tool assembly.

    Returns:
        The number of entry points successfully loaded.
    """
    loaded = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover — importlib.metadata always present on 3.8+
        logger.debug('[ToolRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group='tofu.tools')
    except TypeError:
        # Python <3.10 returns a dict-like; filter by group key.
        eps = entry_points().get('tofu.tools', [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[ToolRegistry] entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        try:
            register_fn = ep.load()
            register_fn(register_tool_spec)
            loaded += 1
            logger.info('[ToolRegistry] loaded plugin tool spec(s) from %s',
                        ep.name)
        except Exception as e:
            logger.warning('[ToolRegistry] plugin %s failed to load: %s',
                           getattr(ep, 'name', '?'), e, exc_info=True)
    return loaded


# Register built-ins + discover plugins at import time.
_register_builtins()
discover_plugin_specs()
