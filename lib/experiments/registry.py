"""Typed experiment provider registry and ``tofu.experiments`` discovery.

Plugins contribute strategies, metrics, and analyzers as one atomic bundle.
Registration returns a disposer so tests and future reload owners cannot leave
half-mounted capabilities behind.  Discovery is optional and fail-soft; a
resolved experiment reference is strict and fails before activation.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from lib.log import get_logger


logger = get_logger(__name__)
ENTRY_POINT_GROUP = "tofu.experiments"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,63}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class ExperimentPluginError(RuntimeError):
    """Raised for ambiguous, missing, or invalid experiment capabilities."""


StrategyResolver = Callable[[Mapping[str, Any]], Mapping[str, Any]]
StrategyConflict = Callable[[Mapping[str, Any]], str | None]
StrategyApply = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]
MetricExtract = Callable[[Mapping[str, Any]], float | bool | None]
AnalyzerRun = Callable[[Mapping[str, Any]], dict[str, Any]]


def implementation_digest(path: str | Path) -> str:
    """Fingerprint one provider source file for immutable experiment specs."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _provider_identity(plugin_id: str, capability_id: str, version: str,
                       digest: str) -> tuple[str, str, str, str]:
    if not _IDENTIFIER.fullmatch(plugin_id):
        raise ExperimentPluginError("invalid experiment plugin_id")
    if not _IDENTIFIER.fullmatch(capability_id):
        raise ExperimentPluginError("invalid experiment capability id")
    if not _VERSION.fullmatch(version):
        raise ExperimentPluginError("invalid experiment plugin version")
    if not _DIGEST.fullmatch(digest):
        raise ExperimentPluginError("implementation_digest must be SHA-256")
    return plugin_id, capability_id, version, digest


@dataclass(frozen=True, slots=True)
class StrategyProvider:
    """Trusted request-policy implementation contributed by one plugin."""

    plugin_id: str
    strategy_id: str
    version: str
    implementation_digest: str
    description: str
    config_schema: Mapping[str, Any]
    resolve: StrategyResolver
    conflict: StrategyConflict
    apply: StrategyApply

    def __post_init__(self) -> None:
        _provider_identity(
            self.plugin_id, self.strategy_id, self.version,
            self.implementation_digest,
        )
        if not callable(self.resolve) or not callable(self.conflict) \
                or not callable(self.apply):
            raise ExperimentPluginError("strategy callbacks must be callable")

    def resolve_config(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        """Resolve and detach one strategy configuration."""
        if not isinstance(raw, Mapping):
            raise ExperimentPluginError("strategy config must be an object")
        value = self.resolve(raw)
        if not isinstance(value, Mapping):
            raise ExperimentPluginError("strategy resolver must return an object")
        from .contracts import canonical_json

        import json

        return json.loads(canonical_json(value))


@dataclass(frozen=True, slots=True)
class MetricProvider:
    """Outcome metric definition with one pure extractor."""

    plugin_id: str
    metric_id: str
    version: str
    implementation_digest: str
    description: str
    unit: str
    direction: str
    extract: MetricExtract

    def __post_init__(self) -> None:
        _provider_identity(
            self.plugin_id, self.metric_id, self.version,
            self.implementation_digest,
        )
        if self.direction not in {"increase", "decrease", "guardrail"}:
            raise ExperimentPluginError("metric direction is unsupported")
        if not self.unit or not callable(self.extract):
            raise ExperimentPluginError("metric unit and extractor are required")


@dataclass(frozen=True, slots=True)
class AnalyzerProvider:
    """Pure report analyzer consuming assignment-unit observations."""

    plugin_id: str
    analyzer_id: str
    version: str
    implementation_digest: str
    description: str
    analyze: AnalyzerRun

    def __post_init__(self) -> None:
        _provider_identity(
            self.plugin_id, self.analyzer_id, self.version,
            self.implementation_digest,
        )
        if not callable(self.analyze):
            raise ExperimentPluginError("analyzer callback must be callable")


@dataclass(frozen=True, slots=True)
class ExperimentPlugin:
    """Atomic capability bundle published by one install-time plugin."""

    plugin_id: str
    version: str
    description: str = ""
    strategies: tuple[StrategyProvider, ...] = ()
    metrics: tuple[MetricProvider, ...] = ()
    analyzers: tuple[AnalyzerProvider, ...] = ()

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.plugin_id):
            raise ExperimentPluginError("invalid experiment plugin id")
        if not _VERSION.fullmatch(self.version):
            raise ExperimentPluginError("invalid experiment plugin version")
        if not (self.strategies or self.metrics or self.analyzers):
            raise ExperimentPluginError("experiment plugin contributes no capability")
        for provider in (*self.strategies, *self.metrics, *self.analyzers):
            if provider.plugin_id != self.plugin_id or provider.version != self.version:
                raise ExperimentPluginError(
                    "bundle and provider plugin identity must match"
                )


