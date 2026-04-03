"""lib/swarm/integration.py — Glue between swarm system and existing task orchestrator.

This module:
  1. Provides ``execute_swarm_tool()`` — called by the executor when
     LLM invokes spawn_agents / check_agents / spawn_more_agents
  2. Manages the MasterOrchestrator lifecycle per-task
  3. Supports reactive mode — where the master LLM reviews results and
     can spawn additional agents dynamically
  4. Handles artifact tools when called outside sub-agent context
  5. Automatic TTL-based cleanup of stale sessions to prevent memory leaks

The design ensures the swarm is a drop-in tool for the existing
orchestrator loop: the LLM calls ``spawn_agents``, we block while
the swarm runs, and return a synthesis as the tool result.
"""

import threading
import time
import uuid
from collections.abc import Callable
from typing import Optional

from lib.log import get_logger
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentStatus, SubTaskSpec

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Session TTL Configuration
# ═══════════════════════════════════════════════════════════

SESSION_TTL_SECONDS = 1800  # 30 minutes — stale sessions are cleaned up after this
MAX_SESSIONS = 20  # Maximum concurrent sessions — oldest evicted when exceeded
_CLEANUP_INTERVAL = 300  # Background cleanup every 5 minutes

# ═══════════════════════════════════════════════════════════
#  Active Swarm Sessions — tracked per task
# ═══════════════════════════════════════════════════════════

# Key: task_id, Value: MasterOrchestrator instance
_active_sessions: dict[str, MasterOrchestrator] = {}
_session_timestamps: dict[str, float] = {}  # Key: task_id, Value: creation timestamp
_sessions_lock = threading.Lock()
_last_cleanup: float = 0.0


def _cleanup_stale_sessions():
    """Remove sessions older than SESSION_TTL_SECONDS and enforce MAX_SESSIONS.

    Called lazily from _get_session(), _set_session(), and execute_swarm_tool(),
    and periodically by the background cleanup timer.

    Note: This function must be called while holding _sessions_lock.
    """
    global _last_cleanup
    now = time.time()

    # Only run cleanup at most once per 60 seconds to avoid overhead
    if now - _last_cleanup < 60:
        return

    _last_cleanup = now

    # --- Phase 1: Remove TTL-expired sessions ---
    stale_ids = [
        task_id for task_id, ts in _session_timestamps.items()
        if now - ts > SESSION_TTL_SECONDS
    ]

    for task_id in stale_ids:
        session = _active_sessions.pop(task_id, None)
        _session_timestamps.pop(task_id, None)
        if session:
            logger.info('[Swarm:%s] Session expired after %ds TTL — cleaning up', task_id, SESSION_TTL_SECONDS)
            try:
                session.abort()
            except Exception as e:
                logger.debug('[Swarm:%s] Best-effort cleanup failed: %s', task_id, e, exc_info=True)

    # --- Phase 2: Enforce MAX_SESSIONS by evicting oldest ---
    if len(_active_sessions) > MAX_SESSIONS:
        sorted_ids = sorted(_session_timestamps, key=_session_timestamps.get)
        excess = len(_active_sessions) - MAX_SESSIONS
        for task_id in sorted_ids[:excess]:
            session = _active_sessions.pop(task_id, None)
            _session_timestamps.pop(task_id, None)
            if session:
                logger.warning('[Swarm:%s] Evicted (MAX_SESSIONS=%d exceeded)', task_id, MAX_SESSIONS)
                try:
                    session.abort()
                except Exception as e:
                    logger.debug('[Swarm:%s] Eviction abort failed: %s', task_id, e, exc_info=True)


def _background_cleanup():
    """Background timer callback — acquires lock and cleans stale sessions."""
    global _last_cleanup
    try:
        with _sessions_lock:
            # Reset _last_cleanup so the inner function actually runs
            _last_cleanup = 0.0
            _cleanup_stale_sessions()
    except Exception as e:
        logger.warning('[Swarm] Background cleanup error: %s', e, exc_info=True)
    finally:
        _start_cleanup_timer()


