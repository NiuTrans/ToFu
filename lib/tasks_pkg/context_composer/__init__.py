"""Single context composition boundary for conversational LLM roles."""

from __future__ import annotations

import json

from lib.tasks_pkg.context_composer._models import (
    ComposeRequest,
    ComposeResult,
    ContextBlock,
    ContextPlanEntryV2,
    ContextPlanV2,
)
from lib.tasks_pkg.context_composer._providers import collect_context_blocks
from lib.tasks_pkg.context_composer._render import render_context
from lib.tasks_pkg.context_composer.task_state import (
    TaskStateSnapshotV1,
    derive_task_state_snapshot,
)


def disabled_context_blocks(cfg: dict | None) -> frozenset[str]:
    """Return the explicitly disabled prompt-block identifiers."""
    prompt_blocks = (cfg or {}).get('systemPromptBlocks') or {}
    if not isinstance(prompt_blocks, dict):
        return frozenset()
    disabled = prompt_blocks.get('disabled') or ()
    if isinstance(disabled, str):
        disabled = (disabled,)
    return frozenset(str(item) for item in disabled if item)


def compose_task_context(
    messages: list[dict],
    *,
    user_id: int,
    project_path: str = '',
    project_enabled: bool = False,
    memory_enabled: bool = False,
    search_enabled: bool = False,
    has_real_tools: bool = False,
    conv_id: str = '',
    task: dict | None = None,
    model: str = '',
    system_prompt_mode: str = 'append',
    tool_names: set[str] | None = None,
    disabled_blocks: set[str] | frozenset[str] | None = None,
) -> ComposeResult:
    """Build a request from task state and compose all managed context once."""
    from lib.context_experiment_flags import normalize_context_experiment_flags
    cfg = (task or {}).get('config') or {}
    flags = normalize_context_experiment_flags(cfg)
    global_budget = int(flags['context']['globalBudgetTokens'] or 0)
    base_context_tokens = 0
    if global_budget:
        try:
            from lib.token_counter import count_text
            base_context_tokens = int(count_text(
                json.dumps(messages, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), default=str),
                model=model or '',
            ))
        except Exception as exc:
            from lib.log import get_logger
            get_logger(__name__).debug(
                '[ContextComposer] base token count failed: %s', exc)
    raw_permissions = cfg.get('grantedToolPermissions') or ()
    if isinstance(raw_permissions, str):
        raw_permissions = (raw_permissions,)
    request = ComposeRequest(
        project_path=project_path or '',
        project_enabled=bool(project_enabled),
        memory_enabled=bool(memory_enabled),
        search_enabled=bool(search_enabled),
        has_real_tools=bool(has_real_tools),
        conv_id=conv_id or '',
        user_id=int(user_id),
        model=model or '',
        system_prompt_mode=system_prompt_mode or 'append',
        tool_names=frozenset(tool_names or ()),
        disabled_blocks=frozenset(disabled_blocks or ()),
        task=task,
        global_budget_tokens=global_budget or None,
        base_context_tokens=base_context_tokens,
        granted_permissions=frozenset(
            str(value) for value in raw_permissions if value),
    )
    return compose_context(messages, request)


def compose_context(messages: list[dict], request: ComposeRequest) -> ComposeResult:
    blocks = collect_context_blocks(messages, request)
    result = render_context(messages, blocks, request)
    if request.task is not None:
        request.task['_contextManifest'] = result.manifest
        if result.plan is not None:
            from dataclasses import asdict
            request.task['_contextPlanV2'] = asdict(result.plan)
    return result


def append_context_blocks(messages: list[dict], blocks: list[ContextBlock],
                          request: ComposeRequest) -> ComposeResult:
    """Append round-scoped blocks without rewriting the stable task prefix."""
    result = render_context(messages, blocks, request, replace_managed=False)
    if request.task is not None:
        manifest = request.task.setdefault('_contextManifest', [])
        manifest.extend(
            row for row in result.manifest if row.get('appended'))
    return result


__all__ = [
    'ComposeRequest', 'ComposeResult', 'ContextBlock', 'ContextPlanEntryV2',
    'ContextPlanV2', 'TaskStateSnapshotV1', 'compose_context',
    'compose_task_context', 'disabled_context_blocks',
    'append_context_blocks',
    'collect_context_blocks', 'derive_task_state_snapshot', 'render_context',
]
