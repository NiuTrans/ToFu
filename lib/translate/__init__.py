"""Lazy compatibility facade for translation engines and task state.

The stable package-level surface remains available while focused imports and
HTTP route registration keep LLM, incremental, worker, and PPTX execution
modules dormant until their corresponding operation starts.
"""

from __future__ import annotations

from importlib import import_module

__all__ = [
    '_TRANSLATE_TASK_TTL', '_SYNC_TRANSLATE_MAX_CHARS',
    '_build_translate_prompt', '_wrap_for_translation', '_strip_notranslate_tags',
    '_extract_notranslate_blocks', '_reattach_notranslate_blocks',
    '_NOTRANSLATE_RE', '_NOTRANSLATE_ALIAS_RE',
    '_NT_PLACEHOLDER_FMT', '_NT_PLACEHOLDER_RE', '_NT_PLACEHOLDER_LOOSE_RE',
    '_dedup_repetition_loop', '_dedup_inline_loop',
    '_translate_one_chunk', '_translate_freetext', 'TranslationContentRefused',
    '_format_status_message',
    '_translate_runtime', '_cleanup_translate_tasks', '_do_translate',
    'commit_translation_to_turn', 'mark_turn_translation_complete',
    'submit_round_segment', 'submit_thinking_segment', 'finalize_incremental',
    'finalize_incremental_stamp_only', 'cancel_incremental',
    '_do_translate_pptx', '_ensure_pptx_upload_dir',
    '_PPTX_UPLOAD_DIR', '_MAX_PPTX_BYTES',
]

_EXPORT_MODULES = {
    # Constants and pure text transforms.
    '_TRANSLATE_TASK_TTL': 'lib.translate.constants',
    '_SYNC_TRANSLATE_MAX_CHARS': 'lib.translate.constants',
    '_build_translate_prompt': 'lib.translate.prompt',
    '_wrap_for_translation': 'lib.translate.prompt',
    '_strip_notranslate_tags': 'lib.translate.prompt',
    '_extract_notranslate_blocks': 'lib.translate.notranslate',
    '_reattach_notranslate_blocks': 'lib.translate.notranslate',
    '_NOTRANSLATE_RE': 'lib.translate.notranslate',
    '_NOTRANSLATE_ALIAS_RE': 'lib.translate.notranslate',
    '_NT_PLACEHOLDER_FMT': 'lib.translate.notranslate',
    '_NT_PLACEHOLDER_RE': 'lib.translate.notranslate',
    '_NT_PLACEHOLDER_LOOSE_RE': 'lib.translate.notranslate',
    '_dedup_repetition_loop': 'lib.translate.dedup',
    '_dedup_inline_loop': 'lib.translate.dedup',
    # LLM/MT execution and typed failures.
    '_translate_one_chunk': 'lib.translate.engine',
    '_translate_freetext': 'lib.translate.engine',
    'TranslationContentRefused': 'lib.translate.errors',
    '_format_status_message': 'lib.translate.status',
    # Shared task authority and background worker.
    '_translate_runtime': 'lib.translate.runtime._state',
    '_cleanup_translate_tasks': 'lib.translate.runtime._state',
    '_do_translate': 'lib.translate.runtime._worker',
    'commit_translation_to_turn': 'lib.translate.commit',
    'mark_turn_translation_complete': 'lib.translate.commit',
    # Incremental per-round translation.
    'submit_round_segment': 'lib.translate.incremental',
    'submit_thinking_segment': 'lib.translate.incremental',
    'finalize_incremental': 'lib.translate.incremental',
    'finalize_incremental_stamp_only': 'lib.translate.incremental',
    'cancel_incremental': 'lib.translate.incremental',
    # PPTX storage and execution.
    '_do_translate_pptx': 'lib.translate.pptx',
    '_ensure_pptx_upload_dir': 'lib.translate.pptx',
    '_PPTX_UPLOAD_DIR': 'lib.translate.pptx',
    '_MAX_PPTX_BYTES': 'lib.translate.pptx',
}

_CHILD_MODULES = {
    'commit', 'constants', 'dedup', 'engine', 'errors', 'incremental',
    'notranslate', 'pptx', 'prompt', 'runtime', 'status',
}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.translate.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
