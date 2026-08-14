"""Section 3 context injection — extracted from ``_run.py`` (pt_03f4cdf1 slice 7).

The block this module replaces was the ~83-line "Section 3: Context Injection"
region of ``run_task``, sitting between tool-history restoration and the pre-loop
memory-prefetch step. It is a pure orchestration seam: no closures captured, no
recursion, no shared mutable state beyond what already lives on ``task``.

**Byte-parity extraction**: every step, every kwarg order, every log line, every
event predicate is preserved verbatim.  The caller (run_task) hands in the
resolved-at-that-point locals it needs; the helper returns the ``_t_prep_done``
timing anchor (also stashed on the task the way the inline code did) so the
caller keeps the local for use in the pre-loop init that follows.

**What it does** (in order):

  1. Emit VU phase ``Autopilot：注入系统上下文（项目结构、记忆检索）…``.
  2. Build ``_tool_names`` set from ``tool_list``.
  3. Call ``_inject_system_contexts(...)`` with the resolved
     project/memory/search/swarm capabilities and disabled prompt blocks.
  4. Emit ``PREFERENCES_APPLIED`` SSE if ``task['_appliedPreferences']`` was
     populated by the injection (the chip that lets the user see which
     preferences the model was made aware of).
  5. Emit ``RELATED_CONVERSATIONS`` SSE if ``task['_relatedConversations']``
     was populated by the injection (the sibling-conversation digest chip).
  6. Pop the project-context future + shutdown the ``prefetch_executor``.
  7. Stash ``_t_prep_done`` on the task and log the prep-duration timing line.
  8. Emit VU phase ``Autopilot：上下文就绪，正在发送请求…``.

Steps 4 & 5 are guarded on task-field truthiness (no config lookup) — matches
inline exactly.  Both are wrapped in a debug-swallow try/except to prevent an
emit-side failure from breaking the run, mirroring the inline behaviour.
"""

from __future__ import annotations

import time
from typing import Any

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event
from lib.tasks_pkg.system_context import (
    _disabled_prompt_blocks,
    _inject_system_contexts,
)

logger = get_logger(__name__)


def _emit_vu_context_phase(vu_phase, detail: str, detail_key: str) -> None:
    """Emit one context boundary through run_task's VU-only adapter.

    The callback owns the ``_vu_subtask`` gate and the carrier transform; this
    helper only keeps the expensive operation fail-open while ensuring an
    emit-side error leaves a diagnostic trace.
    """
    if not callable(vu_phase):
        return
    try:
        vu_phase(detail, detail_key=detail_key)
    except Exception as e:
        logger.debug('[orchestrator] vu context phase emit failed key=%s: %s',
                     detail_key, e)


def inject_context_and_emit_chips(
    *,
    task: dict[str, Any],
    messages: list,
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    memory_enabled: bool,
    search_enabled: bool,
    swarm_enabled: bool,
    has_real_tools: bool,
    model: str,
    tool_list: list | None,
    prefetch_executor,
    tid: str,
    t_run_start: float,
    vu_phase=None,
) -> float:
    """Run the extracted Section 3.

    Args:
        task: The live task dict (mutable — this function stashes fields on it).
        messages: The current messages list (mutable — _inject_system_contexts
            splices meta turns into it).
        cfg: The run's config dict.
        project_path / project_enabled / memory_enabled / search_enabled /
            swarm_enabled / has_real_tools: resolved capability locals from
            Section 1.
        model: The resolved model id.
        tool_list: The assembled tool list from Section 2 (used to derive
            ``_tool_names``).
        prefetch_executor: The prefetch pool started before Section 2; this
            function shuts it down after inject.
        tid: The short task id (``task['id'][:8]``).
        t_run_start: Wall-clock start-of-run anchor (for the prep timing line).
        vu_phase: Optional callback accepting ``detail`` plus the structured
            ``detail_key`` keyword. The run_task adapter owns the VU-only gate;
            this helper calls it immediately before and after the expensive
            context-injection boundary.

    Returns:
        ``_t_prep_done`` — the wall-clock timestamp taken at the end of
        Section 3, the anchor stream_llm_response uses for time-to-first-token.
    """
    # 1. Attribute the real slow boundary before entering context providers.
    _emit_vu_context_phase(
        vu_phase,
        'Autopilot is injecting system context (project structure and memory)…',
        'stream.phase.vuInjectContext',
    )

    # 2. Tool-name set for injection filter.
    _tool_names = {
        (t.get('function') or {}).get('name')
        for t in (tool_list or [])
        if isinstance(t, dict)
    }
    _tool_names.discard(None)
    # Compaction re-entry rebuilds capability-aware providers from these names
    # without retaining the full tool schema in ambient context.
    task['_contextToolNames'] = sorted(_tool_names)
    task['_contextHasRealTools'] = bool(has_real_tools)

    # 3. Do the injection (mutates messages in place; stashes chip metadata
    #    on task).
    _inject_system_contexts(
        messages, project_path, project_enabled,
        memory_enabled, search_enabled, swarm_enabled,
        has_real_tools,
        conv_id=task.get('convId', ''),
        task=task,
        model=model,
        system_prompt_mode=cfg.get('systemPromptMode', 'append'),
        tool_names=_tool_names or None,
        disabled_blocks=_disabled_prompt_blocks(cfg),
    )

    # 4. Preferences-applied chip.
    _applied_prefs = task.get('_appliedPreferences')
    if _applied_prefs:
        try:
            append_event(task, build_event(
                EventType.PREFERENCES_APPLIED,
                chars=_applied_prefs.get('chars', 0),
                items=_applied_prefs.get('items', []),
                core=_applied_prefs.get('core', []),
                detail=_applied_prefs.get('detail', []),
            ))
            task['_preferencesApplied'] = dict(_applied_prefs)
        except Exception as _e:
            logger.debug('[orchestrator] preferences_applied emit failed: %s', _e)

    # 5. Related-conversations chip.
    _related_convs = task.get('_relatedConversations')
    if _related_convs:
        try:
            append_event(task, build_event(
                EventType.RELATED_CONVERSATIONS,
                count=_related_convs.get('count', 0),
                items=_related_convs.get('items', []),
                toolsAvailable=_related_convs.get('toolsAvailable', False),
            ))
            task['_relatedConversations'] = dict(_related_convs)
        except Exception as _e:
            logger.debug('[orchestrator] related_conversations emit failed: %s', _e)

    # 6. Prefetch cleanup.
    task.pop('_prefetch_project', None)
    try:
        prefetch_executor.shutdown(wait=False)
    except Exception as _e:
        logger.debug('[orchestrator] prefetch_executor shutdown failed: %s', _e)

    # 7. Timing anchor + prep-duration log line (byte-parity with inline).
    _t_prep_done = time.time()
    task['_t_prep_done'] = _t_prep_done
    logger.info('[Timing:%s] prep=%.3fs (run_task→context-ready, '
                'model=%s) — about to build first LLM request',
                tid, _t_prep_done - t_run_start, model)

    # 8. The next real backend action is request construction / dispatch.
    _emit_vu_context_phase(
        vu_phase,
        'Autopilot context is ready; sending the model request…',
        'stream.phase.vuContextReady',
    )

    return _t_prep_done


__all__ = ['inject_context_and_emit_chips']