def _start_cleanup_timer():
    """Start (or restart) the background cleanup daemon timer."""
    global _cleanup_timer
    _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _background_cleanup)
    _cleanup_timer.daemon = True
    _cleanup_timer.start()


_cleanup_timer: threading.Timer | None = None
_start_cleanup_timer()  # Launch on module import


def _get_session(task_id: str) -> MasterOrchestrator | None:
    """Get the active swarm session for a task."""
    with _sessions_lock:
        _cleanup_stale_sessions()
        return _active_sessions.get(task_id)


def _set_session(task_id: str, session: MasterOrchestrator):
    """Register an active swarm session."""
    with _sessions_lock:
        _cleanup_stale_sessions()
        _active_sessions[task_id] = session
        _session_timestamps[task_id] = time.time()


def _remove_session(task_id: str):
    """Remove a completed swarm session."""
    with _sessions_lock:
        _active_sessions.pop(task_id, None)
        _session_timestamps.pop(task_id, None)


def get_active_session(task_id: str) -> MasterOrchestrator | None:
    """Public accessor for routes/other modules to check swarm status."""
    return _get_session(task_id)


def get_swarm_status(task_id: str) -> dict | None:
    """Return swarm status for a task, or None if no active swarm."""
    session = _get_session(task_id)
    if session is None:
        return None
    try:
        agents_info = []
        for agent_id, agent in getattr(session, '_agents', {}).items():
            agents_info.append({
                'id': agent_id,
                'role': getattr(agent, 'role', 'unknown'),
                'status': getattr(agent, 'status', 'unknown'),
                'task': getattr(agent, 'task_desc', ''),
            })
        return {
            'active': True,
            'task_id': task_id,
            'agents': agents_info,
            'agent_count': len(agents_info),
            'created_at': _session_timestamps.get(task_id, 0),
        }
    except Exception as e:
        logger.warning('[swarm] Error getting status for %s: %s', task_id, e, exc_info=True)
        return {'active': True, 'task_id': task_id, 'error': str(e)}


