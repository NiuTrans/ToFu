"""lib/swarm/integration.py — Glue between async swarm and the task orchestrator.

Routes the four swarm-control tools the master LLM may call:

  * ``spawn_agents``      — fire-and-forget; returns a handle dict
  * ``await_agents``      — blocking wait (capped at 120 s)
  * ``get_agent_result``  — pull one agent's full final answer
  * artifact tools (``store_artifact`` / ``read_artifact`` / ``list_artifacts``)
                          — proxied to the live session's ArtifactStore

There is **no** synchronous "run swarm and return synthesised answer" path
anymore. The async swarm handle is a JSON object the LLM sees as the tool
result; sub-agent completions arrive on subsequent turns as auto-injected
``<swarm-update>`` user messages (see ``lib.agent_inbox`` and the
orchestrator's between-round drain hook).
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable

from lib import agent_inbox
from lib.log import get_logger
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Session bookkeeping
# ═══════════════════════════════════════════════════════════

#: Sessions older than this are auto-aborted/evicted.
SESSION_TTL_SECONDS = 1800
#: Concurrent session ceiling. Oldest evicted past the ceiling.
MAX_SESSIONS = 20
#: Background cleanup tick.
_CLEANUP_INTERVAL = 300

#: Output dir override — falls back to ``./data/swarm`` when unset.
SWARM_OUTPUT_DIR = os.environ.get('TOFU_SWARM_OUTPUT_DIR', '')
#: Hard-cap how long ``await_agents`` may block. The model can ask for
#: up to 120 s, beyond which we degrade to "still running" and let the
#: main agent move on rather than freeze the UI for 5 minutes.
AWAIT_AGENTS_HARD_CAP_SEC = 120

_active_sessions: dict[str, MasterOrchestrator] = {}
_session_timestamps: dict[str, float] = {}
_sessions_lock = threading.Lock()
_last_cleanup: float = 0.0
_cleanup_timer: threading.Timer | None = None


# ── Output dir resolution ────────────────────────────────

def _resolve_output_dir(task_id: str) -> str:
    """Return absolute path to ``<base>/<task_id>/`` for sub-agent log streams."""
    base = SWARM_OUTPUT_DIR or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'data', 'swarm',
    )
    return os.path.join(base, task_id)


# ── Cleanup ──────────────────────────────────────────────

def _task_is_live(task_id: str) -> bool:
    """True if the owning main task is still running.

    A swarm session's lifetime is bounded by its parent task (see
    ``MasterOrchestrator`` docstring). TTL eviction exists only to reap
    sessions whose parent has gone away without an explicit cleanup — it
    must NOT kill the swarm of a long-running task that merely parked a
    wave >30 min ago and is still working. We read the chat task registry
    by-key (a plain dict read, safe under the GIL — no lock needed for a
    best-effort heuristic) and treat anything not in a terminal state as
    live. Import is lazy + guarded so a missing/renamed registry never
    breaks cleanup.
    """
    try:
        from lib.tasks_pkg.manager import tasks as _chat_tasks
        t = _chat_tasks.get(task_id)
        if t is None:
            return False
        return t.get('status') not in ('done', 'error', 'aborted')
    except Exception as e:
        logger.debug('[Swarm] task liveness check failed for %s: %s', task_id, e)
        return False


def _cleanup_stale_sessions():
    """Drop sessions past TTL or above MAX_SESSIONS. Caller must hold lock."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 60:
        return
    _last_cleanup = now

    stale_ids = [
        tid for tid, ts in _session_timestamps.items()
        if now - ts > SESSION_TTL_SECONDS and not _task_is_live(tid)
    ]
    for tid in stale_ids:
        session = _active_sessions.pop(tid, None)
        _session_timestamps.pop(tid, None)
        agent_inbox.clear(tid)
        if session:
            logger.info('[Swarm:%s] Session expired after %ds TTL — cleaning up',
                        tid, SESSION_TTL_SECONDS)
            try:
                session.abort()
            except Exception as e:
                logger.debug('[Swarm:%s] cleanup abort failed: %s', tid, e, exc_info=True)

    if len(_active_sessions) > MAX_SESSIONS:
        sorted_ids = sorted(_session_timestamps, key=_session_timestamps.get)
        excess = len(_active_sessions) - MAX_SESSIONS
        for tid in sorted_ids[:excess]:
            session = _active_sessions.pop(tid, None)
            _session_timestamps.pop(tid, None)
            agent_inbox.clear(tid)
            if session:
                logger.warning('[Swarm:%s] Evicted (MAX_SESSIONS=%d exceeded)',
                               tid, MAX_SESSIONS)
                try:
                    session.abort()
                except Exception as e:
                    logger.debug('[Swarm:%s] eviction abort failed: %s',
                                 tid, e, exc_info=True)


