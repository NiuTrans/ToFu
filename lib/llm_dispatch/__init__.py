"""Lazy public facade for dynamic multi-provider LLM dispatch.

The package maps stable public names to their owning modules without loading
provider discovery, dispatcher state, or HTTP transports during server route
registration. Accessing an operation retains the historical package-level
API, for example ``from lib.llm_dispatch import dispatch_stream``.
"""

from __future__ import annotations

from importlib import import_module

from lib.log import get_logger

_logger = get_logger(__name__)

__all__ = [
    "DEFAULT_SLOT_CONFIGS",
    "MODEL_ALIASES",
    "MODEL_ALIAS_GROUPS",
    "PRICING_TIERS",
    "MANAGED_TIER_TAGS",
    "get_pricing_tiers",
    "reevaluate_pricing_tags",
    "Slot",
    "THINKING_FORMATS",
    "discover_models",
    "enrich_models_with_pricing",
    "is_local_endpoint",
    "is_raw_ip_host",
    "should_bypass_proxy",
    "normalize_base_url",
    "probe_provider",
    "DispatcherFactory",
    "get_dispatcher",
    "reset_dispatcher",
    "LLMDispatcher",
    "DispatchNoAdmissibleSlot",
    "DispatchRateLimitBudgetExceeded",
    "DispatchSharedContentionDeferred",
    "pick_key_for_model",
    "dispatch_chat",
    "dispatch_stream",
    "async_dispatch_stream",
    "dispatch_fastest",
    "dispatch_parallel",
    "get_dispatch_status",
    "_group_by_capability",
    "smart_chat",
    "smart_chat_batch",
]

_EXPORT_MODULES = {
    # Static configuration and slot state.
    "DEFAULT_SLOT_CONFIGS": "lib.llm_dispatch.config",
    "MODEL_ALIASES": "lib.llm_dispatch.config",
    "MODEL_ALIAS_GROUPS": "lib.llm_dispatch.config",
    "PRICING_TIERS": "lib.llm_dispatch.config",
    "MANAGED_TIER_TAGS": "lib.llm_dispatch.config",
    "get_pricing_tiers": "lib.llm_dispatch.config",
    "reevaluate_pricing_tags": "lib.llm_dispatch.config",
    "Slot": "lib.llm_dispatch.slot",
    "THINKING_FORMATS": "lib.llm_dispatch.slot",
    # Provider discovery and dispatcher lifecycle.
    "discover_models": "lib.llm_dispatch.discovery",
    "enrich_models_with_pricing": "lib.llm_dispatch.discovery",
    "is_local_endpoint": "lib.llm_dispatch.discovery",
    "is_raw_ip_host": "lib.llm_dispatch.discovery",
    "should_bypass_proxy": "lib.llm_dispatch.discovery",
    "normalize_base_url": "lib.llm_dispatch.discovery",
    "probe_provider": "lib.llm_dispatch.discovery",
    "DispatcherFactory": "lib.llm_dispatch.factory",
    "get_dispatcher": "lib.llm_dispatch.factory",
    "reset_dispatcher": "lib.llm_dispatch.factory",
    "LLMDispatcher": "lib.llm_dispatch.dispatcher",
    # High-level request operations.
    "DispatchNoAdmissibleSlot": "lib.llm_dispatch.api",
    "DispatchRateLimitBudgetExceeded": "lib.llm_dispatch.api",
    "DispatchSharedContentionDeferred": "lib.llm_dispatch.api",
    "pick_key_for_model": "lib.llm_dispatch.api",
    "dispatch_chat": "lib.llm_dispatch.api",
    "dispatch_stream": "lib.llm_dispatch.api",
    "async_dispatch_stream": "lib.llm_dispatch.api",
    "dispatch_fastest": "lib.llm_dispatch.api",
    "dispatch_parallel": "lib.llm_dispatch.api",
    "get_dispatch_status": "lib.llm_dispatch.api",
    "_group_by_capability": "lib.llm_dispatch.api",
    "smart_chat": "lib.llm_dispatch.api",
    "smart_chat_batch": "lib.llm_dispatch.api",
}

_CHILD_MODULES = {
    "api",
    "config",
    "discovery",
    "dispatcher",
    "factory",
    "slot",
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f"lib.llm_dispatch.{name}"
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    try:
        module = import_module(module_name)
        value = module if name in _CHILD_MODULES else getattr(module, name)
    except Exception as exc:
        _logger.warning(
            "%s%s failed to load while resolving %s: %s",
            __name__, module_name, name, exc, exc_info=True,
        )
        raise AttributeError(
            f"module {__name__!r} could not resolve {name!r}"
        ) from exc
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
