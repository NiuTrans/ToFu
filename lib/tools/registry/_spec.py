"""lib/tools/registry/_spec.py — ToolContext / ToolSpec + the spec registry.

The **single home** of the process-global spec registry:

  * :data:`_TOOL_SPECS` — the ordered list of registered :class:`ToolSpec`
    objects (registration order is prompt-cache-critical).
  * :data:`_REGISTERED_KEYS` — the set of keys already registered (dedup).
  * :data:`_dispatch_registry` — the late-bound dispatch registry the executor
    installs at startup via :func:`sync_spec_handlers`.

These live here and ONLY here. ``_build`` (built-in registration) and
``_plugins`` (entry-point discovery) both append to the SAME :data:`_TOOL_SPECS`
list through :func:`register_tool_spec`, and :func:`all_specs` /
:func:`assemble_tool_list` read it. The package ``__init__`` re-exports the
list/set objects themselves, so tests that mutate ``registry._TOOL_SPECS[:]``
in place touch this single home.

Dependency direction: ``_spec → _latch`` only (``ToolContext.multiroot_active``
uses the sticky-latch helpers). Never the reverse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lib.log import get_logger

from lib.tools.registry._latch import (
    clear_tool_list_latch,
    is_multiroot_sticky,
    is_project_ready_sticky,
    mark_multiroot_sticky,
    mark_project_ready_sticky,
)

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
    # ``lean`` is a retained backend seam (chat_mode.is_lean_mode, currently
    # always False after the air/pro tier merge). When True, the always-on
    # capability tools that attach purely on has_base_tools — memory / todo /
    # scheduler — skip themselves, shipping only search+fetch+read+inspect
    # (≈4 tools) instead of ~15. Kept for a future "auto-retract tools on a
    # simple turn" feature. See lib/tasks_pkg/chat_mode.is_lean_mode.
    lean: bool = False
    messages: list[dict[str, Any]] | None = None

    # Conversation id — used to make schema-shaping decisions sticky for a
    # conversation's lifetime (e.g. the multi-root path hint). Empty for
    # one-off / stateless assembly (tests, compat adapters).
    conv_id: str = ''

    # ── Multi-tenant plugin visibility allow-list ──
    # Which third-party (``source='plugin'``) tool specs this task may see.
    #   * ``None``  → ALL plugins visible (single-tenant / legacy behaviour —
    #                 e.g. a dedicated app/ deployment that owns its process).
    #   * ``set()`` → NO plugins visible (the safe headless multi-tenant
    #                 default: a shared server never leaks one tenant's
    #                 plugins to another).
    #   * ``{names}`` → only plugins whose ``ToolSpec.plugin_name`` is in the
    #                 set are visible.
    # Built-in specs are NEVER affected by this field. Populated from
    # ``cfg['plugins']`` (per-request) falling back to the
    # ``TOFU_DEFAULT_TOOL_PLUGINS`` env var — see :meth:`resolve_enabled_plugins`.
    enabled_plugins: set[str] | None = None

    # ── Mutated by the assembler between phases ──
    current_count: int = 0
    has_base_tools: bool = False

    @property
    def tid(self) -> str:
        """Short task-id prefix for log lines."""
        return (self.task_id or '')[:8]

    def plugin_allowed(self, plugin_name: str) -> bool:
        """Whether a ``source='plugin'`` spec named *plugin_name* is visible.

        ``enabled_plugins is None`` → all plugins allowed (legacy / dedicated
        single-tenant process). Otherwise the plugin must be explicitly listed.
        A plugin spec with an empty ``plugin_name`` (a misconfigured plugin
        that didn't get tagged) is treated as NOT allow-listed unless the gate
        is fully open (``None``) — fail-closed, never leak by accident.
        """
        if self.enabled_plugins is None:
            return True
        return bool(plugin_name) and plugin_name in self.enabled_plugins

    @property
    def multiroot_active(self) -> bool:
        """True when more than one workspace root is configured for this task.

        Read from ``cfg['projectPaths']`` (the full root list the frontend
        sends; element 0 is the primary, the rest are extras). Used to decide
        whether path-taking tool schemas should carry the ``rootname:`` prefix
        hint — single-root sessions keep the cache-stable default schema.

        **Sticky per conversation.** Once a conversation has gone multi-root,
        the hint stays on for the rest of that conversation even if a later
        task transiently reports a single ``projectPaths`` (e.g. an extra root
        was auto-registered mid-conversation by an absolute-path write, then a
        subsequent task's snapshot lags). Flapping this value rewrites every
        path-taking tool's schema and breaks the prompt-cache prefix — see
        ``mark_multiroot_sticky``. A conversation never silently downgrades to
        single-root mid-stream; the latch is cleared only on cleanup.
        """
        live = self._multiroot_live()
        if not self.conv_id:
            # Stateless assembly (tests / compat adapters): no latch.
            return live
        if live:
            # Going multi-root is a LEGITIMATE one-time schema change: the
            # model in THIS conversation needs the ``rootname:`` path hint
            # immediately. On the OFF→ON transition, re-establish the
            # tool-schema latch so the next assembly (this same round, since
            # multiroot_active is read before latch_tool_list) re-freezes the
            # snapshot WITH the hint — one deliberate cache rebuild, then
            # byte-stable again, and no permanent phantom empty-name-diff
            # divergence. Mirrors the clear_all_tool_list_latches MCP-mutation
            # precedent. Idempotent: only the first mark fires the clear.
            if mark_multiroot_sticky(self.conv_id):
                clear_tool_list_latch(self.conv_id)
                logger.info('[ToolLatch] conv=%s went multi-root — cleared '
                            'tool-schema latch so the rootname hint re-freezes '
                            '(one-time cache rebuild)', self.conv_id[:8])
            return True
        return is_multiroot_sticky(self.conv_id)

    def _multiroot_live(self) -> bool:
        """The raw, un-latched multi-root signal from this task's cfg."""
        paths = self.cfg.get('projectPaths') or []
        if not isinstance(paths, (list, tuple)):
            return False
        distinct = {p for p in paths if p}
        return len(distinct) > 1

    @property
    def project_ready(self) -> bool:
        """True when this task has a project attached (``project_enabled``).

        Read by the project spec builder (:func:`_build_project_or_code_exec`)
        to gate the project tool family (list_dir / grep_search / find_files /
        write_file / apply_diff / … / run_command).

        **Sticky per conversation, but only the OFF→ON transition matters.**
        A conversation whose FIRST turn had no project (empty roots) freezes a
        no-project tool-schema snapshot; without this hook, attaching a project
        on a later turn is masked forever by the tool-schema latch. Attaching a
        project mid-conversation is a LEGITIMATE one-time schema change, so on
        the OFF→ON transition we clear the tool-schema latch (exactly like the
        multi-root OFF→ON path) so the next assembly — this same round, since
        this property is read before ``latch_tool_list`` — re-freezes the
        snapshot WITH the project tools. One deliberate cache rebuild, then
        byte-stable again.

        Note the asymmetry vs :attr:`multiroot_active`: this returns the LIVE
        ``project_enabled`` value (never forcing the sticky ON when live is
        False), because a project genuinely being detached mid-conversation
        SHOULD drop the tools. The sticky set is used ONLY to fire the
        one-time latch-clear on the first attach, not to pin the gate on.
        """
        live = bool(self.project_enabled)
        if not self.conv_id:
            # Stateless assembly (tests / compat adapters): raw signal, no latch.
            return live
        if live and mark_project_ready_sticky(self.conv_id):
            # First time this conversation has a project attached. If it had
            # already frozen a no-project tool-schema snapshot on an earlier
            # (empty-roots) round, clear it so the project tools re-freeze in.
            clear_tool_list_latch(self.conv_id)
            logger.info('[ToolLatch] conv=%s attached a project — cleared '
                        'tool-schema latch so the project tools re-freeze '
                        '(one-time cache rebuild)', self.conv_id[:8])
        return live

    @property
    def has_conv_ref(self) -> bool:
        """True when a USER turn actually attached a referenced conversation.

        Enables the conversation-reference tools (``list_conversations`` /
        ``get_conversation``) only when the user genuinely attached a
        conversation via the ``@`` affordance — never because the literal
        token happens to appear in free-form prose.

        Detection, in priority order, scanning **user-role messages only**:
          1. The structured ``convRefs`` / ``convRefTexts`` field — the
             authoritative signal set by the send path when a reference is
             attached (present on raw conversation rows).
          2. The server-injected wrapper signature
             ``[REFERENCED_CONVERSATION`` ... ``title="`` — what
             ``conv_message_builder`` prepends to the user message after
             resolving a ref (present on API-built messages, which no longer
             carry ``convRefs``). The ``title="`` guard distinguishes the
             real injected block from someone quoting the bare token.

        Assistant content is NEVER scanned: a conversation *about* this
        feature (where the model quotes the marker, as in this very chat)
        must not self-enable the tools and break the prompt-cache latch.
        """
        if not self.messages:
            return False
        for m in self.messages:
            if m.get('role') != 'user':
                continue
            if m.get('convRefs') or m.get('convRefTexts'):
                return True
            c = m.get('content', '')
            if isinstance(c, str) and '[REFERENCED_CONVERSATION' in c \
                    and 'title="' in c:
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
    source:
        Provenance of this spec — ``'builtin'`` (registered by core at import
        time) or ``'plugin'`` (contributed by a third-party ``tofu.tools``
        entry point).  Set automatically by :func:`discover_plugin_specs`;
        built-ins keep the default.  Drives the per-request visibility gate in
        :func:`assemble_tool_list`: ``'builtin'`` specs are ALWAYS evaluated,
        ``'plugin'`` specs only when allow-listed via
        :attr:`ToolContext.enabled_plugins`.  This is the multi-tenant
        isolation seam — see the module-level "Plugin isolation" note.
    plugin_name:
        For ``source='plugin'`` specs, the entry-point name the spec was loaded
        from (e.g. ``'liantong_kb'``).  This — NOT :attr:`key` — is what a
        caller lists in ``config.plugins`` / ``TOFU_DEFAULT_TOOL_PLUGINS`` to
        make the plugin visible.  One entry point may register several specs;
        they all share its ``plugin_name``.  Empty for built-ins.
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
    source: str = 'builtin'
    plugin_name: str = ''


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

    def _visible(spec: ToolSpec) -> bool:
        # Built-ins always evaluated; plugins gated by the per-request
        # allow-list so one tenant's installed plugin can't leak into another's
        # tool surface on a shared server.
        if spec.source != 'plugin':
            return True
        if ctx.plugin_allowed(spec.plugin_name):
            return True
        logger.debug('[Task %s] plugin spec key=%s (plugin=%s) hidden — not in '
                     'enabled_plugins', ctx.tid, spec.key, spec.plugin_name)
        return False

    # ── Base phase ──
    for spec in _TOOL_SPECS:
        if spec.phase != 'base' or not _visible(spec):
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
        if spec.phase != 'capability' or not _visible(spec):
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