def _background_cleanup():
    global _last_cleanup
    try:
        with _sessions_lock:
            _last_cleanup = 0.0
            _cleanup_stale_sessions()
    except Exception as e:
        logger.warning('[Swarm] Background cleanup error: %s', e, exc_info=True)
    finally:
        _start_cleanup_timer()


def _start_cleanup_timer():
    global _cleanup_timer
    _cleanup_timer = threading.Timer(_CLEANUP_INTERVAL, _background_cleanup)
    _cleanup_timer.daemon = True
    _cleanup_timer.start()


_start_cleanup_timer()  # launch on module import


# ── Session getters / setters ────────────────────────────

def _get_session(task_id: str) -> MasterOrchestrator | None:
    with _sessions_lock:
        _cleanup_stale_sessions()
        return _active_sessions.get(task_id)


def _set_session(task_id: str, session: MasterOrchestrator):
    with _sessions_lock:
        _cleanup_stale_sessions()
        _active_sessions[task_id] = session
        _session_timestamps[task_id] = time.time()


def _remove_session(task_id: str):
    with _sessions_lock:
        _active_sessions.pop(task_id, None)
        _session_timestamps.pop(task_id, None)
    agent_inbox.clear(task_id)


def get_active_session(task_id: str) -> MasterOrchestrator | None:
    """Public accessor for routes / orchestrator to inspect a live swarm."""
    return _get_session(task_id)


def get_swarm_status(task_id: str) -> dict | None:
    """Return swarm status for a task, or None if no active swarm."""
    session = _get_session(task_id)
    if session is None:
        return None
    try:
        agents_info = []
        for sid, info in session.get_status().items():
            agents_info.append({'id': sid, **info})
        return {
            'active':     not session.is_terminated,
            'task_id':    task_id,
            'agents':     agents_info,
            'agent_count': len(agents_info),
            'pending':    session.pending_count,
            'running':    session.running_count,
            'completed':  session.completed_count,
            'created_at': _session_timestamps.get(task_id, 0),
        }
    except Exception as e:
        logger.warning('[swarm] Error getting status for %s: %s',
                       task_id, e, exc_info=True)
        return {'active': True, 'task_id': task_id, 'error': str(e)}


