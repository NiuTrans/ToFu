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

    # ── Registration ──────────────────────────────────────

    def register(self, names, handler: ToolHandler, *, category: str = '', description: str = ''):
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
        """
        if isinstance(names, str):
            names = {names}
        for name in names:
            self._exact[name] = handler
            self._metadata[name] = {'category': category, 'description': description}

    def register_set(self, name_set, handler: ToolHandler, *, category: str = '', description: str = ''):
        """Register *handler* for a set of tool names (checked in order).

        Unlike ``register()``, set-based entries are checked sequentially
        after exact matches, preserving priority ordering.
        """
        self._sets.append((frozenset(name_set), handler))
        meta = {'category': category, 'description': description}
        for name in name_set:
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
