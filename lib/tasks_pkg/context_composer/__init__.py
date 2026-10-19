"""Single context composition boundary for conversational LLM roles."""

from __future__ import annotations

import json


from lib.log import get_logger

logger = get_logger(__name__)
from lib.tasks_pkg.context_composer._models import (
    ComposeRequest,
    ComposeResult,
    ContextBlock,
    ContextPlanEntryV2,
    ContextPlanV2,
)
from lib.tasks_pkg.context_composer._providers import collect_context_blocks
from lib.tasks_pkg.context_composer._render import render_context, verified_block_hash
from lib.tasks_pkg.context_composer._snapshot_compaction import compact_context_snapshots
from lib.tasks_pkg.context_composer.injections import (
    append_injection, can_append_injection, record_injection_diagnostic,
    record_manifest_rows,
)
from lib.tasks_pkg.context_composer.registry import (
    INJECTION_REGISTRY, InjectionSpec, context_block, injection_spec,
)
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


# Ambient owner/context providers a delegated child invocation never needs.
# The child has its own scoped worker contract (worker_policy), objective and
# tool allowlist; the parent's skills index, credential vault index, user
# profile, swarm guidance and UI-nav directory are root-assembly overhead that
# costs tokens on every sub-agent turn without informing its bounded objective.
_CHILD_DISABLED_PROVIDERS = frozenset({
    'skills_index',     # provider 'skills' (index + selection follow-on)
    'selected_skills',
    'credential_vault', # provider 'vault'
    'user_context',     # provider 'profile'
    'parallel_execution',  # provider 'swarm'
    'ui_nav_directory',
})


def _child_disabled_blocks(request: ComposeRequest) -> frozenset[str]:
    """Role-derived narrowing for child invocations (design principle 4).

    Extends the existing disabled_blocks ablation seam rather than a parallel
    provider gate. Only optional ambient providers are narrowed; project rules
    and tool-relevant blocks (mcp_tools_delta, disclosed_tools, environment)
    are preserved so the child keeps its routing/capability contract.
    """
    task = request.task if isinstance(request.task, dict) else None
    if task is None:
        return frozenset()
    try:
        from lib.tasks_pkg.invocation import is_child_invocation
        if not is_child_invocation(task):
            return frozenset()
    except Exception:
        return frozenset()
    return _CHILD_DISABLED_PROVIDERS


def _with_context_policy(request: ComposeRequest) -> ComposeRequest:
    """All entrypoints inherit task-owned ablations without changing the input."""
    from dataclasses import replace
    disabled = (request.disabled_blocks
                | disabled_context_blocks((request.task or {}).get('config'))
                | _child_disabled_blocks(request))
    return (replace(request, disabled_blocks=disabled)
            if disabled != request.disabled_blocks else request)


def _round_num(task: dict) -> int:
    # The root loop stamps this before composition/admission, including retry
    # and no-tool rounds. Tool-entry count is NOT a model-round ordinal.
    value = task.get('_activeRoundNum')
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _record_session_context_note(task: dict, manifest: list[dict]) -> None:
    """Fire-once-per-conversation note witnessing committed static session
    context (platform_static, project_rules, …). Aggregate, not per-block, for
    signal-to-noise; re-fires only if the committed set/hash actually changed
    after a reload."""
    static_rows = [row for row in manifest
                   if row.get('appended') and row.get('placement') == 'system']
    if not static_rows:
        return
    ids = sorted(str(row.get('registryId') or row.get('id')) for row in static_rows)
    signature = '|'.join(f'{row.get("registryId") or row.get("id")}:{row.get("hash")}'
                         for row in sorted(static_rows,
                                           key=lambda r: str(r.get('registryId') or r.get('id'))))
    if task.get('_sessionContextNoteSig') == signature:
        return
    body = ('Session context injected into the model-visible message stream:\n'
            + '\n'.join(f'- {i}' for i in ids))
    try:
        from lib.tasks_pkg.segments import record_injected_note
        note = record_injected_note(task, llm_round=_round_num(task),
                                    kind='session-context', text=body, action='prompt',
                                    reason='session_context_committed')
        if note is not None:
            task['_sessionContextNoteSig'] = signature
        else:
            logger.warning('session-context note recorder rejected committed witness')
    except Exception:
        logger.exception('session-context note emission failed (display-only)')


def _record_committed_context_notes(task: dict, manifest: list[dict]) -> None:
    """Display-only note for every round-scoped block committed to the wire this
    pass. Fire-once per commit: content-address reuse reports appended=False, so
    steady-state turns emit nothing and a changed block re-fires exactly once."""
    rows = [row for row in manifest
            if row.get('appended') and row.get('placement') != 'system']
    if not rows:
        return
    try:
        from lib.tasks_pkg.segments import record_injected_note
    except Exception:
        logger.exception('context-injection note recorder import failed (display-only)')
        return
    round_num = _round_num(task)
    for row in rows:
        try:
            record_injected_note(task, llm_round=round_num, kind='context-injection',
                                 text=str(row.get('noteText') or ''),
                                 action='prompt',
                                 reason=str(row.get('registryId') or row.get('id') or ''))
        except Exception:
            logger.exception('context-injection note emission failed for %s',
                             row.get('id'))

