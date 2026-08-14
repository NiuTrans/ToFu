"""routes/plugin_registry.py — Pluggable Flask/Quart Blueprint discovery.

This is the **route-side mirror** of ``lib/tools/registry.py`` (tools) and
``lib/llm_dispatch/provider_registry.py`` (LLM body dialects).  Where those let
a third-party package contribute *tools* or *body dialects*, this registry lets
one contribute whole *Blueprints* — a self-contained feature surface (routes +
their side-effect handler registrations) that mounts onto the app at startup.

Why this exists
---------------
Historically, optional feature bundles (the trading subsystem) were wired into
``routes/__init__.py`` by name: a hardcoded ``if TRADING_ENABLED:`` block that
imported ``routes.trading_*`` and ``routes.api_v1.trading.*`` directly.  That
made the core repo aware of an optional feature and blocked extracting it into
its own package.

This registry adds the seam that removes that coupling: an external package
declares in its ``pyproject.toml``::

    [project.entry-points."tofu.blueprints"]
    trading = "tofu_trading.routes:register"

where ``register()`` returns a list of Blueprint objects to mount.  Importing
the plugin's route modules (which ``register`` does) triggers their handler
decorators, exactly as the in-tree side-effect imports did.

Design contract
---------------
1. **Additive & fail-soft.**  Discovery NEVER raises into ``register_all``.  A
   plugin that errors is logged at WARNING and skipped; the rest of the app
   boots normally.
2. **Plugin owns its gate.**  Whether a plugin's routes should mount (a feature
   flag, a license check, …) is the plugin's decision: ``register()`` returns
   an empty list to opt out.  Core does not second-guess it.
3. **Returns Blueprint objects only.**  ``register_all`` is the single place
   that calls ``app.register_blueprint`` — the registry just collects them.
"""

from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)

# Entry-point group external packages publish their Blueprint registrars under.
_ENTRY_POINT_GROUP = 'tofu.blueprints'

# Entry-point group for post-registration startup hooks (background workers,
# schedulers). Each loads to a callable ``hook(app)`` run once during
# ``register_all`` after all blueprints are mounted.
_STARTUP_GROUP = 'tofu.startup'

# Optional paired teardown hooks. A plugin that owns background workers should
# publish ``hook(app)`` here and perform bounded, idempotent cleanup.
_SHUTDOWN_GROUP = 'tofu.shutdown'

# Entry-point group for plugin TaskRuntime instances, so the generic
# /api/v1/tasks lifecycle endpoints can discover a plugin's background-task
# kinds (e.g. trading-sim) without core naming them. Each loads to a callable
# returning a TaskRuntime or a list of them.
_TASK_RUNTIME_GROUP = 'tofu.task_runtimes'

# ``importlib.metadata.entry_points()`` scans every installed distribution.
# On the production environment this costs ~0.18 s even after warm-up and the
# generic ``GET /api/v1/tasks`` path called it on EVERY request.  Plugins cannot
# appear inside a running Python process (installation already requires a
# server restart), so resolve task runtimes once per entry_points loader.
# Keying the cache by the loader object also keeps monkeypatched tests and
# embedders honest: replacing importlib.metadata.entry_points automatically
# forces a fresh discovery rather than returning a stale test fixture.
_TASK_RUNTIME_CACHE: tuple | None = None
_TASK_RUNTIME_CACHE_LOADER = None
_TASK_RUNTIME_CACHE_LOCK = threading.RLock()


def discover_blueprint_plugins() -> list:
    """Load Blueprints contributed via the ``tofu.blueprints`` entry-point group.

    Each entry point loads to a zero-arg callable ``register() -> list``.  The
    returned Blueprint objects are collected and returned as a flat list for
    ``register_all`` to mount.  Failures in any single plugin are logged and
    skipped so one broken plugin never takes down the whole app.

    Returns:
        A flat list of Blueprint objects (possibly empty).
    """
    blueprints: list = []
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover — stdlib always present on 3.8+
        logger.debug('[BlueprintRegistry] importlib.metadata unavailable: %s', e)
        return blueprints

    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        # Python <3.10: entry_points() returns a dict keyed by group.
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[BlueprintRegistry] entry_points lookup failed: %s', e)
        return blueprints

    for ep in eps:
        name = getattr(ep, 'name', '?')
        try:
            register_fn = ep.load()
            bps = register_fn() or []
            blueprints.extend(bps)
            logger.info('[BlueprintRegistry] loaded %d blueprint(s) from plugin %r',
                        len(bps), name)
        except Exception as e:
            logger.warning('[BlueprintRegistry] plugin %r failed to load: %s',
                           name, e, exc_info=True)
    return blueprints


