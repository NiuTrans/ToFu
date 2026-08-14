"""Compatibility facade for the canonical Context Composer.

All conversational roles still import ``_inject_system_contexts`` from this
module, but the function no longer owns any block-specific splicing logic.
Providers describe blocks and ``context_composer`` renders them exactly once.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# Kept for compaction/re-entry probes and existing external imports.
_CC_STATIC_MARKER = 'IMPORTANT: You must NEVER generate or guess URLs'


def _disabled_prompt_blocks(cfg: dict) -> set[str] | None:
    try:
        disabled = ((cfg or {}).get('systemPromptBlocks') or {}).get(
            'disabled') or []
        result = {str(item) for item in disabled if item}
        return result or None
    except Exception as exc:
        logger.debug('[ContextComposer] disabled-block parse failed: %s', exc)
        return None


def _extract_last_user_text(messages: list) -> str:
    from lib.memory.prefetch._query import _extract_current_user_request
    return _extract_current_user_request(messages)


def _inject_system_contexts(messages, project_path, project_enabled,
                             memory_enabled, search_enabled, swarm_enabled,
                             has_real_tools, conv_id: str = '',
                             task: dict | None = None, model: str = '',
                             system_prompt_mode: str = 'append',
                             tool_names: set[str] | None = None,
                             disabled_blocks: set[str] | None = None):
    """Compose every ambient context source through the single renderer.

    The positional signature is retained so orchestrator, compaction, endpoint
    roles, and third-party callers migrate without a second context path.
    """
    from lib.tasks_pkg.context_composer import ComposeRequest, compose_context

    request = ComposeRequest(
        project_path=project_path or '',
        project_enabled=bool(project_enabled),
        memory_enabled=bool(memory_enabled),
        search_enabled=bool(search_enabled),
        swarm_enabled=bool(swarm_enabled),
        has_real_tools=bool(has_real_tools),
        conv_id=conv_id or '',
        model=model or '',
        system_prompt_mode=system_prompt_mode or 'append',
        tool_names=frozenset(tool_names or ()),
        disabled_blocks=frozenset(disabled_blocks or ()),
        task=task,
    )
    return compose_context(messages, request)


__all__ = [
    '_CC_STATIC_MARKER', '_disabled_prompt_blocks',
    '_extract_last_user_text', '_inject_system_contexts',
]
