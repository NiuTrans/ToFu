"""Single context composition boundary for conversational LLM roles."""

from __future__ import annotations

from lib.tasks_pkg.context_composer._models import (
    ComposeRequest,
    ComposeResult,
    ContextBlock,
)
from lib.tasks_pkg.context_composer._providers import collect_context_blocks
from lib.tasks_pkg.context_composer._render import render_context


def compose_context(messages: list[dict], request: ComposeRequest) -> ComposeResult:
    blocks = collect_context_blocks(messages, request)
    result = render_context(messages, blocks, request)
    if request.task is not None:
        request.task['_contextManifest'] = result.manifest
    return result


def append_context_blocks(messages: list[dict], blocks: list[ContextBlock],
                          request: ComposeRequest) -> ComposeResult:
    """Append round-scoped blocks without rewriting the stable task prefix."""
    result = render_context(messages, blocks, request, replace_managed=False)
    if request.task is not None:
        manifest = request.task.setdefault('_contextManifest', [])
        manifest.extend(result.manifest)
    return result


__all__ = [
    'ComposeRequest', 'ComposeResult', 'ContextBlock', 'compose_context',
    'append_context_blocks',
    'collect_context_blocks', 'render_context',
]
