"""lib/orchestration_endpoint_runner.py — Endpoint mode via FlowExecutor.

The FLAGGED cutover entry point. When ``TOFU_ENDPOINT_VIA_FLOW=1`` (default
OFF), ``routes/chat.py`` routes an endpoint task here instead of to the
battle-tested ``lib/tasks_pkg/endpoint.py``. This runs the canonical
endpoint graph (``build_endpoint_definition``) through
:class:`lib.orchestration_engine.FlowExecutor`, translating engine events
into the endpoint SSE/message schema via
:class:`lib.orchestration_endpoint_adapter.EndpointEventAdapter`.

Default-off by design: this is scaffolding so the WHOLE path exists behind
a flag and can be exercised side-by-side with the live path, NOT a
replacement. The live endpoint.py remains authoritative until this has been
validated on real tasks.

Kill switch / opt-in:
    TOFU_ENDPOINT_VIA_FLOW=1   → use this engine path
    (unset / 0)                → use the live lib/tasks_pkg/endpoint.py
"""

from __future__ import annotations


from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def endpoint_via_flow_enabled() -> bool:
    """True iff the flagged FlowExecutor endpoint path is opted in.

    Default OFF — only ``TOFU_ENDPOINT_VIA_FLOW=1`` (or ``true``/``yes``)
    enables it. Anything else (unset, ``0``, garbage) uses the live path.
    """
    val = getenv_compat('TOFU_ENDPOINT_VIA_FLOW', default='0').strip().lower()
    return val in ('1', 'true', 'yes', 'on')


def _extract_user_request(task: dict) -> str:
    """Pull the latest user message text from the task as the flow input."""
    for msg in reversed(task.get('messages') or []):
        if msg.get('role') == 'user':
            content = msg.get('content')
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                # multimodal — concatenate text parts
                parts = [b.get('text', '') for b in content
                         if isinstance(b, dict) and b.get('type') == 'text']
                return '\n'.join(p for p in parts if p)
    return ''


def _build_tools_for_task(task: dict):
    """Assemble the same tool list the orchestrator would, for SubAgents.

    Reuses ``_resolve_model_config`` + ``_assemble_tool_list`` so the flow's
    agents get identical role-scoped tools / model. Returns ``(tool_list,
    model)``; on any failure returns ``([], '')`` and logs — the flow can
    still run (agents just have no tools), surfacing the misconfig in the
    transcript rather than crashing.
    """
    try:
        from lib.tasks_pkg.model_config import (
            _assemble_tool_list, _resolve_model_config,
        )
        cfg = task.get('config') or {}
        mcfg = _resolve_model_config(cfg, task['id'])
        tool_list, _has_real, _max_rounds = _assemble_tool_list(
            cfg, mcfg['project_path'], mcfg['project_enabled'], task['id'],
            mcfg['search_mode'], mcfg['search_enabled'], mcfg['fetch_enabled'],
            mcfg['code_exec_enabled'], mcfg['browser_enabled'],
            mcfg['desktop_enabled'], mcfg['swarm_enabled'],
            image_gen_enabled=mcfg['image_gen_enabled'],
            human_guidance_enabled=mcfg['human_guidance_enabled'],
            scheduler_enabled=mcfg['scheduler_enabled'],
            messages=task.get('messages'),
        )
        return tool_list, mcfg.get('model', ''), mcfg.get('project_path', '')
    except Exception as e:
        logger.error('[EndpointViaFlow] tool assembly failed: %s', e, exc_info=True)
        return [], '', ''


