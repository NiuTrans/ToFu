"""lib/tools/registry/_plugins.py — Third-party plugin discovery + allow-list.

Loads external ``tofu.tools`` entry points into the SAME process-global
``_TOOL_SPECS`` list as the built-ins (:func:`discover_plugin_specs`), stamping
each spec with its provenance so the per-request visibility gate can tell a
plugin's specs apart from built-ins. Also resolves the per-request plugin
allow-list from ``cfg['plugins']`` / ``TOFU_DEFAULT_TOOL_PLUGINS``
(:func:`resolve_enabled_plugins`).

Depends only on ``_spec`` (``ToolSpec`` / ``register_tool_spec`` /
``_TOOL_SPECS``). :func:`discover_plugin_specs` is invoked once at package
import (from ``lib/tools/registry/__init__.py``) after ``_register_builtins``,
mirroring the monolith's import-time side-effect order.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from typing import Any

from lib.log import get_logger

from lib.tools.registry._spec import (
    ToolSpec,
    _TOOL_SPECS,
    register_tool_spec,
)

logger = get_logger(__name__)


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
    except TypeError as e:
        # Python <3.10 returns a dict-like; filter by group key.
        logger.debug('[ToolRegistry] entry_points(group=) unsupported (%s) — '
                     'using Python <3.10 dict-like fallback', e)
        eps = entry_points().get('tofu.tools', [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[ToolRegistry] entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        ep_name = getattr(ep, 'name', '?')
        try:
            register_fn = ep.load()
            # Hand the plugin a wrapper that STAMPS provenance onto every spec
            # it registers, so we don't depend on the plugin author remembering
            # to set source/plugin_name. This is what makes the per-request
            # visibility gate (ToolContext.enabled_plugins) able to tell a
            # plugin's specs apart from built-ins — see the module "Plugin
            # isolation" note. ``replace`` is safe on the frozen dataclass.
            def _stamping_register(spec: ToolSpec, *, replace_existing: bool = False,
                                   _pname: str = ep_name, **_kw) -> None:
                # Accept the plugin author's ``replace=`` kwarg under either
                # name (back-compat) without colliding with dataclasses.replace.
                do_replace = replace_existing or bool(_kw.get('replace'))
                stamped = replace(spec, source='plugin', plugin_name=_pname)
                register_tool_spec(stamped, replace=do_replace)
            register_fn(_stamping_register)
            loaded += 1
            logger.info('[ToolRegistry] loaded plugin tool spec(s) from %s',
                        ep_name)
        except Exception as e:
            logger.warning('[ToolRegistry] plugin %s failed to load: %s',
                           ep_name, e, exc_info=True)
    return loaded


def available_plugins() -> dict[str, list[str]]:
    """Map each loaded plugin name → the spec keys it registered.

    Introspection helper for ops / docs / a future ``/api/v1/capabilities``
    surface: lets an operator see WHICH third-party plugins are installed in
    this process and therefore what a caller may name in ``config.plugins``.
    Built-in specs are excluded.
    """
    out: dict[str, list[str]] = {}
    for spec in _TOOL_SPECS:
        if spec.source == 'plugin' and spec.plugin_name:
            out.setdefault(spec.plugin_name, []).append(spec.key)
    return out


# ══════════════════════════════════════════════════════════
#  Per-request plugin allow-list resolution
# ══════════════════════════════════════════════════════════

_DEFAULT_PLUGINS_ENV = 'TOFU_DEFAULT_TOOL_PLUGINS'


def _parse_plugin_spec(value: Any) -> set[str] | None:
    """Normalise a raw plugins value into an allow-list set (or ``None``).

    Accepts:
      * ``'*'`` / ``['*']`` / ``'all'`` → ``None`` (gate fully open, ALL
        plugins visible).
      * a comma/space-separated string  → set of names.
      * a list/tuple/set of names       → set of names.
      * ``None`` / ``''`` / ``[]``      → empty set (NO plugins visible).

    The ``'*'`` sentinel maps to ``None`` because that is exactly the
    ``ToolContext.enabled_plugins`` value meaning "allow everything".
    """
    if value is None:
        return set()
    if isinstance(value, str):
        v = value.strip()
        if v in ('*', 'all'):
            return None
        if not v:
            return set()
        return {tok for tok in re.split(r'[,\s]+', v) if tok}
    if isinstance(value, (list, tuple, set)):
        items = {str(x).strip() for x in value if str(x).strip()}
        if '*' in items or 'all' in items:
            return None
        return items
    logger.debug('[ToolRegistry] ignoring unrecognised plugins value: %r', value)
    return set()


def resolve_enabled_plugins(cfg: dict[str, Any]) -> set[str] | None:
    """Resolve the per-request plugin allow-list for :class:`ToolContext`.

    Resolution order (first non-absent wins):

    1. ``cfg['plugins']`` — request-scoped. A headless caller sets this via
       ``config.plugins`` on ``/api/v1/agent/run`` (or any orchestrator cfg).
    2. ``TOFU_DEFAULT_TOOL_PLUGINS`` env var — deployment-wide default. A
       dedicated single-tenant install (e.g. liantong's ``app/`` copy) sets
       this once so it never has to pass ``plugins`` per request.
    3. Neither set → **fail-closed**: empty set → NO third-party plugins.

    Each level accepts the :func:`_parse_plugin_spec` vocabulary, including the
    ``'*'`` wildcard (→ ``None`` = all plugins visible).

    Returns:
        ``None`` (all plugins), ``set()`` (none), or a set of plugin names.
    """
    if 'plugins' in cfg:
        return _parse_plugin_spec(cfg.get('plugins'))
    env = os.environ.get(_DEFAULT_PLUGINS_ENV)
    if env is not None:
        return _parse_plugin_spec(env)
    return set()
