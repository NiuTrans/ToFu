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

Dependency direction: ``_spec`` depends only on schema/build-neutral helpers;
the built-in builders import it, never the reverse.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# A dispatch handler — same signature as lib.protocols.ToolHandler.  Typed
# loosely here to avoid importing the protocol into this low-level module.
ToolHandlerFn = Callable[..., tuple[str, str, bool]]
ToolResultMetaBuilder = Callable[
    [str, dict[str, Any], str],
    dict[str, Any],
]


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
    image_gen_enabled: bool = False
    human_guidance_enabled: bool = False
    scheduler_enabled: bool = False
    # Authenticated owner used by owner-scoped capability discovery. Zero means
    # no durable/browser authority (for static inventory and isolated tests).
    owner_user_id: int = 0
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
    # Populated only by the opt-in native router.  They are diagnostic state,
    # not a second authority for tool execution.
    routed_spec_keys: set[str] = field(default_factory=set)
    omitted_spec_keys: set[str] = field(default_factory=set)
    # Tool Search policy sidecars.  The registry records provenance while it
    # still knows which ToolSpec produced each schema.  Protocol adapters use
    # these sidecars later; they never ride a non-Responses wire.
    frontend_selected_tool_names: set[str] = field(default_factory=set)
    tool_namespace_by_name: dict[str, str] = field(default_factory=dict)
    # The complete task-local execution surface.  Unlike the wire schema this
    # is never reduced by composer/native exposure or provider-side discovery.
    executable_tool_catalog: list[dict[str, Any]] = field(default_factory=list)
    discovery_policy_by_name: dict[str, str] = field(default_factory=dict)
    script_safe_by_name: dict[str, bool] = field(default_factory=dict)
    # Private retrieval text (ToolSpec family hints plus MCP aliases/intents).
    # It is task-local search data, never serialized into provider tool schemas.
    search_text_by_name: dict[str, str] = field(default_factory=dict)
    # Canonical rich contracts stay server-side and are returned only after
    # Tool Search. Existing schemas receive a read-only v2 adapter here.
    tool_contract_documents_by_name: dict[str, dict[str, Any]] = field(
        default_factory=dict)

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
    def durable_state_available(self) -> bool:
        """Whether storage-backed tool families may join this task surface."""
        return not bool(self.cfg.get('_storageFreeRuntime'))

    @property
    def multiroot_active(self) -> bool:
        """True when more than one workspace root is configured for this task.

        Read from ``cfg['projectPaths']`` (the full root list the frontend
        sends; element 0 is the primary, the rest are extras). Used to decide
        whether path-taking tool schemas should carry the ``rootname:`` prefix
        hint. The value is evaluated live for every assembly.
        """
        return self._multiroot_live()

    def _multiroot_live(self) -> bool:
        """Return the current request's multi-root signal."""
        paths = self.cfg.get('projectPaths') or []
        if not isinstance(paths, (list, tuple)):
            return False
        distinct = {p for p in paths if p}
        return len(distinct) > 1

    @property
    def project_ready(self) -> bool:
        """True when this task has a project attached (``project_enabled``).

        Read by the project spec builder (:func:`_build_project_or_code_exec`)
        to gate the project tool family (grep_search / find_files /
        write_file / apply_diff / … / run_command).

        This is deliberately live on every assembly: attaching a project adds
        the project tools on the next model round, and detaching it removes
        them on the next model round.
        """
        return bool(self.project_enabled)

    @property
    def project_remote(self) -> bool:
        """True when this task's project is bound to a REMOTE worktree.

        Tool names and parameter schemas stay identical; descriptions gain the
        local-execution hint from :func:`lib.tools.project.with_remote_hint`.
        The binding is evaluated live for every assembly.
        """
        from lib.desktop.remote import remote_worktree_binding
        return remote_worktree_binding(self.cfg) is not None

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
        Subsets of :attr:`provides` that mutate state / may be retried without
        changing their semantic effect.  Idempotency is a contract property;
        it is deliberately not the cache policy.
    cacheable_tools:
        Subset of :attr:`provides` whose successful result may be reused
        within one task. ``None`` preserves the legacy plugin convention that
        idempotent tools are cacheable; an explicit empty set opts a mutable
        observer (for example a live browser page) out of result caching.
    unchanged_receipt_tools:
        Subset of :attr:`provides` whose fresh byte-identical observations may
        use a compact model projection while the prior full projection remains
        in active context. Execution is never skipped by this policy.
    confirmation_tools:
        Subset of :attr:`write_tools` that always requires an attended human
        confirmation, independent of the conversation's ordinary Auto/Manual
        write mode. Unattended execution fails closed.
    programmatic_tools:
        Subset of :attr:`provides` explicitly reviewed for execution from an
        OpenAI Programmatic Tool Calling program.  This is intentionally NOT
        derived from :attr:`idempotent_tools`: retry-safety alone does not
        prove that a tool preserves citations/native artifacts, avoids an
        approval boundary, or has a predictable program-facing result.
    category / description:
        Human-readable metadata for tooling and docs.
    gate:
        One short human-readable sentence naming the switch that turns this
        family on (e.g. ``'设置 → 搜索 → 联网搜索'`` or ``'连接浏览器扩展'``).
        Purely diagnostic — emitted by
        ``_introspect.build_tool_inventory`` for headless clients. The
        Settings → 工具 page is a global catalogue and deliberately does
        not render request-local gate state. Never consulted by gating logic;
        the ``build`` callable remains the only authority.
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
    catalog_active_only:
        Omit this family from the global Settings catalogue when ``build``
        contributes no live schemas.  Most families deliberately advertise
        their gated surface; data-backed families such as local knowledge use
        this to avoid showing a callable tool before any corpus exists.
    discovery_policy:
        ``'eager'`` keeps the family's schemas on the initial wire surface;
        ``'searchable'`` makes them discoverable through Tool Search while
        retaining them in the immutable task-level executable catalog.
    script_safe:
        Whether calls from a local ToolScript may execute without first being
        handed back to the ordinary approval/execution lane.  This is a trust
        property and deliberately independent of discovery policy.
    result_meta_builder:
        Optional plugin-owned adapter for producing the frontend result
        metadata of names in :attr:`provides`. Core execution reaches this
        callable through :func:`build_tool_result_meta`; it never imports the
        concrete tool family that owns the metadata shape.
    result_recovery_by_name:
        Per-tool overflow recovery policy. ``source`` means the observation is
        cheaply reconstructible from its original arguments and must not be
        copied into the generic artifact store; ``artifact`` is the safe
        default for volatile or expensive results; ``none`` exposes only a
        bounded preview. This is execution policy, not presentation metadata.
    """

    key: str
    build: Callable[[ToolContext], list[dict]]
    phase: str = 'base'
    provides: frozenset[str] = field(default_factory=frozenset)
    write_tools: frozenset[str] = field(default_factory=frozenset)
    idempotent_tools: frozenset[str] = field(default_factory=frozenset)
    category: str = ''
    description: str = ''
    gate: str = ''
    handler: ToolHandlerFn | None = None
    handler_names: frozenset[str] = field(default_factory=frozenset)
    handler_special: str = ''
    source: str = 'builtin'
    plugin_name: str = ''
    # Appended for positional-constructor compatibility with third-party specs.
    programmatic_tools: frozenset[str] = field(default_factory=frozenset)
    catalog_active_only: bool = False
    discovery_policy: str = 'eager'
    script_safe: bool = False
    # Private per-function aliases/intents for local discovery. Appended for
    # positional-constructor compatibility and never copied into wire schemas.
    search_hints: dict[str, str] = field(default_factory=dict)
    # Optional request/UI exposure preference.  ``False`` means omit schemas
    # from the wire while keeping task-available tools searchable and exactly
    # callable. Environment/tenant availability remains the builder's job.
    exposure_gate: Callable[[ToolContext], bool] | None = None
    # A searchable family can still be explicitly pinned by its own composer
    # toggle. Piggy-backed gates (for example Produce riding Web Search) leave
    # this false so they remain deferred.
    pin_on_exposure: bool = False
    # Native v2 contracts. Appended for positional compatibility; families
    # without them are adapted from their legacy provider schemas.
    contracts: tuple[Any, ...] = field(default_factory=tuple)
    # Appended for positional compatibility with third-party ToolSpec users.
    result_meta_builder: ToolResultMetaBuilder | None = None
    # Appended for positional compatibility. These are writes whose authority
    # can only be minted by the attended approval UI for this exact call.
    confirmation_tools: frozenset[str] = field(default_factory=frozenset)
    # Appended for positional compatibility. Idempotency describes retry
    # semantics; this field independently controls same-task result reuse.
    cacheable_tools: frozenset[str] | None = None
    # Appended for positional compatibility. These request-router signals are
    # owned by the family instead of duplicated as key-specific exceptions in
    # lib.tools.routing. Any matching signal exposes this eager family.
    native_route_groups: frozenset[str] = field(default_factory=frozenset)
    # Appended for positional compatibility. Keys must be declared in provides.
    result_recovery_by_name: dict[str, str] = field(default_factory=dict)
    # Appended for positional compatibility. Fresh reads only; never a cache.
    unchanged_receipt_tools: frozenset[str] = field(default_factory=frozenset)


# ── Module-level registry ─────────────────────────────────
_TOOL_SPECS: list[ToolSpec] = []
_REGISTERED_KEYS: set[str] = set()
_SPEC_LOCK = RLock()


def _ordinary_claims(spec: ToolSpec) -> set[str]:
    # Policy tables are dispatch authority too. In particular, marking a core
    # write tool cacheable (or relying on the legacy idempotent=>cacheable
    # default) would make repeated calls reusable without replacing its schema
    # or handler, so those names participate in ordinary collision arbitration.
    claims = (
        set(spec.provides)
        | set(spec.write_tools)
        | set(spec.idempotent_tools)
        | set(spec.cacheable_tools or ())
        | set(spec.unchanged_receipt_tools)
        | set(spec.programmatic_tools)
        | set(spec.confirmation_tools)
        | set(spec.result_recovery_by_name)
    )
    if spec.handler is not None and not spec.handler_special:
        claims.update(spec.handler_names)
    return claims


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
    with _SPEC_LOCK:
        saved_specs = list(_TOOL_SPECS)
        saved_keys = set(_REGISTERED_KEYS)
        registry_snap = (
            _dispatch_registry.snapshot()
            if _dispatch_registry is not None
            and hasattr(_dispatch_registry, 'snapshot')
            else None
        )
        try:
            _register_tool_spec_unlocked(spec, replace=replace)
        except Exception:
            _TOOL_SPECS[:] = saved_specs
            _REGISTERED_KEYS.clear()
            _REGISTERED_KEYS.update(saved_keys)
            if registry_snap is not None:
                _dispatch_registry.restore(registry_snap)
            raise


def _register_tool_spec_unlocked(spec: ToolSpec, *, replace: bool) -> None:
    if spec.source not in {'builtin', 'plugin'}:
        raise ValueError(
            f'ToolSpec {spec.key!r} source must be builtin or plugin, '
            f'got {spec.source!r}')
    if spec.handler is not None and not (
            spec.handler_special or spec.handler_names or spec.provides):
        raise ValueError(
            f'ToolSpec {spec.key!r} has a handler but no dispatch names')
    for policy_name in (
            'idempotent_tools', 'programmatic_tools', 'confirmation_tools',
            'unchanged_receipt_tools'):
        undeclared = set(getattr(spec, policy_name)) - set(spec.provides)
        if undeclared:
            raise ValueError(
                f'ToolSpec {spec.key!r} {policy_name} contains undeclared '
                f'tool names: {sorted(undeclared)}')
    if spec.cacheable_tools is not None:
        undeclared_cache = set(spec.cacheable_tools) - set(spec.provides)
        if undeclared_cache:
            raise ValueError(
                f'ToolSpec {spec.key!r} cacheable_tools contains undeclared '
                f'tool names: {sorted(undeclared_cache)}')
    undeclared_recovery = set(spec.result_recovery_by_name) - set(spec.provides)
    if undeclared_recovery:
        raise ValueError(
            f'ToolSpec {spec.key!r} result_recovery_by_name contains '
            f'undeclared tool names: {sorted(undeclared_recovery)}')
    invalid_recovery = {
        name: policy for name, policy in spec.result_recovery_by_name.items()
        if policy not in {'artifact', 'source', 'none'}
    }
    if invalid_recovery:
        raise ValueError(
            f'ToolSpec {spec.key!r} has invalid result recovery policies: '
            f'{invalid_recovery}')
    non_writes = set(spec.confirmation_tools) - set(spec.write_tools)
    if non_writes:
        raise ValueError(
            f'ToolSpec {spec.key!r} confirmation_tools must also be writes: '
            f'{sorted(non_writes)}')

    claims = _ordinary_claims(spec)
    name_collisions = [
        (existing, sorted(claims & _ordinary_claims(existing)))
        for existing in _TOOL_SPECS
        if (existing.key != spec.key
            and claims & _ordinary_claims(existing))
    ]
    if name_collisions and spec.source == 'plugin':
        details = ', '.join(
            f'{owner.key}:{names}' for owner, names in name_collisions)
        logger.warning(
            '[ToolRegistry] plugin %r REFUSED spec key=%r — declared tool '
            'names already have owners (%s)', spec.plugin_name or '?',
            spec.key, details)
        return
    if name_collisions:
        builtin_collisions = [
            (owner, names) for owner, names in name_collisions
            if owner.source == 'builtin'
        ]
        if builtin_collisions:
            details = ', '.join(
                f'{owner.key}:{names}' for owner, names in builtin_collisions)
            raise ValueError(
                f'Built-in ToolSpec {spec.key!r} duplicates core tool names: '
                f'{details}')
        # Core specs normally load first. In a synthetic late-arrival case,
        # remove a colliding plugin family in full: partially trimming a
        # frozen spec would desynchronise its partitions and handler_names.
        for owner, names in name_collisions:
            logger.warning(
                '[ToolRegistry] built-in spec key=%r reclaimed names %s; '
                'removing colliding plugin spec key=%r (plugin=%r)',
                spec.key, names, owner.key, owner.plugin_name or '?')
            _TOOL_SPECS.remove(owner)
            _REGISTERED_KEYS.discard(owner.key)
            _unsync_all(owner, _dispatch_registry)

    if spec.key in _REGISTERED_KEYS:
        existing = next(
            item for item in _TOOL_SPECS if item.key == spec.key)

        # A plugin-controlled ``replace=True`` must not turn the spec key into
        # an authority bypass. Specs define visibility and the write/
        # idempotency partitions in addition to schemas, so replacing a core
        # spec is just as dangerous as replacing its dispatch handler.
        if spec.source == 'plugin':
            if existing.source == 'builtin':
                logger.warning(
                    '[ToolRegistry] plugin %r REFUSED spec key=%r — that key '
                    'belongs to a built-in spec', spec.plugin_name or '?',
                    spec.key)
                return
            if existing.plugin_name != spec.plugin_name:
                logger.warning(
                    '[ToolRegistry] plugin %r REFUSED spec key=%r — already '
                    'provided by plugin %r', spec.plugin_name or '?', spec.key,
                    existing.plugin_name or '?')
                return

        # Preserve the same arrival-order-independent authority as handler
        # registration: core can reclaim a key a plugin registered first.
        if spec.source == 'builtin' and existing.source == 'plugin':
            replace = True
            logger.warning(
                '[ToolRegistry] built-in spec key=%r reclaimed from plugin %r',
                spec.key, existing.plugin_name or '?')

        if replace:
            for i, existing in enumerate(_TOOL_SPECS):
                if existing.key == spec.key:
                    _TOOL_SPECS[i] = spec
                    logger.info('[ToolRegistry] replaced spec key=%s', spec.key)
                    _sync_one(spec, _dispatch_registry)
                    _unsync_removed(existing, spec, _dispatch_registry)
                    return
        logger.warning('[ToolRegistry] duplicate spec key=%s ignored '
                       '(pass replace=True to override)', spec.key)
        return
    if spec.phase not in ('base', 'capability'):
        logger.warning('[ToolRegistry] spec key=%s has unknown phase=%r; '
                       'treating as capability', spec.key, spec.phase)
    if spec.discovery_policy not in ('eager', 'searchable'):
        raise ValueError(
            f'ToolSpec {spec.key!r} discovery_policy must be eager or searchable')
    _TOOL_SPECS.append(spec)
    _REGISTERED_KEYS.add(spec.key)
    # If the dispatch registry already exists (late registration, e.g. a plugin
    # loaded after startup), sync this spec's handler immediately.  At import
    # time _dispatch_registry is None and the executor's startup
    # sync_spec_handlers() picks everything up.
    _sync_one(spec, _dispatch_registry)


def all_specs() -> list[ToolSpec]:
    """Return the registered specs in registration order (a shallow copy)."""
    with _SPEC_LOCK:
        return list(_TOOL_SPECS)


def build_tool_result_meta(
    tool_name: str,
    tool_args: dict[str, Any],
    tool_content: str,
) -> dict[str, Any]:
    """Build result metadata through the owning :class:`ToolSpec` seam.

    The neutral fallback preserves the historical metadata shape for tool
    families that do not need a specialized adapter. The callable is invoked
    outside the registry lock because plugin code must never block registry
    registration or introspection.
    """
    with _SPEC_LOCK:
        builder = next((
            spec.result_meta_builder
            for spec in _TOOL_SPECS
            if tool_name in spec.provides
            and spec.result_meta_builder is not None
        ), None)
    if builder is not None:
        return builder(tool_name, tool_args, tool_content)
    return {
        'title': tool_name,
        'source': 'Project',
        'fetched': True,
        'fetchedChars': len(tool_content),
        'url': '',
        'snippet': tool_content[:120].replace('\n', ' '),
        'badge': '',
    }


# ── Handler sync: push spec-attached handlers into the dispatch registry ──
# The dispatch registry (``lib.tasks_pkg.executor.tool_registry``) is created
# AFTER this module is imported, so we can't bind at module-load time.  The
# executor calls :func:`sync_spec_handlers` once at startup; thereafter
# :func:`register_tool_spec` syncs each late-registered spec on its own.
_dispatch_registry: Any = None


def _sync_one(spec: ToolSpec, registry: Any) -> bool:
    """Register *spec*'s handler (if any) into the dispatch *registry*."""
    if spec.handler is None or registry is None:
        return False
    if spec.handler_special:
        bound = registry.register_special(
            spec.handler_special, spec.handler,
            category=spec.category, description=spec.description,
            source=spec.source, plugin_name=spec.plugin_name)
        if bound is False:
            raise ValueError(
                f'ToolSpec {spec.key!r} could not claim special dispatch key '
                f'{spec.handler_special!r}')
        logger.info('[ToolRegistry] synced handler for special key=%s '
                    '(spec=%s)', spec.handler_special, spec.key)
        return True
    names = spec.handler_names or spec.provides
    bound = registry.register(
        set(names), spec.handler,
        category=spec.category, description=spec.description,
        source=spec.source, plugin_name=spec.plugin_name)
    if isinstance(bound, int) and bound != len(names):
        raise ValueError(
            f'ToolSpec {spec.key!r} could bind only {bound}/{len(names)} '
            'handler names')
    logger.info('[ToolRegistry] synced handler for %s (spec=%s)',
                sorted(names), spec.key)
    return True


