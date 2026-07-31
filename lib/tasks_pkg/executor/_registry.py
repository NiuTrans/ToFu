# HOT_PATH
"""ToolRegistry — formal registry pattern for tool dispatch + the module-level
``tool_registry`` singleton.

Split out of the original ``executor.py`` (facade-preserving package). The
``ToolRegistry`` class is kept WHOLE in this single submodule; consumers reach
it via ``from lib.tasks_pkg.executor import ToolRegistry, tool_registry``.
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.protocols import ToolHandler

logger = get_logger(__name__)

# NOTE: Do NOT re-export _lib.FETCH_* as module-level copies here.
# Module-level copies become stale after reload_config() — always read
# from _lib.<VAR> at call time to pick up hot-reloaded values.

# ══════════════════════════════════════════════════════════
#  ToolRegistry — formal registry pattern for tool dispatch
# ══════════════════════════════════════════════════════════

class ToolRegistry:
    """Central registry for tool handlers with metadata.

    Supports three registration modes:
    - **exact**: a single tool name → handler (fastest lookup).
    - **set-based**: a ``frozenset`` of tool names → handler (checked in order).
    - **special**: a key like ``'__code_exec__'`` matched via ``round_entry``
      rather than ``fn_name``.

    All registration methods have corresponding decorator forms to
    co-locate handler definitions with their registration::

        registry = ToolRegistry()

        @registry.handler('web_search', category='search',
                          description='Perform a web search via API')
        def _handle_web_search(task, tc, fn_name, ...):
            ...

        @registry.tool_set(BROWSER_TOOL_NAMES, category='browser',
                           description='Execute a browser automation tool')
        def _handle_browser_tool(task, tc, fn_name, ...):
            ...

        @registry.special('__code_exec__', category='code',
                           description='Execute a shell command')
        def _handle_code_exec(task, tc, fn_name, ...):
            ...

        # Lookup at dispatch time
        handler = registry.lookup(fn_name, round_entry)
    """

    def __init__(self) -> None:
        self._exact: dict[str, ToolHandler] = {}          # name → handler
        self._sets: list[tuple[frozenset, ToolHandler]] = []  # (name_set, handler)
        self._metadata: dict[str, dict[str, str]] = {}    # name → {category, description}
        self._special: dict[str, ToolHandler] = {}        # key → handler (e.g. __code_exec__)
        # name → (source, plugin_name) — who owns this tool name. Drives the
        # built-in hijack protection in :meth:`_claim_name`.
        self._provenance: dict[str, tuple[str, str]] = {}

    # ── State snapshot / restore (test isolation) ─────────
    #
    # The registry is a process-global singleton, so any test that registers
    # a spec mutates state every later test sees. Cleaning up by hand means
    # listing the state tables at the call site — and that list silently goes
    # stale the moment a new table is added. That already bit us twice:
    # ``test_tool_registry``'s per-test ``_cleanup`` dropped the ToolSpec but
    # never unbound the handler (leaking ``_hsync_tool_a`` /
    # ``__hsync_special__`` into the SSOT coverage ratchet), and the first
    # version of the hijack guard's fixture restored four tables while
    # ``_provenance`` — added in the same commit — kept a stale claim that
    # would silently REFUSE a later legitimate registration.
    #
    # These two methods are therefore INTROSPECTIVE: they enumerate the
    # instance's own ``__dict__`` rather than a hand-written table list, so a
    # sixth state table is covered the day it is added, with no edit here and
    # none at any call site.

    def snapshot(self) -> dict[str, Any]:
        """Deep-ish copy of every state table, for later :meth:`restore`.

        Containers are copied one level (the handlers inside are immutable
        function references, so a shallow copy per container is exact).
        """
        snap: dict[str, Any] = {}
        for attr, value in self.__dict__.items():
            if isinstance(value, dict):
                snap[attr] = dict(value)
            elif isinstance(value, list):
                snap[attr] = list(value)
            elif isinstance(value, set):
                snap[attr] = set(value)
            else:
                snap[attr] = value
        return snap

    def restore(self, snap: dict[str, Any]) -> None:
        """Restore state captured by :meth:`snapshot`, in place.

        In-place mutation (rather than rebinding ``self.__dict__``) keeps any
        module that captured a direct reference to a table — e.g. a test
        holding ``registry._exact`` — pointing at the live object.
        """
        for attr, saved in snap.items():
            current = getattr(self, attr, None)
            if isinstance(current, dict) and isinstance(saved, dict):
                current.clear()
                current.update(saved)
            elif isinstance(current, list) and isinstance(saved, list):
                current[:] = saved
            elif isinstance(current, set) and isinstance(saved, set):
                current.clear()
                current.update(saved)
            else:
                setattr(self, attr, saved)
        # A table created AFTER the snapshot was taken is not in ``snap``;
        # clearing it is the only way "restore" can mean what it says.
        for attr, value in list(self.__dict__.items()):
            if attr in snap:
                continue
            if isinstance(value, (dict, list, set)):
                value.clear()

    # ── Name provenance + built-in hijack protection ──────

    def _claim_name(self, name: str, source: str, plugin_name: str) -> bool:
        """Decide whether *source* may bind *name*; record the owner.

        **Built-ins always beat plugins, regardless of arrival order.** A
        "first writer wins" rule would be wrong in both directions here:
        ``discover_plugin_specs()`` runs while ``lib.tools.registry`` is
        imported, i.e. BEFORE the built-in ``@tool_registry`` decorators in
        ``lib.tasks_pkg.handlers`` have run. So at real startup a plugin binds
        first and the built-in binds second — ordering carries no authority.

        Two distinct hijack shapes are covered, which is why this cannot live
        in :meth:`register` as a dict-overwrite check:

        * **overwrite** — the name is already in ``_exact`` (7 names today) and
          gets replaced.
        * **shadow** — the name lives in a ``_sets`` entry (83 names today,
          including ``run_command`` / ``write_file`` / ``apply_diff``). A
          plugin registering it lands in ``_exact``, and since
          :meth:`lookup` consults ``_exact`` FIRST, the built-in set entry is
          silently shadowed while remaining physically present.

        Returns True when the caller may bind the name.
        """
        prev = self._provenance.get(name)
        if prev is None:
            self._provenance[name] = (source, plugin_name)
            return True
        prev_source, prev_plugin = prev

        if source == 'builtin':
            if prev_source == 'plugin':
                # Plugin got there first (the normal startup order). Evict its
                # _exact entry too, or it would keep shadowing a built-in that
                # resolves through _sets.
                self._exact.pop(name, None)
                logger.warning(
                    '[ToolRegistry] built-in tool %r reclaimed from plugin %r '
                    '— a plugin may not provide a core tool name; its handler '
                    'has been evicted', name, prev_plugin or '?')
            self._provenance[name] = (source, plugin_name)
            return True

        # source == 'plugin'
        if prev_source == 'builtin':
            logger.warning(
                '[ToolRegistry] plugin %r REFUSED tool name %r — that is a '
                'built-in tool. Binding it would run third-party code while '
                'inheriting the built-in write partition and approval prompt '
                '(the user would see the familiar dialog for a different '
                'callable). Rename the plugin tool.',
                plugin_name or '?', name)
            return False
        if prev_plugin != plugin_name:
            logger.warning(
                '[ToolRegistry] plugin %r REFUSED tool name %r — already '
                'provided by plugin %r', plugin_name or '?', name,
                prev_plugin or '?')
            return False
        # Same plugin re-syncing its own name: idempotent, stay silent.
        return True

    # ── Registration ──────────────────────────────────────

    def register(self, names, handler: ToolHandler, *, category: str = '', description: str = '',
                 source: str = 'builtin', plugin_name: str = ''):
        """Register *handler* for one or more exact tool names.

        Parameters
        ----------
        names : str | set | frozenset | list
            Tool name(s) to register.
        handler : ToolHandler
            Handler function satisfying the :class:`~lib.protocols.ToolHandler` protocol.
        category : str
            Logical grouping (e.g. ``'search'``, ``'browser'``).
        description : str
            Human-readable description of what the handler does.
        source : str
            ``'builtin'`` (core) or ``'plugin'`` (third-party ``tofu.tools``
            entry point). A plugin may not take over a built-in tool name —
            see :meth:`_claim_name`.
        plugin_name : str
            Entry-point name when ``source='plugin'``; used in log lines.
        """
        if isinstance(names, str):
            names = {names}
        for name in names:
            if not self._claim_name(name, source, plugin_name):
                continue
            self._exact[name] = handler
            self._metadata[name] = {'category': category, 'description': description}

    def register_set(self, name_set, handler: ToolHandler, *, category: str = '', description: str = '',
                     source: str = 'builtin', plugin_name: str = ''):
        """Register *handler* for a set of tool names (checked in order).

        Unlike ``register()``, set-based entries are checked sequentially
        after exact matches, preserving priority ordering.
        """
        allowed = frozenset(
            n for n in name_set if self._claim_name(n, source, plugin_name))
        if not allowed:
            return
        self._sets.append((allowed, handler))
        meta = {'category': category, 'description': description}
        for name in allowed:
            self._metadata.setdefault(name, meta)

    def register_special(self, key: str, handler: ToolHandler, *, category: str = '', description: str = ''):
        """Register a handler for a special dispatch key (e.g. ``'__code_exec__'``).

        Special handlers are matched via ``round_entry`` metadata rather
        than ``fn_name`` directly.
        """
        self._special[key] = handler
        self._metadata[key] = {'category': category, 'description': description}

    def handler(self, names, *, category='', description=''):
        """Decorator form of :meth:`register`.

        Example::

            @registry.handler('web_search', category='search',
                              description='Web search via API')
            def _handle_web_search(task, tc, fn_name, ...):
                ...
        """
        def decorator(fn):
            self.register(names, fn, category=category, description=description)
            return fn
        return decorator

    def tool(self, name: str, *, category: str = '', description: str = ''):
        """Decorator form of :meth:`register` for a single tool name.

        Example::

            @registry.tool('web_search', category='search',
                           description='Perform a web search via API')
            def _handle_web_search(task, tc, fn_name, ...):
                ...

        This is equivalent to calling ``registry.register(name, fn, ...)``
        after the function definition.
        """
        def decorator(fn):
            self.register(name, fn, category=category, description=description)
            return fn
        return decorator

    def special(self, key: str, *, category: str = '', description: str = ''):
        """Decorator form of :meth:`register_special`.

        Example::

            @registry.special('__code_exec__', category='code',
                               description='Execute a shell command')
            def _handle_code_exec(task, tc, fn_name, ...):
                ...

        This is equivalent to calling ``registry.register_special(key, fn, ...)``
        after the function definition.
        """
        def decorator(fn):
            self.register_special(key, fn, category=category, description=description)
            return fn
        return decorator

    def tool_set(self, name_set, *, category: str = '', description: str = ''):
        """Decorator form of :meth:`register_set`.

        Co-locates the registration with the handler definition, eliminating
        the need for a separate imperative ``register_set()`` call.

        Example::

            @registry.tool_set(BROWSER_TOOL_NAMES, category='browser',
                               description='Execute a browser automation tool')
            def _handle_browser_tool(task, tc, fn_name, ...):
                ...

        This is equivalent to calling ``registry.register_set(name_set, fn, ...)``
        after the function definition.
        """
        def decorator(fn):
            self.register_set(name_set, fn, category=category, description=description)
            return fn
        return decorator

    # ── Lookup ────────────────────────────────────────────

    def lookup(self, fn_name: str, round_entry: dict[str, Any] | None = None) -> ToolHandler | None:
        """Find the handler for *fn_name*.

        Lookup order:
        1. Exact-name match (O(1) dict lookup).
        2. Special ``code_exec`` check via ``round_entry['toolName']``.
        3. Set-based match (linear scan, first match wins).
        4. ``None`` if no handler found.
        """
        # 1. Exact
        h = self._exact.get(fn_name)
        if h is not None:
            return h

        # 2. Special: code_exec identified by round_entry, not fn_name
        if round_entry and round_entry.get('toolName') == 'code_exec':
            h = self._special.get('__code_exec__')
            if h is not None:
                return h

        # 3. Set-based
        for name_set, handler in self._sets:
            if fn_name in name_set:
                return handler

        return None

    # ── Introspection ─────────────────────────────────────

    def list_tools(self):
        """Return a list of ``(name, category, description)`` for all registered tools."""
        seen = set()
        result = []
        # Exact registrations first
        for name in self._exact:
            if name not in seen:
                meta = self._metadata.get(name, {})
                result.append((name, meta.get('category', ''), meta.get('description', '')))
                seen.add(name)
        # Special registrations
        for key in self._special:
            if key not in seen:
                meta = self._metadata.get(key, {})
                result.append((key, meta.get('category', ''), meta.get('description', '')))
                seen.add(key)
        # Set-based registrations
        for name_set, _ in self._sets:
            for name in sorted(name_set):
                if name not in seen:
                    meta = self._metadata.get(name, {})
                    result.append((name, meta.get('category', ''), meta.get('description', '')))
                    seen.add(name)
        return result

    def __contains__(self, fn_name):
        """Support ``fn_name in registry`` syntax."""
        return self.lookup(fn_name) is not None

    def __repr__(self):
        n_exact = len(self._exact)
        n_sets = sum(len(s) for s, _ in self._sets)
        n_special = len(self._special)
        return f'<ToolRegistry exact={n_exact} set_names={n_sets} special={n_special}>'


# Module-level singleton — all tool handlers register here.
tool_registry = ToolRegistry()