def _retain_project_delivery(task: dict, manifest: list[dict], *,
                             full_refresh: bool = False) -> None:
    """Only a complete admitted page may await provider-success ACK.

    A detached provider can prepare a cursor page before optional admission.
    Suppression, truncation and open-batch deferral must not acknowledge it.
    Unrelated round-scoped appends leave a previously admitted page alone.
    """
    rows = [row for row in manifest if row['id'] == 'project_context']
    if not rows and not full_refresh:
        return
    if not any(row.get('injected')
               and (row.get('appended') or row.get('reused'))
               and 'truncated' not in str(row.get('reason') or '')
               for row in rows):
        task.pop('_projectNarrativeDelivery', None)


def compose_context(messages: list[dict], request: ComposeRequest) -> ComposeResult:
    if request.task and request.task.get('aborted'):
        return ComposeResult(messages=messages, manifest=[])
    request = _with_context_policy(request)
    record_manifest_rows(request.task, [
        dict(message['_contextInjection']) for message in messages
        if isinstance(message, dict) and isinstance(message.get('_contextInjection'), dict)
    ])
    blocks = collect_context_blocks(messages, request)
    # Providers may yield while owner Stop arrives; neither new context nor
    # withdrawal of stale checklist carriers may commit after cancellation.
    if request.task and request.task.get('aborted'):
        return ComposeResult(messages=messages, manifest=[])
    from .checklist import reconcile_checklist_context
    reconcile_checklist_context(messages, request.task)
    result = render_context(messages, blocks, request)

    if request.task is not None:
        _retain_project_delivery(request.task, result.manifest, full_refresh=True)
    if request.task is not None:
        _record_session_context_note(request.task, result.manifest)
        _record_committed_context_notes(request.task, result.manifest)
        record_manifest_rows(request.task, result.manifest)
        if result.plan is not None:
            from dataclasses import asdict
            request.task['_contextPlanV2'] = asdict(result.plan)
    return result


def refresh_programmatic_inputs(messages: list[dict], *, task: dict,
                                user_id: int, model: str, tool_names: set[str]) -> ComposeResult:
    """Acquire the current turn's bounded directory before prompt admission.

    Startup composition precedes the first program and cannot discover later
    captures. This round port refreshes only the owned directory, not providers.
    """
    from ._providers import _programmatic_input_block
    from lib.context_experiment_flags import normalize_context_experiment_flags
    flags = normalize_context_experiment_flags(task.get('config') or {})
    budget = int(flags['context']['globalBudgetTokens'] or 0)
    base_tokens = 0
    if budget:
        from lib.token_counter import count_text
        base_tokens = count_text(json.dumps(messages, ensure_ascii=False,
                                           separators=(',', ':'), default=str), model=model)
    request = ComposeRequest(user_id=user_id, model=model, task=task,
                             has_real_tools=bool(tool_names), tool_names=frozenset(tool_names),
                             global_budget_tokens=budget or None, base_context_tokens=base_tokens,
                             disabled_blocks=disabled_context_blocks(task.get('config') or {}))
    return append_context_blocks(messages, [_programmatic_input_block(request)], request)


def append_context_blocks(messages: list[dict], blocks: list[ContextBlock],
                          request: ComposeRequest) -> ComposeResult:
    """Append round-scoped blocks without rewriting the stable task prefix."""

    if request.task and request.task.get('aborted'):
        return ComposeResult(messages=messages, manifest=[])
    from .checklist import reconcile_checklist_context
    reconcile_checklist_context(messages, request.task)
    request = _with_context_policy(request)
    result = render_context(messages, blocks, request, replace_managed=False)

    if request.task is not None:
        _retain_project_delivery(request.task, result.manifest)
    if request.task is not None:
        _record_session_context_note(request.task, result.manifest)
        _record_committed_context_notes(request.task, result.manifest)
    record_manifest_rows(request.task, result.manifest)
    return result


__all__ = [
    'ComposeRequest', 'ComposeResult', 'ContextBlock', 'ContextPlanEntryV2',
    'ContextPlanV2', 'TaskStateSnapshotV1', 'compose_context',
    'compose_task_context', 'disabled_context_blocks',
    'append_context_blocks', 'compact_context_snapshots',
    'refresh_programmatic_inputs',
    'INJECTION_REGISTRY', 'InjectionSpec', 'context_block', 'injection_spec',
    'append_injection', 'can_append_injection', 'record_injection_diagnostic',
    'record_manifest_rows',
    'collect_context_blocks', 'derive_task_state_snapshot', 'render_context',
]