def run_endpoint_via_flow(task: dict):
    """Run endpoint mode through FlowExecutor (flagged path).

    Mirrors ``run_endpoint_task``'s task contract: streams events via
    ``append_event`` and persists via ``persist_task_result``. Each engine
    event is translated to the endpoint schema by EndpointEventAdapter and
    emitted live so the existing frontend renders it unchanged.
    """
    from lib.tasks_pkg.manager import append_event, persist_task_result
    from lib.agent_core.events import EventType, build_event
    from lib.orchestration import build_endpoint_definition
    from lib.orchestration_engine import FlowExecutor, FlowExecutionError
    from lib.orchestration_endpoint_adapter import EndpointEventAdapter
    # Reuse the LIVE endpoint DB-sync path verbatim so the conversation row
    # is written identically (survives SSE disconnect / reload / poll
    # recovery). The adapter's message shapes already match what these
    # functions key off (_isEndpointPlanner / _epIteration / _isEndpointReview).
    from lib.tasks_pkg.endpoint import (
        _store_endpoint_turns_on_task,
        _sync_endpoint_turns_to_conversation,
        _trigger_endpoint_auto_translate,
        _trigger_per_turn_auto_translate,
    )

    if 'id' not in task:
        raise ValueError("run_endpoint_via_flow called with a task missing 'id'")
    tid = task['id'][:8]
    task['endpoint_mode'] = True
    task['_endpoint_phase'] = 'planning'
    task['_endpoint_iteration'] = 0
    task['_endpoint_via_flow'] = True

    audit_log('endpoint_via_flow_start', task_id=task['id'])
    logger.info('[EndpointViaFlow] task=%s START (flagged FlowExecutor path)', tid)

    cfg = task.get('config') or {}
    max_iter = int(cfg.get('endpointMaxIterations') or 10)
    tool_list, model, project_path = _build_tools_for_task(task)
    user_request = _extract_user_request(task)

    defn = build_endpoint_definition(max_iterations=max_iter)

    # Adapter forwards endpoint-shaped messages live AND accumulates them.
    # ``adapter.messages`` is the running list of endpoint turns; we capture
    # it by closure so the emit callback can sync the full snapshot per turn.
    _adapter_ref = {}

    def _emit_endpoint_msg(msg: dict):
        # 1) Translate the adapter's message dict into the SSE event the
        #    frontend already understands.
        if msg.get('_isEndpointPlanner'):
            append_event(task, build_event(
                EventType.ENDPOINT_PLANNER_DONE,
                content=msg.get('content', ''),
                usage={},
            ))
        elif msg.get('_isEndpointReview'):
            append_event(task, build_event(
                EventType.ENDPOINT_CRITIC_MSG,
                iteration=msg.get('_epIteration', 0),
                content=msg.get('content', ''),
                next_phase=msg.get('_epNextPhase', 'worker'),
                synthetic=bool(msg.get('_isSyntheticCritic')),
            ))
        else:
            append_event(task, build_event(
                EventType.ENDPOINT_ITERATION,
                iteration=msg.get('_epIteration', 0),
                phase='working',
            ))

        # 2) Persist incrementally through the LIVE endpoint sync path, so a
        #    turn survives even if SSE drops mid-run (identical to endpoint.py).
        #    The sync returns the absolute DB index of the LAST turn (== msg),
        #    which the per-turn auto-translate trigger needs.
        turns = _adapter_ref.get('messages') or []
        if turns:
            try:
                _store_endpoint_turns_on_task(task, turns)
                msg_idx = _sync_endpoint_turns_to_conversation(task, turns)
                # 3) Pipelined per-turn auto-translate (parallel with the next
                #    phase), identical to the live path. Safety net at the end
                #    re-covers any turn missed here. No-op if autoTranslate OFF.
                _trigger_per_turn_auto_translate(task, msg, msg_idx)
            except Exception as e:
                logger.warning('[EndpointViaFlow] per-turn DB sync/translate failed '
                               '(non-fatal) task=%s: %s', tid, e)

    adapter = EndpointEventAdapter(emit=_emit_endpoint_msg)
    _adapter_ref['messages'] = adapter.messages

    # Surface raw engine progress too (loop/replan/guard) for diagnostics.
    def _on_event(ev):
        adapter.on_event(ev)

    status = 'done'
    stop_reason = 'completed'
    iterations = 0
    try:
        executor = FlowExecutor(
            defn,
            agent_runner=None,            # default SubAgent runner
            on_event=_on_event,
            abort_check=task.get('abort_event').is_set if task.get('abort_event') else None,
            max_iterations=max_iter,
            parent_task=task,
            all_tools=tool_list,
            model=model,
            project_path=project_path,
        )
        result = executor.run(initial_context=user_request)
        iterations = result.get('agents_run', 0)
        if not result.get('ok'):
            stop_reason = result.get('error') or 'failed'
        # Final assistant content = the flow's converged result.
        with task.get('content_lock', _NullLock()):
            task['content'] = result.get('final', '')
    except FlowExecutionError as e:
        status = 'error'
        stop_reason = f'structural: {e}'
        logger.error('[EndpointViaFlow] task=%s structural failure: %s', tid, e)
        task['content'] = ''
    except Exception as e:
        status = 'error'
        stop_reason = f'{type(e).__name__}: {e}'
        logger.error('[EndpointViaFlow] task=%s crashed: %s', tid, e, exc_info=True)
        task['content'] = ''

    # Endpoint turns the adapter produced (for DB persistence parity).
    # Final sync — captures any last turn whose emit raced the loop exit.
    task['_endpoint_turns'] = adapter.messages
    if adapter.messages:
        try:
            _store_endpoint_turns_on_task(task, adapter.messages)
            _sync_endpoint_turns_to_conversation(task, adapter.messages)
        except Exception as e:
            logger.warning('[EndpointViaFlow] final DB sync failed '
                           '(non-fatal) task=%s: %s', tid, e)
        # Safety-net auto-translate: re-covers any turn whose per-turn hook
        # missed (exception / msg_idx=None). Dedups against in-flight translate
        # tasks; no-op when conversation autoTranslate is OFF. Mirrors the live
        # path's _finalize → _trigger_endpoint_auto_translate.
        try:
            _trigger_endpoint_auto_translate(task, adapter.messages)
        except Exception as e:
            logger.warning('[EndpointViaFlow] safety-net auto-translate failed '
                           '(non-fatal) task=%s: %s', tid, e)
    task['_endpoint_phase'] = 'done'
    task['_endpoint_stop_reason'] = stop_reason
    task['status'] = status
    task['finishReason'] = 'stop'

    append_event(task, build_event(
        EventType.ENDPOINT_COMPLETE,
        totalIterations=iterations,
        reason=stop_reason,
        replanCount=0,
    ))
    done_evt = build_event(EventType.DONE, usage=task.get('usage', {}),
                           finishReason='stop', endpointReason=stop_reason)
    if task.get('model'):
        done_evt['model'] = task['model']
    append_event(task, done_evt)
    persist_task_result(task)

    audit_log('endpoint_via_flow_complete', task_id=task['id'],
              status=status, reason=stop_reason, iterations=iterations)
    logger.info('[EndpointViaFlow] task=%s DONE status=%s reason=%s',
                tid, status, stop_reason)


class _NullLock:
    """Context-manager no-op for tasks without a content_lock (defensive)."""
    def __enter__(self): return self
    def __exit__(self, *a): return False


__all__ = ['run_endpoint_via_flow', 'endpoint_via_flow_enabled']
