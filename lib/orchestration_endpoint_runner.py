"""lib/orchestration_endpoint_runner.py — Chat modes via FlowExecutor.

The convergence point where endpoint mode, autopilot mode, AND arbitrary
user-authored Studio flows all run through ONE engine
(:class:`lib.orchestration_engine.FlowExecutor`) and ONE translator
(:class:`lib.orchestration_endpoint_adapter.EndpointEventAdapter` → endpoint
SSE/message schema, so the existing frontend renders every mode unchanged).

Entry points (all share :func:`_run_flow_as_endpoint_task`):
  * :func:`run_endpoint_via_flow`  — canonical endpoint graph.
  * :func:`run_autopilot_via_flow` — canonical autopilot (worker ⇄ VU) graph.
  * :func:`run_flow_via_chat`      — a user-SELECTED flow (inline / builtin /
    stored id) resolved by :func:`resolve_chat_flow_definition`.

``routes/chat.py`` calls :func:`resolve_chat_flow_entry` to pick one (or
``None`` → fall back to the live path / a normal task).

Flags (each default OFF, symmetric):
    TOFU_ENDPOINT_VIA_FLOW=1    → endpoint mode uses this engine path
    TOFU_AUTOPILOT_VIA_FLOW=1   → autopilot mode uses this engine path
    (a user-selected flow is ALWAYS honored — the selection is the opt-in)

The live ``lib/tasks_pkg/endpoint.py`` / ``autopilot.py`` paths remain the
default + authoritative until each flagged path is validated on real tasks.
"""

from __future__ import annotations


from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def _flag_on(name: str) -> bool:
    val = getenv_compat(name, default='0').strip().lower()
    return val in ('1', 'true', 'yes', 'on')


def endpoint_via_flow_enabled() -> bool:
    """True iff the flagged FlowExecutor endpoint path is opted in.

    Default OFF — only ``TOFU_ENDPOINT_VIA_FLOW=1`` (or ``true``/``yes``)
    enables it. Anything else (unset, ``0``, garbage) uses the live path.
    """
    return _flag_on('TOFU_ENDPOINT_VIA_FLOW')


def autopilot_via_flow_enabled() -> bool:
    """True iff the flagged FlowExecutor autopilot path is opted in.

    Symmetric to :func:`endpoint_via_flow_enabled`. Default OFF — only
    ``TOFU_AUTOPILOT_VIA_FLOW=1`` reroutes autopilot mode through the
    unified engine instead of the live ``lib/tasks_pkg/autopilot.py``
    virtual-user follow-up loop.
    """
    return _flag_on('TOFU_AUTOPILOT_VIA_FLOW')


def _load_stored_definition(flow_id: str) -> dict | None:
    """Load a user-authored flow definition from the orchestrations store.

    Reads ``data/config/orchestrations.json`` directly via the shared
    json_store (NOT through the route module, to avoid a routes→lib import
    cycle). Returns the inner ``definition`` dict or ``None``.
    """
    try:
        from lib.config_dir import config_path
        from lib.json_store import read_json
        entries = read_json(config_path('orchestrations.json'), default=[])
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict) and e.get('id') == flow_id:
                    d = e.get('definition')
                    return d if isinstance(d, dict) else None
    except Exception as e:
        logger.warning('[FlowChat] failed to load stored flow %r: %s', flow_id, e)
    return None


def resolve_chat_flow_definition(config: dict) -> tuple[dict | None, str]:
    """Resolve a chat task's selected flow into a definition + source label.

    Precedence: inline ``flowDefinition`` → ``flowBuiltin`` name
    (endpoint|autopilot) → stored ``flowId``. Returns ``(defn, source)`` or
    ``(None, '')`` when no flow is selected.
    """
    from lib.orchestration import (
        build_autopilot_definition, build_endpoint_definition,
    )

    defn = config.get('flowDefinition')
    if isinstance(defn, dict) and defn.get('nodes'):
        return defn, 'inline'

    name = config.get('flowBuiltin')
    if isinstance(name, str) and name:
        builder = {'endpoint': build_endpoint_definition,
                   'autopilot': build_autopilot_definition}.get(name)
        if builder is not None:
            return builder(), f'builtin:{name}'
        logger.warning('[FlowChat] unknown flowBuiltin %r — ignoring', name)

    fid = config.get('flowId')
    if isinstance(fid, str) and fid:
        d = _load_stored_definition(fid)
        if d is not None:
            return d, f'stored:{fid}'
        logger.warning('[FlowChat] flowId %r not found in store — ignoring', fid)

    return None, ''