class ExperimentRegistry:
    """Thread-safe, version-aware registry for trusted capabilities.

    Multiple versions of one plugin may remain mounted so a persisted resolved
    specification can still be replayed after a newer implementation ships.
    Authored specifications may omit a version only while exactly one matching
    provider exists; ambiguity is rejected instead of silently picking latest.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plugins: dict[tuple[str, str], ExperimentPlugin] = {}
        self._strategies: dict[tuple[str, str, str], StrategyProvider] = {}
        self._metrics: dict[tuple[str, str, str], MetricProvider] = {}
        self._analyzers: dict[tuple[str, str, str], AnalyzerProvider] = {}
        self._generation = 0

    @property
    def generation(self) -> int:
        """Monotonic capability-set revision for compiled runtime caches."""
        with self._lock:
            return self._generation

    def register(self, plugin: ExperimentPlugin) -> Callable[[], None]:
        """Register one complete bundle or raise before mutating the registry."""
        if not isinstance(plugin, ExperimentPlugin):
            raise ExperimentPluginError("experiment entry point returned no plugin")
        plugin_key = (plugin.plugin_id, plugin.version)
        strategy_keys = [(item.plugin_id, item.strategy_id, item.version)
                         for item in plugin.strategies]
        metric_keys = [(item.plugin_id, item.metric_id, item.version)
                       for item in plugin.metrics]
        analyzer_keys = [(item.plugin_id, item.analyzer_id, item.version)
                         for item in plugin.analyzers]
        if len(strategy_keys) != len(set(strategy_keys)) \
                or len(metric_keys) != len(set(metric_keys)) \
                or len(analyzer_keys) != len(set(analyzer_keys)):
            raise ExperimentPluginError("plugin contains duplicate capability ids")
        with self._lock:
            if plugin_key in self._plugins:
                raise ExperimentPluginError(
                    "experiment plugin version already registered: "
                    f"{plugin.plugin_id}@{plugin.version}"
                )
            conflicts = [
                *(key for key in strategy_keys if key in self._strategies),
                *(key for key in metric_keys if key in self._metrics),
                *(key for key in analyzer_keys if key in self._analyzers),
            ]
            if conflicts:
                raise ExperimentPluginError(
                    f"experiment capability already registered: {conflicts[0]}"
                )
            self._plugins[plugin_key] = plugin
            self._strategies.update(zip(strategy_keys, plugin.strategies))
            self._metrics.update(zip(metric_keys, plugin.metrics))
            self._analyzers.update(zip(analyzer_keys, plugin.analyzers))
            self._generation += 1

        disposed = False

        def dispose() -> None:
            nonlocal disposed
            with self._lock:
                if disposed:
                    return
                disposed = True
                if self._plugins.get(plugin_key) is not plugin:
                    return
                self._plugins.pop(plugin_key, None)
                for key, provider in zip(strategy_keys, plugin.strategies):
                    if self._strategies.get(key) is provider:
                        self._strategies.pop(key, None)
                for key, provider in zip(metric_keys, plugin.metrics):
                    if self._metrics.get(key) is provider:
                        self._metrics.pop(key, None)
                for key, provider in zip(analyzer_keys, plugin.analyzers):
                    if self._analyzers.get(key) is provider:
                        self._analyzers.pop(key, None)
                self._generation += 1

        return dispose

    @staticmethod
    def _require_version(version: str | None) -> str | None:
        if version is None:
            return None
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ExperimentPluginError("experiment plugin version is invalid")
        return version

    def _require_provider(self, providers: Mapping[tuple[str, str, str], Any],
                          *, kind: str, plugin_id: str, capability_id: str,
                          version: str | None) -> Any:
        version = self._require_version(version)
        with self._lock:
            if version is not None:
                provider = providers.get((plugin_id, capability_id, version))
                matches = [] if provider is None else [provider]
            else:
                matches = [
                    provider for (candidate_plugin, candidate_id, _), provider
                    in providers.items()
                    if candidate_plugin == plugin_id and candidate_id == capability_id
                ]
        reference = f"{plugin_id}/{capability_id}"
        if not matches:
            suffix = f"@{version}" if version else ""
            raise ExperimentPluginError(
                f"required experiment {kind} is unavailable: {reference}{suffix}"
            )
        if len(matches) > 1:
            versions = ", ".join(sorted(item.version for item in matches))
            raise ExperimentPluginError(
                f"experiment {kind} version is ambiguous for {reference}; "
                f"choose pluginVersion from: {versions}"
            )
        return matches[0]

    def require_strategy(self, plugin_id: str, strategy_id: str,
                         version: str | None = None) -> StrategyProvider:
        return self._require_provider(
            self._strategies, kind="strategy", plugin_id=plugin_id,
            capability_id=strategy_id, version=version,
        )

    def require_metric(self, plugin_id: str, metric_id: str,
                       version: str | None = None) -> MetricProvider:
        return self._require_provider(
            self._metrics, kind="metric", plugin_id=plugin_id,
            capability_id=metric_id, version=version,
        )

    def require_analyzer(self, plugin_id: str, analyzer_id: str,
                         version: str | None = None) -> AnalyzerProvider:
        return self._require_provider(
            self._analyzers, kind="analyzer", plugin_id=plugin_id,
            capability_id=analyzer_id, version=version,
        )

    def catalog(self) -> dict[str, Any]:
        """Return callback-free provider metadata for settings and tooling."""
        with self._lock:
            plugins = list(self._plugins.values())
        return {
            "contractVersion": "tofu.experiment-plugin-catalog/v1",
            "plugins": [
                {
                    "pluginId": plugin.plugin_id,
                    "version": plugin.version,
                    "description": plugin.description,
                    "strategies": [
                        {
                            "strategyId": item.strategy_id,
                            "description": item.description,
                            "implementationDigest": item.implementation_digest,
                            "configSchema": dict(item.config_schema),
                        }
                        for item in plugin.strategies
                    ],
                    "metrics": [
                        {
                            "metricId": item.metric_id,
                            "description": item.description,
                            "unit": item.unit,
                            "direction": item.direction,
                            "implementationDigest": item.implementation_digest,
                        }
                        for item in plugin.metrics
                    ],
                    "analyzers": [
                        {
                            "analyzerId": item.analyzer_id,
                            "description": item.description,
                            "implementationDigest": item.implementation_digest,
                        }
                        for item in plugin.analyzers
                    ],
                }
                for plugin in sorted(
                    plugins, key=lambda item: (item.plugin_id, item.version)
                )
            ],
        }


_REGISTRY = ExperimentRegistry()
_LOAD_LOCK = threading.RLock()
_BUILTINS_LOADED = False
_DISCOVERY_LOADED = False


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    with _LOAD_LOCK:
        if _BUILTINS_LOADED:
            return
        from .builtin_context_cost import plugin
        from .builtin_long_agent import plugin as long_agent_plugin

        _REGISTRY.register(plugin())
        _REGISTRY.register(long_agent_plugin())
        _BUILTINS_LOADED = True


def discover_experiment_plugins(*, entries: Any = None,
                                fail_fast: bool = False) -> int:
    """Discover external bundles once; active references remain fail-loud."""
    global _DISCOVERY_LOADED
    with _LOAD_LOCK:
        if entries is None and _DISCOVERY_LOADED:
            return 0
        try:
            selected = (entry_points(group=ENTRY_POINT_GROUP)
                        if entries is None else entries)
        except TypeError:
            selected = entry_points().get(ENTRY_POINT_GROUP, [])
        except Exception as exc:
            if fail_fast:
                raise ExperimentPluginError("experiment discovery failed") from exc
            logger.warning("[Experiments] plugin discovery failed: %s", exc)
            return 0
        loaded = 0
        for entry in selected:
            name = str(getattr(entry, "name", "plugin"))
            try:
                value = entry.load()
                value = value() if callable(value) else value
                bundles = value if isinstance(value, (list, tuple)) else [value]
                for bundle in bundles:
                    _REGISTRY.register(bundle)
                    loaded += 1
            except Exception as exc:
                if fail_fast:
                    raise ExperimentPluginError(
                        f"experiment plugin {name!r} failed"
                    ) from exc
                logger.warning(
                    "[Experiments] optional plugin %r failed: %s",
                    name, exc, exc_info=True,
                )
        if entries is None:
            _DISCOVERY_LOADED = True
        return loaded


def registry(*, discover: bool = True) -> ExperimentRegistry:
    """Return the process registry after mounting built-in capabilities."""
    _load_builtins()
    if discover:
        discover_experiment_plugins()
    return _REGISTRY


__all__ = [
    "AnalyzerProvider",
    "ENTRY_POINT_GROUP",
    "ExperimentPlugin",
    "ExperimentPluginError",
    "ExperimentRegistry",
    "MetricProvider",
    "StrategyProvider",
    "discover_experiment_plugins",
    "implementation_digest",
    "registry",
]