def abort_swarm(task_id: str) -> dict:
    """Abort a running swarm session."""
    session = _get_session(task_id)
    if session is None:
        return {'success': False, 'error': 'No active swarm for this task'}
    try:
        # Signal abort to master
        if hasattr(session, 'abort'):
            session.abort()
        _remove_session(task_id)
        logger.info('[swarm] Aborted swarm for task %s', task_id)
        return {'success': True, 'task_id': task_id}
    except Exception as e:
        logger.error('[swarm] Error aborting %s: %s', task_id, e, exc_info=True)
        _remove_session(task_id)
        return {'success': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  Main Entry Point — execute_swarm_tool
# ═══════════════════════════════════════════════════════════

def execute_swarm_tool(fn_name: str, fn_args: dict, task: dict = None,
                       *,
                       cfg: dict = None,
                       all_tools: list = None,
                       project_path: str = '',
                       project_enabled: bool = False,
                       model: str = '',
                       thinking_enabled: bool = False,
                       search_mode: str = 'multi',
                       abort_check: Callable | None = None,
                       on_event: Callable | None = None,
                       ) -> str:
    """Execute a swarm tool call and return the result string.

    Called by the task executor when it encounters a swarm tool name.

    Supports two calling conventions:
      1. From executor: execute_swarm_tool(fn_name, fn_args, task, model=..., ...)
      2. Full form: execute_swarm_tool(fn_name, fn_args, task=..., cfg=..., all_tools=...)

    Supported tools:
      • spawn_agents — initial swarm spawn (triggers full reactive cycle)
      • spawn_more_agents — add agents to running session
      • check_agents — query agent status
      • swarm_done — early termination signal
      • store_artifact / read_artifact / list_artifacts — artifact tools
    """
    # Lazy cleanup on every tool invocation
    with _sessions_lock:
        _cleanup_stale_sessions()

    task = task or {}
    all_tools = all_tools or []
    task_id = task.get('id', 'unknown')
    # Build a merged cfg from explicit kwargs + any provided cfg dict
    _cfg = dict(cfg or {})
    # Explicit kwargs override cfg dict values
    if model:
        _cfg['model'] = model
    if thinking_enabled:
        _cfg['thinking_enabled'] = thinking_enabled
    if search_mode:
        _cfg['search_mode'] = search_mode
    cfg = _cfg
    model = cfg.get('model', '')
    thinking_enabled = cfg.get('thinking_enabled', False)

    logger.info('[Swarm:%s] ===== execute_swarm_tool: fn=%s args_keys=%s =====',
                task_id, fn_name, list(fn_args.keys()))

    try:
        if fn_name == 'spawn_agents':
            return _handle_spawn_agents(
                fn_args, task_id=task_id, task=task, cfg=cfg,
                all_tools=all_tools, model=model,
                thinking_enabled=thinking_enabled,
                project_path=project_path,
                project_enabled=project_enabled,
                abort_check=abort_check,
                on_event=on_event,
            )

        elif fn_name == 'check_agents':
            return _handle_check_agents(task_id)

        elif fn_name == 'spawn_more_agents':
            return _handle_spawn_more_agents(
                fn_args, task_id=task_id, task=task, cfg=cfg,
                all_tools=all_tools, model=model,
                thinking_enabled=thinking_enabled,
                project_path=project_path,
                abort_check=abort_check,
                on_event=on_event,
            )

        elif fn_name == 'swarm_done':
            return _handle_swarm_done(fn_args, task_id)

        elif fn_name in ('store_artifact', 'read_artifact', 'list_artifacts'):
            return _handle_artifact_tool(fn_name, fn_args, task_id)

        else:
            return f'Unknown swarm tool: {fn_name}'

    except Exception as e:
        logger.error('[Swarm:%s] Tool %s error: %s', task_id, fn_name, e, exc_info=True)
        return f'Swarm tool error: {type(e).__name__}: {e}'


# ═══════════════════════════════════════════════════════════
#  spawn_agents — The Big One
# ═══════════════════════════════════════════════════════════

def _handle_spawn_agents(fn_args: dict, *, task_id: str, task: dict,
                         cfg: dict, all_tools: list,
                         model: str, thinking_enabled: bool,
                         project_path: str, project_enabled: bool,
                         abort_check: Callable | None,
                         on_event: Callable | None) -> str:
    """Handle the spawn_agents tool — create session and run reactive loop."""

    agents_data = fn_args.get('agents', [])
    if not agents_data:
        return 'Error: no agents specified'

    # Parse specs
    specs = []
    for agent_def in agents_data:
        spec = SubTaskSpec(
            role=agent_def.get('role', 'general'),
            objective=agent_def.get('objective', ''),
            context=agent_def.get('context', ''),
            depends_on=agent_def.get('depends_on', []),
            id=agent_def.get('id', str(uuid.uuid4())[:8]),
            max_retries=agent_def.get('max_retries', 1),
            model_override=agent_def.get('model_override', ''),
        )
        specs.append(spec)

    logger.info('[Swarm:%s] Spawning %d agents: %s',
                task_id, len(specs), [s.role + ':' + s.id for s in specs])

    # Emit structured SwarmEvent for spawning phase
    def _emit_structured(event_dict):
        """Emit both structured SwarmEvent and legacy format."""
        if on_event:
            on_event(event_dict)

    _emit_structured({
        'type': 'swarm_phase', 'phase': 'spawning',
        'content': (f'🚀 Spawning {len(specs)} agents:\n'
                    + '\n'.join(f'  • {s.role}: {s.objective[:50]}' for s in specs)),
        'agents': [
            {'agentId': s.id, 'role': s.role,
             'objective': s.objective[:200],
             'context': (s.context or '')[:80] if hasattr(s, 'context') else '',
             'depends_on': list(s.depends_on) if hasattr(s, 'depends_on') and s.depends_on else []}
            for s in specs
        ],
    })

    # Create orchestrator — pass _emit_structured as progress callback
    # so all sub-agent events are emitted as structured SwarmEvents
    conv_id = task.get('convId', cfg.get('convId', ''))
    master = MasterOrchestrator(
        task_id=task_id,
        conv_id=conv_id,
        specs=specs,
        project_path=project_path,
        project_enabled=project_enabled,
        model=model,
        thinking_enabled=thinking_enabled,
        search_mode=cfg.get('search_mode', 'multi'),
        on_progress=_emit_structured,
        abort_check=abort_check,
        all_tools=all_tools,
        max_parallel=8,
        max_reactive_rounds=cfg.get('max_reactive_rounds', 3),
        max_retries=cfg.get('max_retries', 1),
    )

    _set_session(task_id, master)
    spawn_start = time.time()

    try:
        # Recover the original user query for the reactive review prompt
        original_query = _extract_user_query(task)
        logger.info('[Swarm:%s] Original query (first 200 chars): %s',
                    task_id, original_query[:200])

        # Run in reactive mode — master reviews results and can spawn more
        logger.info('[Swarm:%s] ── Starting run_reactive ──', task_id)
        final_answer = master.run_reactive(original_query=original_query)
        spawn_elapsed = time.time() - spawn_start

        # Log completion summary
        n_agents = len(master._results)
        n_ok = sum(1 for _, r in master._results if r.status == 'completed')
        n_fail = n_agents - n_ok
        logger.info('[Swarm:%s] ── run_reactive DONE in %.1fs — agents=%d ok=%d fail=%d answer_len=%d ──',
                    task_id, spawn_elapsed, n_agents, n_ok, n_fail, len(final_answer or ''))

        # NOTE: Do NOT emit a second swarm_phase:complete here — the
        # MasterOrchestrator.run_reactive() already emits one with full
        # agent stats.  Emitting again would overwrite the stats with
        # empty data on the frontend.

        return final_answer

    except Exception as e:
        spawn_elapsed = time.time() - spawn_start
        logger.error('[Swarm:%s] run_reactive FAILED after %.1fs: %s',
                     task_id, spawn_elapsed, e, exc_info=True)
        raise

    finally:
        _remove_session(task_id)


def _extract_user_query(task: dict) -> str:
    """Extract the original user query from the task."""
    # Try to get from messages
    messages = task.get('messages', [])
    for msg in reversed(messages):
        if msg.get('role') == 'user':
            content = msg.get('content', '')
            if isinstance(content, list):
                # Multi-part content
                text_parts = [p.get('text', '') for p in content if p.get('type') == 'text']
                return ' '.join(text_parts)
            return content
    # Fallback to description
    return task.get('description', task.get('id', ''))


# ═══════════════════════════════════════════════════════════
#  spawn_more_agents — Add agents to running session
# ═══════════════════════════════════════════════════════════

def _handle_spawn_more_agents(fn_args: dict, *, task_id: str, task: dict,
                              cfg: dict, all_tools: list,
                              model: str, thinking_enabled: bool,
                              project_path: str,
                              abort_check: Callable | None,
                              on_event: Callable | None) -> str:
    """Handle spawn_more_agents — add agents to an existing session.

    If called outside the reactive loop (e.g. the main orchestrator decides
    to spawn more), we either add to the existing session or create a new one.
    """
    session = _get_session(task_id)
    agents_data = fn_args.get('agents', [])
    reason = fn_args.get('reason', '')

    if not agents_data:
        return 'Error: no agents specified'

    new_specs = []
    for agent_def in agents_data:
        spec = SubTaskSpec(
            role=agent_def.get('role', 'general'),
            objective=agent_def.get('objective', ''),
            context=agent_def.get('context', ''),
            id=agent_def.get('id', str(uuid.uuid4())[:8]),
            max_retries=1,
        )
        new_specs.append(spec)

    if session:
        # Add to existing session
        logger.info('[Swarm:%s] Adding %d agents to existing session (reason: %s): %s',
                    task_id, len(new_specs), reason,
                    [s.role + ':' + s.id for s in new_specs])
        session._execute_additional_specs(new_specs)

        # Build result summary
        results_summary = []
        for spec, result in session._results[-len(new_specs):]:
            status = '✅' if result.status == SubAgentStatus.COMPLETED.value else '❌'
            results_summary.append(
                f'{status} [{spec.role}] {spec.objective[:60]}: '
                f'{result.final_answer[:200] if result.final_answer else result.error_message[:200]}'
            )
        return (f'Spawned {len(new_specs)} additional agents (reason: {reason}).\n\n'
                f'Results:\n' + '\n'.join(results_summary))
    else:
        # No existing session — treat as a new spawn
        return _handle_spawn_agents(
            {'agents': agents_data},
            task_id=task_id, task=task, cfg=cfg,
            all_tools=all_tools, model=model,
            thinking_enabled=thinking_enabled,
            project_path=project_path, project_enabled=bool(project_path),
            abort_check=abort_check, on_event=on_event,
        )


# ═══════════════════════════════════════════════════════════
#  check_agents — Query agent status
# ═══════════════════════════════════════════════════════════

def _handle_check_agents(task_id: str) -> str:
    """Return status of all sub-agents with dashboard summary."""
    session = _get_session(task_id)
    if not session:
        return 'No active swarm session for this task.'

    status = session.get_status()
    lines = ['# Sub-Agent Status\n']

    # Counters for dashboard summary
    total = len(status)
    completed = 0
    running = 0
    failed = 0
    pending = 0

    for sid, info in status.items():
        agent_status = info.get('status', '')
        icon = {'completed': '✅', 'running': '🔄', 'failed': '❌',
                'pending': '⏳', 'cancelled': '🚫'}.get(agent_status, '❓')
        lines.append(
            f'{icon} [{info.get("role", "?")}] {info.get("objective", "")[:60]} '
            f'— {agent_status} '
            f'(round {info.get("round", 0)}/{info.get("max_rounds", 0)})'
        )

        # Tally for dashboard
        if agent_status == 'completed':
            completed += 1
        elif agent_status == 'running':
            running += 1
        elif agent_status == 'failed':
            failed += 1
        elif agent_status in ('pending', 'cancelled'):
            pending += 1

    # Dashboard summary
    lines.insert(1, f'📊 Dashboard: {total} total | '
                    f'{completed} completed | {running} running | '
                    f'{failed} failed | {pending} pending\n')

    # Session uptime from timestamps
    with _sessions_lock:
        ts = _session_timestamps.get(task_id)
    if ts:
        elapsed = time.time() - ts
        mins, secs = divmod(int(elapsed), 60)
        lines.append(f'\n⏱️ Session uptime: {mins}m {secs}s')

    # Also show artifact store info
    artifacts = session.get_artifacts()
    if artifacts:
        lines.append(f'\n# Shared Artifacts ({len(artifacts)} items)')
        for key in artifacts:
            lines.append(f'  • {key}')

    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════
#  swarm_done — Early termination
# ═══════════════════════════════════════════════════════════

def _handle_swarm_done(fn_args: dict, task_id: str) -> str:
    """Handle swarm_done — signal that the main orchestrator is satisfied."""
    summary = fn_args.get('summary', 'Swarm completed')
    session = _get_session(task_id)
    if session:
        session.abort()
    return f'Swarm session ended. Summary: {summary}'


# ═══════════════════════════════════════════════════════════
#  Artifact tools (when called from main orchestrator, not sub-agent)
# ═══════════════════════════════════════════════════════════

def _handle_artifact_tool(fn_name: str, fn_args: dict, task_id: str) -> str:
    """Handle artifact tools from the main orchestrator level."""
    session = _get_session(task_id)
    if not session:
        return 'No active swarm session — artifacts not available.'

    store = session.artifact_store

    if fn_name == 'store_artifact':
        key = fn_args.get('key', '')
        content = fn_args.get('content', '')
        if not key:
            return 'Error: key is required'
        store.put(key, content, writer_id='orchestrator')
        return f'Stored artifact "{key}" ({len(content):,} chars)'

    elif fn_name == 'read_artifact':
        key = fn_args.get('key', '')
        if not key:
            return 'Error: key is required'
        content = store.get(key)
        if not content:
            available = store.list_keys()
            return f'Artifact "{key}" not found. Available: {", ".join(available) or "(none)"}'
        return content

    elif fn_name == 'list_artifacts':
        return store.summary()

    return f'Unknown artifact tool: {fn_name}'