def abort_swarm(task_id: str) -> dict:
    """Abort a running swarm session (used by routes/api_v1/swarm)."""
    session = _get_session(task_id)
    if session is None:
        return {'success': False, 'error': 'No active swarm for this task'}
    try:
        session.abort()
        _remove_session(task_id)
        logger.info('[swarm] Aborted swarm for task %s', task_id)
        return {'success': True, 'task_id': task_id}
    except Exception as e:
        logger.error('[swarm] Error aborting %s: %s', task_id, e, exc_info=True)
        _remove_session(task_id)
        return {'success': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════
#  Tool dispatch
# ═══════════════════════════════════════════════════════════

def execute_swarm_tool(fn_name: str, fn_args: dict, task: dict | None = None,
                       *,
                       cfg: dict | None = None,
                       all_tools: list | None = None,
                       project_path: str = '',
                       project_enabled: bool = False,
                       model: str = '',
                       thinking_enabled: bool = False,
                       search_mode: str = 'multi',
                       abort_check: Callable | None = None,
                       on_event: Callable | None = None,
                       ) -> str:
    """Dispatch one swarm tool call.

    Returns a string — either a JSON-encoded handle/result dict (for
    ``spawn_agents`` / ``await_agents`` / ``get_agent_result``) or a plain
    text body (for the artifact tools).
    """
    with _sessions_lock:
        _cleanup_stale_sessions()

    task = task or {}
    all_tools = all_tools or []
    task_id = task.get('id', 'unknown')
    cfg = dict(cfg or {})
    if model:
        cfg['model'] = model
    if thinking_enabled:
        cfg['thinking_enabled'] = thinking_enabled
    if search_mode:
        cfg['search_mode'] = search_mode
    model = cfg.get('model', '')
    thinking_enabled = cfg.get('thinking_enabled', False)

    logger.info('[Swarm:%s] tool=%s args_keys=%s', task_id, fn_name, list(fn_args.keys()))

    try:
        if fn_name == 'spawn_agents':
            return _handle_spawn_agents(
                fn_args, task_id=task_id, task=task, cfg=cfg,
                all_tools=all_tools, model=model,
                thinking_enabled=thinking_enabled,
                project_path=project_path,
                abort_check=abort_check, on_event=on_event,
            )

        if fn_name == 'await_agents':
            return _handle_await_agents(fn_args, task_id=task_id)

        if fn_name == 'get_agent_result':
            return _handle_get_agent_result(fn_args, task_id=task_id)

        if fn_name in ('store_artifact', 'read_artifact', 'list_artifacts'):
            return _handle_artifact_tool(fn_name, fn_args, task_id)

        return f'Unknown swarm tool: {fn_name}'

    except Exception as e:
        logger.error('[Swarm:%s] Tool %s error: %s', task_id, fn_name, e, exc_info=True)
        return f'Swarm tool error: {type(e).__name__}: {e}'


# ═══════════════════════════════════════════════════════════
#  spawn_agents — async; returns handle immediately
# ═══════════════════════════════════════════════════════════

def _handle_spawn_agents(fn_args: dict, *,
                         task_id: str,
                         task: dict,
                         cfg: dict,
                         all_tools: list,
                         model: str,
                         thinking_enabled: bool,
                         project_path: str,
                         abort_check: Callable | None,
                         on_event: Callable | None) -> str:
    agents_data = fn_args.get('agents') or []
    if not agents_data:
        return json.dumps({'error': 'no agents specified', 'status': 'error'})

    specs: list[SubTaskSpec] = []
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

    # If a session already exists for this task, ADD to it instead of
    # creating a fresh one. This is how the main agent re-uses the same
    # swarm to launch a follow-up wave (replaces legacy
    # ``spawn_more_agents``).  If the existing session has already
    # terminated, drop it so we fall through to the "new session" branch
    # — otherwise the user can never spawn again on the same task_id
    # after the first wave completes.
    session = _get_session(task_id)
    if session is not None and session.is_terminated:
        logger.info('[Swarm:%s] previous session terminated — recycling task_id', task_id)
        _remove_session(task_id)
        session = None

    swarm_id_existing = bool(session)
    output_dir = _resolve_output_dir(task_id)

    def _emit(ev: dict):
        if on_event:
            on_event(ev)

    deduped_dropped: list[SubTaskSpec] = []
    if session is None:
        conv_id = task.get('convId', cfg.get('convId', ''))
        # Forward only routing-relevant parent config (browserClientId is
        # the main one — per-device playwright pool selection).  Other
        # cfg fields like model / thinking are already passed via direct
        # kwargs above, so no need to duplicate them into parent_config.
        parent_cfg = {}
        for _k in ('browserClientId',):
            if _k in (task.get('config') or {}):
                parent_cfg[_k] = task['config'][_k]
            elif _k in cfg:
                parent_cfg[_k] = cfg[_k]

        session = MasterOrchestrator(
            task_id=task_id,
            conv_id=conv_id,
            specs=specs,
            project_path=project_path,
            model=model,
            thinking_enabled=thinking_enabled,
            search_mode=cfg.get('search_mode', 'multi'),
            on_progress=_emit,
            abort_check=abort_check,
            all_tools=all_tools,
            max_parallel=cfg.get('max_parallel', 8),
            max_retries=cfg.get('max_retries', 1),
            output_dir=output_dir,
            parent_config=parent_cfg,
        )
        _set_session(task_id, session)

        try:
            session.run_in_background()
        except ValueError as e:
            # Cycle detection raised by add_specs
            logger.warning('[Swarm:%s] spawn_agents rejected: %s',
                           task_id, e)
            _remove_session(task_id)
            return json.dumps({
                'status': 'error',
                'error':  str(e),
                'message': (
                    'Cycle detected in agent dependencies. Re-issue '
                    'spawn_agents without circular depends_on entries.'),
            })
        # On a fresh session, run_in_background's add_specs accepted everything
        # (no prior state to dedup against). ``specs`` is already correct.
    else:
        # Existing live session — inject new specs into the running scheduler.
        try:
            accepted_specs = session._scheduler.add_specs(specs)  # type: ignore[union-attr]
        except ValueError as e:
            logger.warning('[Swarm:%s] follow-up spawn rejected: %s',
                           task_id, e)
            return json.dumps({
                'status': 'error',
                'error':  str(e),
                'message': 'Cycle detected when adding specs; existing swarm unchanged.',
            })
        if accepted_specs:
            # Track followup specs in MasterOrchestrator so ``get_status``
            # (and the /api/v1/swarm/status route) sees the full agent list.
            session.register_followup_specs(accepted_specs)
        if on_event and accepted_specs:
            # objective is for the UI agent card — full text, CSS wraps it.
            on_event({
                'type': 'swarm_phase', 'phase': 'spawn_more',
                'content': f'🚀 Spawning {len(accepted_specs)} more agent(s) (live)…',
                'agents': [
                    {'agentId': s.id, 'role': s.role,
                     'objective': s.objective,
                     'depends_on': list(s.depends_on or [])}
                    for s in accepted_specs
                ],
            })
        accepted_ids = {s.id for s in accepted_specs}
        deduped_dropped = [s for s in specs if s.id not in accepted_ids]
        specs = accepted_specs

    handle = {
        'status':    'async_launched',
        'swarm_id':  task_id,
        'is_followup': swarm_id_existing,
        'agents': [
            {
                'id':          s.id,
                'role':        s.role,
                'objective':   s.objective[:200],
                'output_file': os.path.join(output_dir, f'{s.id}.log'),
            }
            for s in specs
        ],
        'message': (
            f'Launched {len(specs)} agent(s) in the background. '
            'Continue with other work — completions will arrive automatically '
            'as <swarm-update> user messages on later turns. Use '
            'await_agents() if you have nothing else to do, or '
            'get_agent_result(id) when a preview was insufficient.'),
    }
    if deduped_dropped:
        handle['deduplicated'] = [
            {'id': s.id, 'objective': s.objective[:120]}
            for s in deduped_dropped
        ]
        handle['message'] += (
            f' Note: {len(deduped_dropped)} spec(s) were skipped '
            'because their objective duplicates an already-running '
            'or completed agent — see ``deduplicated`` field.')
    return json.dumps(handle, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  await_agents
# ═══════════════════════════════════════════════════════════

def _handle_await_agents(fn_args: dict, *, task_id: str) -> str:
    session = _get_session(task_id)
    if session is None:
        return json.dumps({
            'status': 'error',
            'error':  'no active swarm session',
            'message': (
                'No active swarm — call spawn_agents first, or you may have '
                'aborted / let the session expire.'),
        })

    ids_in = fn_args.get('ids') or []
    if not isinstance(ids_in, list):
        ids_in = []
    mode = fn_args.get('mode', 'any')
    timeout = fn_args.get('timeout_seconds', 60)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as e:
        logger.debug('[Swarm] Bad await timeout %r, defaulting to 60s: %s', timeout, e)
        timeout = 60.0
    timeout = max(1.0, min(timeout, AWAIT_AGENTS_HARD_CAP_SEC))

    result = session.await_agents(
        ids=[str(x) for x in ids_in] or None,
        mode=mode,
        timeout_seconds=timeout,
    )
    result['status'] = 'ok'
    return json.dumps(result, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  get_agent_result
# ═══════════════════════════════════════════════════════════

def _swarm_base_dir() -> str:
    """Root dir holding all ``<task_id>/`` sub-agent log folders."""
    return SWARM_OUTPUT_DIR or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'data', 'swarm',
    )


def _read_log_file(path: str, task_id: str) -> str | None:
    try:
        with open(path, encoding='utf-8') as fp:
            return fp.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug('[Swarm:%s] could not read agent log %s: %s',
                     task_id, path, e)
        return None


def _read_agent_log(task_id: str, agent_id: str) -> tuple[str, str] | None:
    """Read a finished sub-agent's full streamed transcript from disk.

    Each sub-agent streams its raw output (thinking + content) to
    ``<base>/<task_id>/<agent_id>.log`` (see ``lib/swarm/agent.py``). That
    file OUTLIVES the in-memory session — it is never deleted on session
    teardown / TTL eviction / recycling. It is the durable fallback for
    ``get_agent_result`` when the live ``MasterOrchestrator`` is gone.

    Lookup is two-stage because the agent's log lives under the task_id of
    the turn that SPAWNED it, while ``get_agent_result`` is frequently
    called from a LATER turn in the same conversation (each user message
    gets a fresh task_id). So:

      1. Fast path — try ``<base>/<task_id>/<agent_id>.log``.
      2. Cross-task path — glob ``<base>/*/<agent_id>.log`` (agent ids are
         globally near-unique 8-char tokens). On multiple hits, pick the
         most recently modified.

    Returns ``(text, source_path)`` or None if not found anywhere.
    """
    fast = os.path.join(_resolve_output_dir(task_id), f'{agent_id}.log')
    text = _read_log_file(fast, task_id)
    if text is not None:
        return text, fast

    import glob
    base = _swarm_base_dir()
    try:
        matches = glob.glob(os.path.join(base, '*', f'{agent_id}.log'))
    except OSError as e:
        logger.debug('[Swarm:%s] cross-task glob failed for %s: %s',
                     task_id, agent_id, e)
        return None
    matches = [m for m in matches if m != fast]
    if not matches:
        return None
    if len(matches) > 1:
        try:
            matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        except OSError as e:
            logger.debug('[Swarm:%s] mtime sort failed: %s', task_id, e)
        logger.info('[Swarm:%s] agent %s log found in %d dirs — using newest %s',
                    task_id, agent_id, len(matches), matches[0])
    text = _read_log_file(matches[0], task_id)
    if text is None:
        return None
    return text, matches[0]


def _handle_get_agent_result(fn_args: dict, *, task_id: str) -> str:
    agent_id = (fn_args.get('agent_id') or '').strip()
    if not agent_id:
        return json.dumps({
            'status': 'error',
            'error':  'agent_id is required',
        })

    session = _get_session(task_id)
    if session is not None:
        payload = session.get_agent_result(agent_id)
        if payload.get('found'):
            payload['status'] = 'ok'
            return json.dumps(payload, ensure_ascii=False)
        # Session is live but doesn't know this agent_id (e.g. recycled
        # session). Fall through to the on-disk fallback before giving up.

    # No live session, OR the live session lost this result — recover the
    # full transcript from the durable per-agent log file. This also covers
    # the common cross-task case: the result is asked for on a LATER turn
    # (fresh task_id) than the one that spawned the swarm.
    found = _read_agent_log(task_id, agent_id)
    if found is not None:
        log_text, source_path = found
        cross_task = os.path.dirname(source_path) != _resolve_output_dir(task_id)
        logger.info('[Swarm:%s] get_agent_result(%s) served from disk '
                    '(session %s, %s)', task_id, agent_id,
                    'gone' if session is None else 'lacked result',
                    'cross-task' if cross_task else 'same-task')
        return json.dumps({
            'status':       'ok',
            'found':        True,
            'agent_id':     agent_id,
            'source':       'disk',
            'final_answer': log_text,
            'note': ('Served from the on-disk transcript — the live swarm '
                     'session for this result is no longer in memory, so '
                     'metadata (tokens/elapsed/role) is unavailable. The '
                     'full streamed output is the final_answer field.'),
        }, ensure_ascii=False)

    if session is None:
        return json.dumps({
            'status': 'error',
            'error':  'no active swarm session',
            'message': ('No active swarm and no on-disk transcript for '
                        f'agent {agent_id!r} — perhaps it ended. Use '
                        'spawn_agents to start a new one.'),
        })
    # Session live but agent genuinely unknown and no log on disk.
    payload = session.get_agent_result(agent_id)
    payload['status'] = 'ok' if payload.get('found') else 'error'
    return json.dumps(payload, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
#  Artifact passthrough (master → live session's store)
# ═══════════════════════════════════════════════════════════

def _handle_artifact_tool(fn_name: str, fn_args: dict, task_id: str) -> str:
    session = _get_session(task_id)
    if not session:
        return ('No active swarm session — artifacts require an active '
                'spawn_agents call.')

    store = session.artifact_store

    if fn_name == 'store_artifact':
        key = fn_args.get('key', '')
        content = fn_args.get('content', '')
        if not key:
            return 'Error: key is required'
        store.put(key, content, writer_id='orchestrator',
                  tags=fn_args.get('tags', []))
        return f'Stored artifact "{key}" ({len(content):,} chars)'

    if fn_name == 'read_artifact':
        key = fn_args.get('key', '')
        if not key:
            return 'Error: key is required'
        content = store.get(key)
        if not content:
            available = store.list_keys()
            return (f'Artifact "{key}" not found. '
                    f'Available: {", ".join(available) or "(none)"}')
        return content

    if fn_name == 'list_artifacts':
        return store.summary()

    return f'Unknown artifact tool: {fn_name}'
