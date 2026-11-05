"""Discover optional application contributions without importing HTTP routes.

Entry points: blueprint and TaskRuntime discovery, paired startup/shutdown
hooks. Dependencies: importlib.metadata and logging; plugin code loads only
when a caller requests its group. Plugins own feature admission. One broken
contribution is logged and skipped without suppressing neighboring plugins.

The HTTP assembly mounts returned blueprints; the process lifecycle invokes
hooks. Task runtimes are cached per metadata loader because installing a
plugin requires restarting the process, while embedders/tests can replace the
loader explicitly. Callers receive fresh lists so they cannot mutate the cache.
"""

from __future__ import annotations

import threading

from lib.log import get_logger

logger = get_logger(__name__)

_ENTRY_POINT_GROUP = 'tofu.blueprints'
_STARTUP_GROUP = 'tofu.startup'
_SHUTDOWN_GROUP = 'tofu.shutdown'
_TASK_RUNTIME_GROUP = 'tofu.task_runtimes'

_TASK_RUNTIME_CACHE: tuple | None = None
_TASK_RUNTIME_CACHE_LOADER = None
_TASK_RUNTIME_CACHE_LOCK = threading.RLock()


def _plugin_entries(group, loader):
    """Return entries, or None on lookup failure so failed reads aren't cached."""
    try:
        try:
            return loader(group=group)
        except TypeError:
            return loader().get(group, [])
    except Exception as exc:
        logger.debug('[PluginRegistry] %s lookup failed: %s', group, exc)
        return None


def discover_blueprint_plugins() -> list:
    """Collect native blueprints from zero-argument ``tofu.blueprints`` hooks."""
    from importlib.metadata import entry_points

    blueprints = []
    for entry in _plugin_entries(_ENTRY_POINT_GROUP, entry_points) or ():
        name = getattr(entry, 'name', '?')
        try:
            contributed = entry.load()() or []
            blueprints.extend(contributed)
            logger.info('[PluginRegistry] loaded %d blueprint(s) from %r',
                        len(contributed), name)
        except Exception as exc:
            logger.warning('[PluginRegistry] blueprint plugin %r failed: %s',
                           name, exc, exc_info=True)
    return blueprints


def _run_hooks(app, group: str) -> int:
    from importlib.metadata import entry_points

    ran = 0
    for entry in _plugin_entries(group, entry_points) or ():
        name = getattr(entry, 'name', '?')
        try:
            entry.load()(app)
            ran += 1
            logger.info('[PluginRegistry] ran %s hook from %r', group, name)
        except Exception as exc:
            logger.warning('[PluginRegistry] %s hook %r failed: %s',
                           group, name, exc, exc_info=True)
    return ran


def run_startup_hooks(app) -> int:
    """Invoke ``tofu.startup`` hooks once the process owner starts services."""
    return _run_hooks(app, _STARTUP_GROUP)


def run_shutdown_hooks(app) -> int:
    """Invoke fail-soft ``tofu.shutdown`` hooks before stopping core workers."""
    return _run_hooks(app, _SHUTDOWN_GROUP)


def discover_task_runtime_plugins() -> list:
    """Collect one TaskRuntime or a sequence from ``tofu.task_runtimes`` hooks."""
    from importlib.metadata import entry_points

    global _TASK_RUNTIME_CACHE, _TASK_RUNTIME_CACHE_LOADER
    with _TASK_RUNTIME_CACHE_LOCK:
        if (_TASK_RUNTIME_CACHE is not None
                and _TASK_RUNTIME_CACHE_LOADER is entry_points):
            return list(_TASK_RUNTIME_CACHE)
        entries = _plugin_entries(_TASK_RUNTIME_GROUP, entry_points)
        if entries is None:
            return []
        runtimes = []
        for entry in entries:
            name = getattr(entry, 'name', '?')
            try:
                result = entry.load()()
                if result is None:
                    continue
                if isinstance(result, (list, tuple)):
                    runtimes.extend(result)
                else:
                    runtimes.append(result)
                logger.info('[PluginRegistry] loaded task runtime(s) from %r', name)
            except Exception as exc:
                logger.warning('[PluginRegistry] task-runtime plugin %r failed: %s',
                               name, exc, exc_info=True)
        _TASK_RUNTIME_CACHE = tuple(runtimes)
        _TASK_RUNTIME_CACHE_LOADER = entry_points
        return list(_TASK_RUNTIME_CACHE)


__all__ = [
    'discover_blueprint_plugins', 'discover_task_runtime_plugins',
    'run_shutdown_hooks', 'run_startup_hooks',
]