def resolve_chat_flow_entry(config: dict):
    """Pick the FlowExecutor entry point for a chat task, or ``None``.

    Encapsulates ALL the dispatch/flag logic so ``routes/chat.py`` stays a
    thin switch:

      1. An explicit flow selection (``flowDefinition`` / ``flowBuiltin`` /
         ``flowId``) → :func:`run_flow_via_chat` (a NEW capability — honored
         whenever the user selects a flow; no flag, the selection is the
         opt-in).
      2. ``endpointMode`` + ``TOFU_ENDPOINT_VIA_FLOW`` → :func:`run_endpoint_via_flow`.
      3. ``autopilot`` + ``TOFU_AUTOPILOT_VIA_FLOW`` → :func:`run_autopilot_via_flow`.

    Returns a ``callable(task)`` or ``None`` (caller falls back to the live
    endpoint path or a normal task).
    """
    config = config or {}
    if (config.get('flowDefinition') or config.get('flowBuiltin')
            or config.get('flowId')):
        return run_flow_via_chat
    if config.get('endpointMode') and endpoint_via_flow_enabled():
        return run_endpoint_via_flow
    if config.get('autopilot') and autopilot_via_flow_enabled():
        return run_autopilot_via_flow
    return None


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
            conv_id=task.get('convId', ''),
        )
        return tool_list, mcfg.get('model', ''), mcfg.get('project_path', '')
    except Exception as e:
        logger.error('[FlowChat] tool assembly failed: %s', e, exc_info=True)
        return [], '', ''


def run_endpoint_via_flow(task: dict):
    """Run endpoint mode through FlowExecutor (flagged path).

    Thin wrapper over :func:`_run_flow_as_endpoint_task` with the canonical
    endpoint graph (``build_endpoint_definition``).
    """
    from lib.orchestration import build_endpoint_definition
    cfg = task.get('config') or {}
    max_iter = int(cfg.get('endpointMaxIterations') or 10)
    _run_flow_as_endpoint_task(
        task, build_endpoint_definition(max_iterations=max_iter),
        label='endpoint', max_iter=max_iter)


def run_autopilot_via_flow(task: dict):
    """Run autopilot mode through FlowExecutor (flagged path).

    Symmetric to :func:`run_endpoint_via_flow`: runs the canonical autopilot
    graph (``build_autopilot_definition`` — worker ⇄ virtual_user loop) on
    the unified engine. The virtual_user's turns surface as user-side
    messages via the adapter's ``emits`` handling, so the existing chat UI
    renders the synthetic-user replies with no frontend change.
    """
    from lib.orchestration import build_autopilot_definition
    cfg = task.get('config') or {}
    max_iter = int(cfg.get('autopilotMaxIterations')
                   or cfg.get('endpointMaxIterations') or 12)
    _run_flow_as_endpoint_task(
        task, build_autopilot_definition(max_iterations=max_iter),
        label='autopilot', max_iter=max_iter)


def run_flow_via_chat(task: dict):
    """Run a USER-SELECTED orchestration flow as a chat task.

    The flow is resolved from the task config (inline ``flowDefinition`` /
    ``flowBuiltin`` name / stored ``flowId``) by
    :func:`resolve_chat_flow_definition`. This is the capability that lets a
    flow authored in the Studio drive a real conversation — the final
    convergence point where endpoint, autopilot, AND arbitrary custom flows
    all run through one engine + one adapter.
    """
    cfg = task.get('config') or {}
    defn, source = resolve_chat_flow_definition(cfg)
    if defn is None:
        # Should not happen (resolve_chat_flow_entry gated on selection) but
        # be defensive: fall back to the canonical endpoint flow.
        from lib.orchestration import build_endpoint_definition
        defn, source = build_endpoint_definition(), 'fallback:endpoint'
        logger.warning('[FlowChat] task=%s no flow resolved — using endpoint '
                       'fallback', task['id'][:8])
    max_iter = int(cfg.get('endpointMaxIterations') or 12)
    _run_flow_as_endpoint_task(task, defn, label=f'flow({source})',
                               max_iter=max_iter)


