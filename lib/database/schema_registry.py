"""lib/database/schema_registry.py — Pluggable DB-domain schema initializers.

This is the **database-side mirror** of ``routes/plugin_registry.py`` (Blueprints),
``lib/tools/registry.py`` (tools) and ``lib/llm_dispatch/provider_registry.py``
(LLM body dialects).  Where those let a third-party package contribute *routes*,
*tools*, or *body dialects*, this registry lets one contribute a whole DB
**domain** — a set of tables created during ``init_db()``.

Why this exists
---------------
Historically ``init_db()`` called ``_init_trading_schema(conn)``
**unconditionally**, so a vanilla / trading-disabled install still created the
~12 ``trading_*`` tables.  That baked an optional feature's schema into core and
blocked extracting trading into its own package.

This registry adds the seam that removes that coupling.  A domain initializer is
a callable ``fn(conn)`` that issues idempotent ``CREATE TABLE IF NOT EXISTS`` /
``create_if_absent`` DDL for one feature area.  Core's always-on domains
(``chat``, ``system``) are NOT registered here — they remain hardcoded in
``init_db()``, exactly as built-in body dialects stay in the core ladders.  Only
*optional* domains register, either:

  * **in-tree (transition shim):** ``lib/database/_core.py`` registers the
    in-tree ``_init_trading_schema`` only when ``lib.TRADING_ENABLED`` is set;
  * **out-of-tree (the goal):** an external package declares::

        [project.entry-points."tofu.schema"]
        trading = "tofu_trading.schema:register"

    where ``register(register_schema_initializer)`` adds one or more domain
    initializers.

The set of registered domain names is also the **cache key** for the
fast-startup version check: when it changes (e.g. trading is enabled on a server
that booted without it), ``init_db()`` re-runs DDL even if ``_SCHEMA_VERSION``
is unchanged — otherwise a newly-enabled domain's tables would be silently
skipped by the version fast-path.

Design contract
---------------
1. **Optional domains only.**  ``chat`` / ``system`` are never registered here.
2. **Idempotent initializers.**  Every ``fn(conn)`` MUST be safe to run on an
   existing DB (guard with ``IF NOT EXISTS`` / ``create_if_absent``); the
   fast-path can re-trigger it, and core never tracks per-domain versions.
3. **Fail-soft discovery.**  A plugin that errors on load is logged and skipped;
   a vanilla install with no plugins simply has an empty optional-domain set.
"""

from __future__ import annotations

from collections.abc import Callable

from lib.log import get_logger

logger = get_logger(__name__)

# Entry-point group external packages publish their schema registrars under.
_ENTRY_POINT_GROUP = 'tofu.schema'

# domain name -> initializer callable fn(conn). Optional domains ONLY;
# core's chat/system domains stay hardcoded in init_db().
_INITIALIZERS: dict[str, Callable] = {}


def register_schema_initializer(domain: str, fn: Callable, *, replace: bool = False) -> None:
    """Register an optional DB-domain schema initializer.

    Args:
        domain: Short domain name (e.g. ``'trading'``).  Used as the
            fast-startup cache key — must be stable across boots.
        fn: ``fn(conn)`` issuing idempotent DDL for this domain.  Runs inside
            ``init_db()``'s connection/transaction.
        replace: If True, overwrite an existing initializer for ``domain``.
    """
    if not domain or not isinstance(domain, str):
        logger.warning('[SchemaRegistry] initializer with empty/invalid domain '
                       'ignored: %r', domain)
        return
    if domain in _INITIALIZERS and not replace:
        logger.warning('[SchemaRegistry] duplicate domain %r ignored '
                       '(pass replace=True to override)', domain)
        return
    _INITIALIZERS[domain] = fn
    logger.info('[SchemaRegistry] registered schema initializer for domain=%r', domain)


def unregister_schema_initializer(domain: str) -> None:
    """Remove a registered initializer (mainly for tests)."""
    _INITIALIZERS.pop(domain, None)


def active_domains() -> list[str]:
    """Return the sorted list of registered optional-domain names.

    This is the fast-startup cache key: ``init_db()`` stores it alongside
    ``_schema_version`` and forces a DDL re-run when it changes, so newly
    enabled domains never get skipped by the version fast-path.
    """
    return sorted(_INITIALIZERS)


def run_registered(conn) -> None:
    """Run every registered optional-domain initializer against ``conn``.

    Called by ``init_db()`` after the always-on chat/system schemas.  An
    initializer that raises is allowed to propagate — a failed CREATE during
    bootstrap is fatal and must not be swallowed (CLAUDE.md §2).
    """
    for domain in active_domains():
        fn = _INITIALIZERS[domain]
        fn(conn)
        logger.info('[DB] %s schema initialized (registered domain)', domain)


def discover_schema_plugins() -> int:
    """Load optional-domain initializers from the ``tofu.schema`` entry-point group.

    A plugin package declares in its ``pyproject.toml``::

        [project.entry-points."tofu.schema"]
        trading = "tofu_trading.schema:register"

    where ``register`` receives :func:`register_schema_initializer` and uses it
    to add one or more domain initializers.  Failures in any single plugin are
    logged and skipped.

    Returns:
        The number of entry points successfully loaded.
    """
    loaded = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover — stdlib always present on 3.8+
        logger.debug('[SchemaRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[SchemaRegistry] entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        name = getattr(ep, 'name', '?')
        try:
            register_fn = ep.load()
            register_fn(register_schema_initializer)
            loaded += 1
            logger.info('[SchemaRegistry] loaded schema initializer(s) from plugin %r', name)
        except Exception as e:
            logger.warning('[SchemaRegistry] plugin %r failed to load: %s',
                           name, e, exc_info=True)
    return loaded


__all__ = [
    'register_schema_initializer',
    'unregister_schema_initializer',
    'active_domains',
    'run_registered',
    'discover_schema_plugins',
]