def run_startup_hooks(app) -> int:
    """Run post-registration startup hooks from the ``tofu.startup`` group.

    A plugin package declares in its ``pyproject.toml``::

        [project.entry-points."tofu.startup"]
        trading = "tofu_trading.web:start_workers"

    where the loaded callable takes the Flask/Quart ``app`` and launches any
    background threads / schedulers the feature needs.  This replaces the
    former hardcoded ``server.py`` block that imported ``routes.trading_*`` to
    start the intel + autopilot workers.

    Failures in any single hook are logged and skipped so one broken plugin
    never takes down startup.

    Returns:
        The number of hooks successfully run.
    """
    ran = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover
        logger.debug('[BlueprintRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group=_STARTUP_GROUP)
    except TypeError:
        eps = entry_points().get(_STARTUP_GROUP, [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[BlueprintRegistry] startup entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        name = getattr(ep, 'name', '?')
        try:
            hook = ep.load()
            hook(app)
            ran += 1
            logger.info('[BlueprintRegistry] ran startup hook from plugin %r', name)
        except Exception as e:
            logger.warning('[BlueprintRegistry] startup hook %r failed: %s',
                           name, e, exc_info=True)
    return ran


def run_shutdown_hooks(app) -> int:
    """Run fail-soft plugin teardown hooks during Quart's shutdown lifespan."""
    ran = 0
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover
        logger.debug('[BlueprintRegistry] importlib.metadata unavailable: %s', e)
        return 0
    try:
        eps = entry_points(group=_SHUTDOWN_GROUP)
    except TypeError as exc:
        logger.debug(
            '[BlueprintRegistry] using legacy shutdown entry_points API: %s',
            exc)
        eps = entry_points().get(_SHUTDOWN_GROUP, [])  # type: ignore[attr-defined]
    except Exception as e:
        logger.debug('[BlueprintRegistry] shutdown entry_points lookup failed: %s', e)
        return 0
    for ep in eps:
        name = getattr(ep, 'name', '?')
        try:
            hook = ep.load()
            hook(app)
            ran += 1
            logger.info(
                '[BlueprintRegistry] ran shutdown hook from plugin %r', name)
        except Exception as e:
            logger.warning(
                '[BlueprintRegistry] shutdown hook %r failed: %s',
                name, e, exc_info=True)
    return ran


def discover_task_runtime_plugins() -> list:
    """Load plugin ``TaskRuntime`` instances from ``tofu.task_runtimes``.

    A plugin declares in its ``pyproject.toml``::

        [project.entry-points."tofu.task_runtimes"]
        trading = "tofu_trading.web:get_task_runtimes"

    where the loaded callable returns a ``TaskRuntime`` or a list of them.
    Used by ``routes/api_v1/tasks.py::_registries`` so the generic task
    endpoints surface plugin task kinds. Fail-soft: a broken plugin is logged
    and skipped.

    Returns:
        A flat list of TaskRuntime instances (possibly empty).
    """
    runtimes: list = []
    try:
        from importlib.metadata import entry_points
    except Exception as e:  # pragma: no cover
        logger.debug('[BlueprintRegistry] importlib.metadata unavailable: %s', e)
        return runtimes
    global _TASK_RUNTIME_CACHE, _TASK_RUNTIME_CACHE_LOADER
    with _TASK_RUNTIME_CACHE_LOCK:
        if (_TASK_RUNTIME_CACHE is not None
                and _TASK_RUNTIME_CACHE_LOADER is entry_points):
            # Never expose the cached list itself: callers historically owned
            # and could mutate their result without poisoning later requests.
            return list(_TASK_RUNTIME_CACHE)
        try:
            eps = entry_points(group=_TASK_RUNTIME_GROUP)
        except TypeError:
            eps = entry_points().get(_TASK_RUNTIME_GROUP, [])  # type: ignore[attr-defined]
        except Exception as e:
            logger.debug('[BlueprintRegistry] task-runtime entry_points lookup failed: %s', e)
            return runtimes
        for ep in eps:
            name = getattr(ep, 'name', '?')
            try:
                fn = ep.load()
                result = fn()
                if result is None:
                    continue
                if isinstance(result, (list, tuple)):
                    runtimes.extend(result)
                else:
                    runtimes.append(result)
                logger.info('[BlueprintRegistry] loaded task runtime(s) from plugin %r', name)
            except Exception as e:
                logger.warning('[BlueprintRegistry] task-runtime plugin %r failed: %s',
                               name, e, exc_info=True)
        _TASK_RUNTIME_CACHE = tuple(runtimes)
        _TASK_RUNTIME_CACHE_LOADER = entry_points
        return list(_TASK_RUNTIME_CACHE)


__all__ = [
    'discover_blueprint_plugins', 'run_shutdown_hooks', 'run_startup_hooks',
    'discover_task_runtime_plugins',
]