def _run_flow_as_endpoint_task(task: dict, defn: dict, *, label: str,
                               max_iter: int):
    """Execute *defn* on FlowExecutor with the endpoint task/SSE/DB contract.

    The shared core behind endpoint / autopilot / custom-flow chat entry
    points. Streams events via ``append_event``, translates engine events to
    the endpoint MESSAGE schema via :class:`EndpointEventAdapter` (so the
    frontend renders unchanged), and persists each turn through the LIVE
    endpoint DB-sync + auto-translate functions for reload/poll parity.
    """
    from lib.tasks_pkg.manager import append_event, persist_task_result
    from lib.agent_core.events import EventType, build_event
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
        raise ValueError("_run_flow_as_endpoint_task called with a task missing 'id'")
    tid = task['id'][:8]
    # Derive the opening phase from the flow's ACTUAL first role node instead
    # of always claiming 'planning'. A plannerless flow (e.g. autopilot:
    # worker→vu) must NOT advertise endpointPhase='planning' or the frontend
    # stands up a Planner bubble that never receives content (hangs at
    # "Waiting…"). 'planning' → Planner bubble, 'working' → Worker, etc.
    from lib.orchestration import initial_phase_for_flow
    initial_phase = initial_phase_for_flow(defn)
    task['endpoint_mode'] = True
    task['_endpoint_phase'] = initial_phase
    task['_endpoint_iteration'] = 0
    task['_endpoint_via_flow'] = True
    task['_flow_label'] = label

    audit_log('flow_via_chat_start', task_id=task['id'], flow=label)
    logger.info('[FlowChat] task=%s START label=%s (FlowExecutor path)', tid, label)

    tool_list, model, project_path = _build_tools_for_task(task)
    user_request = _extract_user_request(task)

    # Adapter has TWO output channels:
    #   • on_stream → LIVE SSE events (endpoint_iteration / delta /
    #     endpoint_planner_done / endpoint_critic_msg) forwarded verbatim to
    #     the task stream so the streaming UI renders tokens AS THEY ARRIVE.
    #   • emit      → endpoint-shaped MESSAGE dicts, fired once per COMPLETED
    #     turn → incremental DB persistence (survives SSE drop / reload).
    # ``adapter.messages`` is the running list of endpoint turns; captured by
    # closure so the DB-sync callback can persist the full snapshot per turn.
    _adapter_ref = {}

    def _stream_endpoint_event(ev: dict):
        # Forward the adapter's translated SSE event straight to the task
        # stream. ``ev`` already uses the wire vocabulary the frontend
        # handles (its 'type' is one of the registered endpoint/content
        # events); append_event preserves order + persists for replay.
        append_event(task, ev)

    def _persist_endpoint_msg(msg: dict):
        # Persist incrementally through the LIVE endpoint sync path, so a turn
        # survives even if SSE drops mid-run (identical to endpoint.py). The
        # sync returns the absolute DB index of the LAST turn (== msg), which
        # the per-turn auto-translate trigger needs.
        turns = _adapter_ref.get('messages') or []
        if turns:
            try:
                _store_endpoint_turns_on_task(task, turns)
                msg_idx = _sync_endpoint_turns_to_conversation(task, turns)
                # Pipelined per-turn auto-translate (parallel with the next
                # phase), identical to the live path. Safety net at the end
                # re-covers any turn missed here. No-op if autoTranslate OFF.
                _trigger_per_turn_auto_translate(task, msg, msg_idx)
            except Exception as e:
                logger.warning('[FlowChat] per-turn DB sync/translate failed '
                               '(non-fatal) task=%s: %s', tid, e)

    adapter = EndpointEventAdapter(emit=_persist_endpoint_msg,
                                   on_stream=_stream_endpoint_event)
    _adapter_ref['messages'] = adapter.messages

    # Surface raw engine progress too (loop/replan/guard) for diagnostics.
    def _on_event(ev):
        adapter.on_event(ev)

    status = 'done'
    stop_reason = 'completed'
    iterations = 0
    executor = None
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
        logger.error('[FlowChat] task=%s structural failure: %s', tid, e)
        task['content'] = ''
    except Exception as e:
        status = 'error'
        stop_reason = f'{type(e).__name__}: {e}'
        logger.error('[FlowChat] task=%s crashed: %s', tid, e, exc_info=True)
        task['content'] = ''
    # Per-node run trace (resolved brief + bounded I/O per node) — for the
    # canvas/inspector overlay served via /api/v1/chat/flow-trace/<task>.
    # Read from the executor so a PARTIAL trace survives a mid-run crash.
    if executor is not None:
        try:
            task['_flow_trace'] = executor.trace
        except Exception as _te:
            logger.debug('[FlowChat] trace capture failed task=%s: %s', tid, _te)
            task.setdefault('_flow_trace', [])
    else:
        task.setdefault('_flow_trace', [])

    # Endpoint turns the adapter produced (for DB persistence parity).
    # Final sync — captures any last turn whose emit raced the loop exit.
    task['_endpoint_turns'] = adapter.messages
    if adapter.messages:
        try:
            _store_endpoint_turns_on_task(task, adapter.messages)
            _sync_endpoint_turns_to_conversation(task, adapter.messages)
        except Exception as e:
            logger.warning('[FlowChat] final DB sync failed '
                           '(non-fatal) task=%s: %s', tid, e)
        # Safety-net auto-translate: re-covers any turn whose per-turn hook
        # missed (exception / msg_idx=None). Dedups against in-flight translate
        # tasks; no-op when conversation autoTranslate is OFF. Mirrors the live
        # path's _finalize → _trigger_endpoint_auto_translate.
        try:
            _trigger_endpoint_auto_translate(task, adapter.messages)
        except Exception as e:
            logger.warning('[FlowChat] safety-net auto-translate failed '
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

    audit_log('flow_via_chat_complete', task_id=task['id'], flow=label,
              status=status, reason=stop_reason, iterations=iterations)
    logger.info('[FlowChat] task=%s label=%s DONE status=%s reason=%s',
                tid, label, status, stop_reason)


class _NullLock:
    """Context-manager no-op for tasks without a content_lock (defensive)."""
    def __enter__(self): return self
    def __exit__(self, *a): return False


__all__ = [
    'run_endpoint_via_flow', 'endpoint_via_flow_enabled',
    'run_autopilot_via_flow', 'autopilot_via_flow_enabled',
    'run_flow_via_chat', 'resolve_chat_flow_definition', 'resolve_chat_flow_entry',
]