def _bound_names(spec: ToolSpec) -> set[str]:
    if spec.handler is None or spec.handler_special:
        return set()
    return set(spec.handler_names or spec.provides)


def _unsync_all(spec: ToolSpec, registry: Any) -> None:
    """Remove every dispatch binding still owned by *spec*."""
    if registry is None or spec.handler is None:
        return
    if spec.handler_special:
        registry.unregister_special(
            spec.handler_special, source=spec.source,
            plugin_name=spec.plugin_name)
        return
    names = _bound_names(spec)
    if names:
        registry.unregister(
            names, source=spec.source, plugin_name=spec.plugin_name)


def _unsync_removed(previous: ToolSpec, current: ToolSpec, registry: Any) -> None:
    """Remove bindings a replaced spec no longer owns.

    The current spec is synced first so overlapping names change handlers
    without an observable unbound window. Owner-checked unregister calls make
    this safe when a built-in has already reclaimed one of the old names.
    """
    if registry is None or previous.handler is None:
        return
    old_special = previous.handler_special
    new_special = current.handler_special if current.handler else ''
    if old_special and old_special != new_special:
        registry.unregister_special(
            old_special, source=previous.source,
            plugin_name=previous.plugin_name)

    removed_names = _bound_names(previous) - _bound_names(current)
    if removed_names:
        registry.unregister(
            removed_names, source=previous.source,
            plugin_name=previous.plugin_name)


