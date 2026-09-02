"""Lazy public facade for the bounded Daily Optimizer pipeline."""

from __future__ import annotations

from importlib import import_module

__all__ = ['run_once']

_EXPORT_MODULES = {
    'run_once': 'lib.optimizer.orchestrator',
}

_CHILD_MODULES = {
    'actions', 'analyzer', 'applier', 'orchestrator', 'proposer', 'storage',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.optimizer.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
