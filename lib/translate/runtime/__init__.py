"""Lazy facade for translation task state, segments, commits, and workers."""

from __future__ import annotations

from importlib import import_module

__all__ = [
    '_translate_runtime',
    '_cleanup_translate_tasks',
    '_read_turn_segments',
    '_build_segment_translation_map',
    '_translate_segments_to_map',
    'commit_translation_to_turn',
    'mark_turn_translation_complete',
    '_do_translate',
]

_EXPORT_MODULES = {
    '_translate_runtime': 'lib.translate.runtime._state',
    '_cleanup_translate_tasks': 'lib.translate.runtime._state',
    '_read_turn_segments': 'lib.translate.runtime._segments',
    '_build_segment_translation_map': 'lib.translate.runtime._segments',
    '_translate_segments_to_map': 'lib.translate.runtime._segments',
    'commit_translation_to_turn': 'lib.translate.commit',
    'mark_turn_translation_complete': 'lib.translate.commit',
    '_do_translate': 'lib.translate.runtime._worker',
}

_CHILD_MODULES = {'_segments', '_state', '_worker'}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.translate.runtime.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