def sync_spec_handlers(registry: Any) -> int:
    """Bind every spec-attached handler into *registry*; remember it.

    Called once by the executor at startup (after ``tool_registry`` exists and
    the built-in ``@tool_registry`` decorators have run).  Idempotent —
    re-registering the same name is a harmless overwrite.

    Returns:
        Count of specs whose handler was synced.
    """
    global _dispatch_registry
    with _SPEC_LOCK:
        _dispatch_registry = registry
        specs = list(_TOOL_SPECS)
        count = 0
        failed_plugin_keys: set[str] = set()
        for spec in specs:
            if spec.handler is not None:
                registry_snap = (
                    registry.snapshot()
                    if hasattr(registry, 'snapshot') else None)
                try:
                    if _sync_one(spec, registry):
                        count += 1
                except Exception as e:
                    if registry_snap is not None:
                        registry.restore(registry_snap)
                    logger.error(
                        '[ToolRegistry] failed to sync handler for spec=%s: %s',
                        spec.key, e, exc_info=True)
                    if spec.source == 'plugin':
                        failed_plugin_keys.add(spec.key)
        if failed_plugin_keys:
            _TOOL_SPECS[:] = [
                spec for spec in _TOOL_SPECS
                if spec.key not in failed_plugin_keys
            ]
            _REGISTERED_KEYS.difference_update(failed_plugin_keys)
            logger.error(
                '[ToolRegistry] quarantined plugin specs with unusable handlers: %s',
                sorted(failed_plugin_keys))
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
    with _SPEC_LOCK:
        specs = list(_TOOL_SPECS)
    declared_owner = {
        name: spec.key
        for spec in specs
        for name in _ordinary_claims(spec)
    }
    runtime_owner = dict(declared_owner)
    execution_scope = 'available'
    native_mode = 'full'
    selected_native: set[str] | None = None
    try:
        from lib.context_experiment_flags import (
            normalize_context_experiment_flags)
        normalized_tools = normalize_context_experiment_flags(
            ctx.cfg)['tools']
        native_mode = normalized_tools['nativeExposure']
        execution_scope = normalized_tools['executionScope']
        if native_mode == 'routed':
            from lib.tools.routing import routed_native_spec_keys
            selected_native = routed_native_spec_keys(ctx, specs=specs)
            ctx.routed_spec_keys.update(selected_native)
    except Exception as exc:
        # Routing is experimental and must fail open to full exposure and the
        # default all-task-available execution policy.
        logger.warning('[ToolRouter] selection failed for task=%s: %s; '
                       'using full exposure', ctx.tid, exc, exc_info=True)
        native_mode = 'full'
        execution_scope = 'available'
        selected_native = None

    def _enabled(spec: ToolSpec) -> bool:
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

    def _schema_name(tool: dict) -> str:
        if not isinstance(tool, dict):
            return ''
        func = tool.get('function')
        if isinstance(func, dict):
            return str(func.get('name') or '')
        return str(tool.get('name') or '')

    # ToolContext is normally fresh, but seed from its current authority for
    # compatibility with callers that prepopulate or deliberately reassemble
    # one. Keep this index in lockstep with appends so a large MCP/plugin
    # contribution never rescans the growing catalog once per tool.
    executable_tool_names = {
        name for tool in ctx.executable_tool_catalog
        if (name := _schema_name(tool))
    }

    def _validated_contribution(
            spec: ToolSpec, contributed: list[dict]) -> list[dict]:
        valid: list[dict] = []
        seen_here: set[str] = set()
        for tool in contributed:
            name = _schema_name(tool)
            if not name:
                logger.warning(
                    '[ToolRegistry] spec key=%s returned a schema without a '
                    'function name; dropping it', spec.key)
                continue
            if name in seen_here:
                logger.warning(
                    '[ToolRegistry] spec key=%s returned duplicate schema %r; '
                    'dropping the duplicate', spec.key, name)
                continue
            seen_here.add(name)
            owner_key = runtime_owner.get(name)
            if owner_key and owner_key != spec.key:
                logger.warning(
                    '[ToolRegistry] spec key=%s tried to contribute tool %r '
                    'owned by spec key=%s; dropping the conflicting schema',
                    spec.key, name, owner_key)
                continue
            valid.append(tool)
            runtime_owner[name] = spec.key
        return valid

    def _record_contribution(
            spec: ToolSpec, contributed: list[dict], exposed: bool) -> None:
        namespace = (spec.category or spec.key or 'general').strip().lower()
        # Eager visibility is a discovery policy, not proof that a human chose
        # this individual capability. Only explicit composer/plugin selection
        # becomes a budget pin; otherwise every eager family collapses the
        # priority tiers back into registry order.
        pin = exposed and spec.pin_on_exposure
        # An explicit plugin allow-list is also a caller selection.  A legacy
        # single-tenant "all plugins" default (None) remains searchable.
        if (spec.source == 'plugin'
                and ctx.enabled_plugins is not None
                and ctx.plugin_allowed(spec.plugin_name)):
            pin = True
        authority = list(contributed)
        if spec.key == 'mcp':
            authority = _validated_contribution(
                spec,
                list(ctx.cfg.get('_mcpAllowedToolCatalog') or authority),
            )
            known = {_schema_name(tool) for tool in authority}
            authority.extend(tool for tool in contributed
                             if _schema_name(tool) not in known)
        active_mcp = set(ctx.cfg.get('_mcpActiveToolNames') or [])
        mcp_search_text = ctx.cfg.get('_mcpToolSearchTextByName') or {}
        native_contracts = {
            str(getattr(contract, 'name', '')): contract
            for contract in spec.contracts
            if str(getattr(contract, 'name', ''))
        }
        for tool in authority:
            name = _schema_name(tool)
            if not name:
                continue
            if execution_scope == 'selected_only' and not exposed:
                continue
            if name not in executable_tool_names:
                ctx.executable_tool_catalog.append(tool)
                executable_tool_names.add(name)
            is_active_mcp = spec.key == 'mcp' and name in active_mcp
            ctx.discovery_policy_by_name[name] = (
                'eager' if is_active_mcp else (
                    spec.discovery_policy if exposed else 'searchable'))
            ctx.script_safe_by_name[name] = bool(spec.script_safe)
            ctx.tool_namespace_by_name[name] = namespace
            search_parts = [
                spec.key, spec.category, spec.description,
                str(spec.search_hints.get(name) or ''),
            ]
            if spec.key == 'mcp' and isinstance(mcp_search_text, Mapping):
                search_parts.append(str(mcp_search_text.get(name) or ''))
            ctx.search_text_by_name[name] = ' '.join(
                part for part in search_parts if part)
            try:
                contract = native_contracts.get(name)
                if contract is None:
                    from lib.tools.contracts import adapt_legacy_tool_contract
                    contract = adapt_legacy_tool_contract(
                        tool,
                        namespace=namespace,
                        search_metadata=tuple(
                            part for part in search_parts if part),
                        permission=(
                            'write' if name in spec.write_tools else 'read'),
                        idempotency=(
                            'idempotent' if name in spec.idempotent_tools else
                            'non_idempotent' if name in spec.write_tools else
                            'read_only'),
                        ptc_eligible=name in spec.programmatic_tools,
                        result_recovery=str(
                            spec.result_recovery_by_name.get(
                                name, 'artifact')),
                    )
                document = contract.search_document()
                ctx.tool_contract_documents_by_name[name] = document
                ctx.search_text_by_name[name] = ' '.join(filter(None, (
                    ctx.search_text_by_name[name],
                    str(document.get('help') or ''),
                    ' '.join(document.get('aliases') or ()),
                )))
            except Exception as exc:
                logger.warning(
                    '[ToolRegistry] v2 contract compile failed for %s: %s',
                    name, exc)
            if pin or is_active_mcp:
                ctx.frontend_selected_tool_names.add(name)

    def _wire_visible(spec: ToolSpec, exposed: bool) -> bool:
        if not exposed:
            return False
        if selected_native is None or spec.key in selected_native:
            return True
        ctx.omitted_spec_keys.add(spec.key)
        logger.debug('[Task %s] native spec key=%s hidden by routed exposure',
                     ctx.tid, spec.key)
        return False

    # ── Base phase ──
    for spec in specs:
        if spec.phase != 'base' or not _enabled(spec):
            continue
        exposed = bool(
            spec.exposure_gate(ctx) if spec.exposure_gate else True)
        ctx.current_count = len(ctx.executable_tool_catalog)
        try:
            contributed = spec.build(ctx) or []
        except Exception as e:
            logger.error('[ToolRegistry] spec %s build failed: %s',
                         spec.key, e, exc_info=True)
            contributed = []
        contributed = _validated_contribution(spec, contributed)
        _record_contribution(spec, contributed, exposed)
        if _wire_visible(spec, exposed):
            tool_list.extend(contributed)

    ctx.has_base_tools = len(ctx.executable_tool_catalog) > 0

    # ── Capability phase ──
    for spec in specs:
        if spec.phase != 'capability' or not _enabled(spec):
            continue
        exposed = bool(
            spec.exposure_gate(ctx) if spec.exposure_gate else True)
        ctx.current_count = len(ctx.executable_tool_catalog)
        try:
            contributed = spec.build(ctx) or []
        except Exception as e:
            logger.error('[ToolRegistry] spec %s build failed: %s',
                         spec.key, e, exc_info=True)
            contributed = []
        contributed = _validated_contribution(spec, contributed)
        _record_contribution(spec, contributed, exposed)
        if _wire_visible(spec, exposed):
            tool_list.extend(contributed)

    return tool_list, ctx.has_base_tools
