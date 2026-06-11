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

# ═══════════════════════════════════════════════════════════
#  Auto-continue (Phase 2): wake the main agent when a swarm settles
#  with pending <swarm-update>s but no live turn to drain them.
# ═══════════════════════════════════════════════════════════

#: Master switch. When falsy, a settled swarm just leaves its inbox for the
#: NEXT user-initiated turn (legacy behaviour). Default ON. Kill with
#: ``TOFU_SWARM_AUTOCONTINUE=0``.
def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name, '')
    if raw == '':
        return default
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


SWARM_AUTOCONTINUE_ENABLED = _env_truthy('TOFU_SWARM_AUTOCONTINUE', True)

#: Hard ceiling on CONSECUTIVE auto-continued turns per conversation. An
#: auto-continued turn that itself spawns a fresh wave could otherwise
#: re-trigger this indefinitely (a runaway token burn). The counter resets
#: whenever a human-initiated turn runs in the conversation.
SWARM_AUTOCONTINUE_MAX_CHAIN = int(os.environ.get('TOFU_SWARM_AUTOCONTINUE_MAX', '3') or '3')

#: conv key → number of consecutive auto-continuations since the last
#: human turn. Guarded by ``_autocontinue_lock``.
_autocontinue_chain: dict[str, int] = {}
#: conv keys with an auto-continue in flight (latch against double-fire when
#: several agents settle near-simultaneously / from spawn-more waves).
_autocontinue_inflight: set[str] = set()
_autocontinue_lock = threading.Lock()

#: Swarm sessions are keyed by a STABLE *swarm key* — the conversation id
#: when available, else the spawning task id. This is what lets a swarm
#: outlive the single task-turn that spawned it: a later "continue" turn in
#: the same conversation (which has a fresh task_id) still resolves to the
#: same live session. ``_key_aliases`` maps every task_id that has touched a
#: session → its swarm key, so route callers that only know a task_id (the
#: /api/v1/swarm/* endpoints) keep working unchanged.
_active_sessions: dict[str, MasterOrchestrator] = {}
_session_timestamps: dict[str, float] = {}
_key_aliases: dict[str, str] = {}
_sessions_lock = threading.Lock()
_last_cleanup: float = 0.0
_cleanup_timer: threading.Timer | None = None


def swarm_key_for(task: dict | None) -> str:
    """Return the stable swarm key for *task* — conv id, else task id.

    Single source of truth for the session/inbox key. The orchestrator's
    inbox drain hook and ``MasterOrchestrator.inbox_key`` MUST agree with
    this, otherwise <swarm-update> items enqueued under one key are never
    drained under the other.
    """
    task = task or {}
    return (task.get('convId') or '') or task.get('id', 'unknown')


def _resolve_key(arg: str) -> str:
    """Map a task_id (or already-a-key) to its swarm key via the alias table."""
    return _key_aliases.get(arg, arg)


# ── Output dir resolution ────────────────────────────────

def _resolve_output_dir(task_id: str) -> str:
    """Return absolute path to ``<base>/<task_id>/`` for sub-agent log streams."""
    base = SWARM_OUTPUT_DIR or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'data', 'swarm',
    )
    return os.path.join(base, task_id)


# ── Cleanup ──────────────────────────────────────────────

def _key_is_live(swarm_key: str) -> bool:
    """True if ANY non-terminal task belongs to this swarm key's conversation.

    A swarm session is now conversation-scoped (see ``swarm_key_for``), so
    its lifetime is bounded by the *conversation*, not a single task-turn.
    TTL eviction exists only to reap sessions whose conversation has gone
    quiet — it must NOT kill a swarm just because the turn that spawned it
    ended (the whole point of Option A). We scan the chat task registry for
    any live task whose ``convId`` (or ``id``) matches the key. The registry
    read is a plain dict iteration (GIL-safe for a best-effort heuristic).
    Import is lazy + guarded so a missing/renamed registry never breaks
    cleanup.
    """
    if not swarm_key:
        return False
    try:
        from lib.tasks_pkg.manager import tasks as _chat_tasks
        # Direct task-id hit (legacy task-keyed sessions).
        t = _chat_tasks.get(swarm_key)
        if t is not None and t.get('status') not in ('done', 'error', 'aborted'):
            return True
        # Conversation-scoped: any live task in this conversation keeps the
        # swarm alive across turns.
        for t in list(_chat_tasks.values()):
            if (t.get('convId') == swarm_key
                    and t.get('status') not in ('done', 'error', 'aborted')):
                return True
        return False
    except Exception as e:
        logger.debug('[Swarm] key liveness check failed for %s: %s', swarm_key, e)
        return False


def _cleanup_stale_sessions():
    """Drop sessions past TTL or above MAX_SESSIONS. Caller must hold lock."""
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 60:
        return
    _last_cleanup = now

    def _purge_aliases(key: str):
        for alias in [a for a, k in _key_aliases.items() if k == key]:
            _key_aliases.pop(alias, None)

    stale_ids = [
        key for key, ts in _session_timestamps.items()
        if now - ts > SESSION_TTL_SECONDS and not _key_is_live(key)
    ]
    for key in stale_ids:
        session = _active_sessions.pop(key, None)
        _session_timestamps.pop(key, None)
        agent_inbox.clear(key)
        _purge_aliases(key)
        # Drop the auto-continue bookkeeping for a reaped conversation so the
        # chain counter / inflight latch don't accumulate stale keys.
        with _autocontinue_lock:
            _autocontinue_chain.pop(key, None)
            _autocontinue_inflight.discard(key)
        if session:
            logger.info('[Swarm:%s] Session expired after %ds TTL — cleaning up',
                        key, SESSION_TTL_SECONDS)
            try:
                session.abort()
            except Exception as e:
                logger.debug('[Swarm:%s] cleanup abort failed: %s', key, e, exc_info=True)

    if len(_active_sessions) > MAX_SESSIONS:
        sorted_ids = sorted(_session_timestamps, key=_session_timestamps.get)
        excess = len(_active_sessions) - MAX_SESSIONS
        for key in sorted_ids[:excess]:
            session = _active_sessions.pop(key, None)
            _session_timestamps.pop(key, None)
            agent_inbox.clear(key)
            _purge_aliases(key)
            if session:
                logger.warning('[Swarm:%s] Evicted (MAX_SESSIONS=%d exceeded)',
                               key, MAX_SESSIONS)
                try:
                    session.abort()
                except Exception as e:
                    logger.debug('[Swarm:%s] eviction abort failed: %s',
                                 key, e, exc_info=True)


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
        return _active_sessions.get(_resolve_key(task_id))


def _set_session(swarm_key: str, session: MasterOrchestrator, *,
                 task_id: str = ''):
    """Register *session* under its stable swarm key.

    ``task_id`` (when distinct) is recorded as an alias so route callers and
    the orchestrator teardown — which only know the task id — still resolve
    to this session.
    """
    with _sessions_lock:
        _cleanup_stale_sessions()
        _active_sessions[swarm_key] = session
        _session_timestamps[swarm_key] = time.time()
        if task_id and task_id != swarm_key:
            _key_aliases[task_id] = swarm_key


def _remove_session(task_id: str):
    """Remove the session resolved from *task_id* (task id or swarm key)."""
    key = _resolve_key(task_id)
    with _sessions_lock:
        _active_sessions.pop(key, None)
        _session_timestamps.pop(key, None)
        for alias in [a for a, k in _key_aliases.items() if k == key]:
            _key_aliases.pop(alias, None)
    agent_inbox.clear(key)


def add_session_alias(task_id: str, swarm_key: str):
    """Map a later turn's task_id onto an existing conv-scoped session.

    Called when a fresh task in the same conversation wants to reach the
    live swarm (e.g. ``await_agents`` from a "continue" turn) but isn't the
    task that spawned it.
    """
    if not task_id or not swarm_key or task_id == swarm_key:
        return
    with _sessions_lock:
        if swarm_key in _active_sessions:
            _key_aliases[task_id] = swarm_key


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
            'created_at': _session_timestamps.get(_resolve_key(task_id), 0),
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
#  Auto-continue helpers (Phase 2)
# ═══════════════════════════════════════════════════════════

def reset_autocontinue_chain(swarm_key: str) -> None:
    """Reset the consecutive-auto-continue counter for a conversation.

    Called by the orchestrator at the start of a HUMAN-initiated turn so the
    chain ceiling only bounds *unattended* auto-continue loops, not normal
    back-and-forth conversation.
    """
    if not swarm_key:
        return
    with _autocontinue_lock:
        _autocontinue_chain.pop(swarm_key, None)


def _maybe_autocontinue(swarm_key: str) -> None:
    """Wake the main agent if a settled swarm left unread <swarm-update>s.

    Fired from the swarm driver's ``on_settled`` hook (master.py) when the
    whole swarm terminates. Without this, a swarm that finishes AFTER the
    spawning turn ended leaves its <swarm-update>s in the inbox until the
    user happens to send another message — so the sub-agents' work sits
    unseen (the wasted-inbox half of the Phase-2 design).

    Guardrails (this spends tokens unprompted, so be conservative):
      * disabled unless ``SWARM_AUTOCONTINUE_ENABLED``;
      * no-op when a turn is already live for this conv (``_key_is_live``) —
        that turn will drain the inbox naturally;
      * no-op when the inbox is empty (nothing to deliver);
      * latch + per-conv chain ceiling so near-simultaneous settles and
        auto-continued-turns-that-spawn-more can't runaway-loop;
      * skipped when the conversation has no connected browser client AND
        no other reason to run — handled by the caller via push presence.
    """
    if not SWARM_AUTOCONTINUE_ENABLED or not swarm_key:
        return
    try:
        # A live turn (the spawning turn hasn't ended yet, or a user just
        # sent another message) will drain the inbox itself — don't race it.
        if _key_is_live(swarm_key):
            logger.debug('[Swarm:%s] autocontinue skipped — conversation still live',
                         swarm_key)
            return
        if not agent_inbox.has_pending(swarm_key):
            logger.debug('[Swarm:%s] autocontinue skipped — inbox empty', swarm_key)
            return

        with _autocontinue_lock:
            if swarm_key in _autocontinue_inflight:
                logger.debug('[Swarm:%s] autocontinue already in flight', swarm_key)
                return
            chain = _autocontinue_chain.get(swarm_key, 0)
            if chain >= SWARM_AUTOCONTINUE_MAX_CHAIN:
                logger.warning(
                    '[Swarm:%s] autocontinue chain ceiling reached (%d) — '
                    'leaving %d update(s) for the next human turn',
                    swarm_key, SWARM_AUTOCONTINUE_MAX_CHAIN,
                    agent_inbox.peek(swarm_key))
                return
            _autocontinue_inflight.add(swarm_key)
            _autocontinue_chain[swarm_key] = chain + 1

        # ``swarm_key`` is the conversation id (Option A) except in
        # standalone/test contexts where it's a bare task id. Auto-continue
        # only makes sense for a real conversation row, so bail otherwise.
        conv_id = swarm_key
        try:
            n_pending = agent_inbox.peek(swarm_key)
            logger.info('[Swarm:%s] auto-continuing main agent — %d pending '
                        'swarm-update(s), chain=%d',
                        swarm_key, n_pending, _autocontinue_chain.get(swarm_key, 0))
            started = _start_autocontinue_turn(conv_id)
            if not started:
                # Failed to start — release the chain increment so a later
                # settle (or human turn) can retry rather than being blocked.
                with _autocontinue_lock:
                    cur = _autocontinue_chain.get(swarm_key, 0)
                    if cur > 0:
                        _autocontinue_chain[swarm_key] = cur - 1
        finally:
            with _autocontinue_lock:
                _autocontinue_inflight.discard(swarm_key)
    except Exception as e:
        logger.error('[Swarm:%s] autocontinue error: %s', swarm_key, e, exc_info=True)
        with _autocontinue_lock:
            _autocontinue_inflight.discard(swarm_key)


def _start_autocontinue_turn(conv_id: str) -> bool:
    """Start a backend-initiated chat turn that drains the swarm inbox.

    Mirrors the proactive-agent path (``lib.scheduler._shared.inject_and_run_task``)
    but injects NO user message — the orchestrator's between-round inbox
    drain hook prepends the pending <swarm-update>s as the turn's first user
    message, exactly as it would on a human "continue" turn. We only need to
    create + spawn an agentic task whose config matches the conversation's.

    Returns True if a task was created and spawned, else False.
    """
    try:
        import json as _json

        from lib.database import (DOMAIN_CHAT, db_execute_with_retry,
                                  get_thread_db, json_dumps_pg)
        from lib.tasks_pkg import spawn_task
        from lib.tasks_pkg.manager import create_task as _create_task

        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        if not row:
            logger.warning('[Swarm:%s] autocontinue: conversation not found', conv_id)
            return False

        try:
            messages = _json.loads(row['messages'] or '[]')
        except (ValueError, TypeError) as e:
            logger.debug('[Swarm:%s] autocontinue: bad messages json: %s', conv_id, e)
            messages = []
        try:
            settings = _json.loads(row['settings'] or '{}')
        except (ValueError, TypeError) as e:
            logger.debug('[Swarm:%s] autocontinue: bad settings json: %s', conv_id, e)
            settings = {}

        # Append a placeholder assistant message so the frontend (and the
        # result-sync path) has a bubble to stream into — tagged so the UI
        # can badge it as an automatic continuation.
        assistant_msg = {
            'role': 'assistant',
            'content': '',
            'thinking': '',
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
            '_swarmAutoContinue': True,
        }
        messages.append(assistant_msg)

        from lib.conversations import build_search_text, update_conversation_fts
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db,
            'UPDATE conversations SET messages=?, updated_at=?, msg_count=?, '
            'search_text=? WHERE id=? AND user_id=1',
            (messages_json, now_ms, len(messages), search_text, conv_id))
        try:
            update_conversation_fts(db, conv_id, search_text)
        except Exception as e:
            logger.debug('[Swarm:%s] autocontinue fts update failed: %s', conv_id, e)

        # Build a task config from the conversation's own settings so the
        # continuation runs with the SAME model / tools / swarm-enabled the
        # user had configured. Keep swarm enabled so the model can await /
        # fetch results it was notified about.
        config = {
            'model':            settings.get('model', ''),
            'preset':           settings.get('model', ''),
            'thinkingEnabled':  settings.get('thinkingEnabled', True),
            'searchMode':       settings.get('searchMode', 'multi'),
            'fetchEnabled':     settings.get('fetchEnabled', True),
            'projectPath':      settings.get('projectPath', ''),
            'projectEnabled':   settings.get('projectEnabled', False),
            'codeExecEnabled':  settings.get('codeExecEnabled', False),
            'browserEnabled':   settings.get('browserEnabled', False),
            'memoryEnabled':    settings.get('memoryEnabled', True),
            'swarmEnabled':     settings.get('swarmEnabled', True),
            'imageGenEnabled':  settings.get('imageGenEnabled', False),
            'schedulerEnabled': settings.get('schedulerEnabled', False),
            '_swarmAutoContinue': True,
        }

        task = _create_task(conv_id, messages, config)
        task_id = task['id']

        settings['activeTaskId'] = task_id
        try:
            db_execute_with_retry(db,
                'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
                (_json.dumps(settings, ensure_ascii=False), conv_id))
        except Exception as e:
            logger.debug('[Swarm:%s] autocontinue activeTaskId persist failed: %s',
                         conv_id, e)

        # Notify any connected browser tab so it attaches to this turn it
        # didn't POST (opens the SSE stream + renders the continuation
        # bubble). Best-effort — headless API clients just see the result
        # land in the conversation on next load.
        try:
            from lib.agent_core.push import push_event
            # NOTE: do NOT put a 'taskId' key in the payload — the hub frame
            # is {'channel', 'taskId': <routing id = conv_id>, **payload}, so
            # a payload 'taskId' would clobber the routing field the
            # subscriber reads as convId. Use 'newTaskId' for the task id.
            push_event('swarm', conv_id, {
                'type': 'swarm_autocontinue_started',
                'convId': conv_id,
                'newTaskId': task_id,
            })
        except Exception as e:
            logger.debug('[Swarm:%s] autocontinue push notify failed: %s', conv_id, e)

        logger.info('[Swarm:%s] autocontinue task %s spawned', conv_id, task_id[:8])
        spawn_task(task)
        return True
    except Exception as e:
        logger.error('[Swarm:%s] autocontinue task start failed: %s',
                     conv_id, e, exc_info=True)
        return False


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
            return _handle_await_agents(fn_args, task_id=task_id, task=task)

        if fn_name == 'get_agent_result':
            return _handle_get_agent_result(fn_args, task_id=task_id, task=task)

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

    # Conversation-scoped session key (Option A): a swarm spawned on one
    # turn is reachable from later turns in the SAME conversation. Falls
    # back to task_id when there's no conv (tests, standalone).
    swarm_key = swarm_key_for(task)

    # If a session already exists for this conversation, ADD to it instead
    # of creating a fresh one. This is how the main agent re-uses the same
    # swarm to launch a follow-up wave (replaces legacy
    # ``spawn_more_agents``).  If the existing session has already
    # terminated, drop it so we fall through to the "new session" branch
    # — otherwise the user can never spawn again after the first wave
    # completes.
    session = _get_session(swarm_key)
    if session is not None and session.is_terminated:
        logger.info('[Swarm:%s] previous session terminated — recycling key', swarm_key)
        _remove_session(swarm_key)
        session = None

    # A fresh wave must be able to enqueue <swarm-update>s even if a prior
    # wave on this key was explicitly aborted (which tombstoned the inbox).
    agent_inbox.untombstone(swarm_key)

    swarm_id_existing = bool(session)
    # Per-agent log streams stay task-scoped on disk (one dir per spawning
    # turn) — the cross-task glob in _read_agent_log already finds them
    # from any later turn, so durable result recovery is unaffected.
    output_dir = _resolve_output_dir(task_id)

    # Conversation id for the durable push channel. Unlike ``on_event`` (the
    # SSE stream of the SPAWNING turn, which dies when that turn ends), the
    # /api/push WebSocket is conversation-global and survives turn end — so a
    # detached swarm's later events (esp. the terminal ``swarm_phase:complete``
    # that clears the "N running async" badge) still reach the browser when the
    # agents finish AFTER the turn that spawned them. Without this the panel
    # stays stuck on "running" forever (the bug this fixes).
    push_conv_id = (task.get('convId') or cfg.get('convId') or '')

    def _emit(ev: dict):
        if on_event:
            on_event(ev)
        if push_conv_id:
            try:
                from lib.agent_core.push import push_event
                push_event('swarm', push_conv_id, ev)
            except Exception as e:
                logger.debug('[Swarm:%s] push mirror failed: %s', task_id, e)

    deduped_dropped: list[SubTaskSpec] = []
    if session is None:
        conv_id = task.get('convId', cfg.get('convId', '')) or ''
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
            inbox_key=swarm_key,
            on_settled=lambda k=swarm_key: _maybe_autocontinue(k),
        )
        _set_session(swarm_key, session, task_id=task_id)

        try:
            session.run_in_background()
        except ValueError as e:
            # Cycle detection raised by add_specs
            logger.warning('[Swarm:%s] spawn_agents rejected: %s',
                           swarm_key, e)
            _remove_session(swarm_key)
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
        # A live session reached from a later turn: alias this task_id so
        # await_agents / get_agent_result on THIS turn resolve to it.
        add_session_alias(task_id, swarm_key)
        try:
            accepted_specs = session._scheduler.add_specs(specs)  # type: ignore[union-attr]
        except ValueError as e:
            logger.warning('[Swarm:%s] follow-up spawn rejected: %s',
                           swarm_key, e)
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
                # Full objective (not truncated): this handle is the only
                # persisted source the swarm panel re-parses to rebuild agent
                # cards after a reload (_recoverSwarmAgents in streaming_ui.js),
                # and the card renders it verbatim with CSS wrapping. A [:N]
                # cap here clips the displayed text mid-sentence. The text is
                # the model's own spawn input, so echoing it in full is no new
                # information in its context.
                'objective':   s.objective,
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

def _handle_await_agents(fn_args: dict, *, task_id: str,
                         task: dict | None = None) -> str:
    # Resolve the conversation-scoped session (Option A): a "continue" turn
    # has a fresh task_id but the live swarm lives under the conv key.
    swarm_key = swarm_key_for(task) if task is not None else task_id
    session = _get_session(swarm_key) or _get_session(task_id)
    if session is not None:
        add_session_alias(task_id, swarm_key)
    if session is None:
        # No live session anywhere for this conversation. Fall back to the
        # durable on-disk transcripts so a cross-turn await still returns
        # whatever the (now torn-down) agents produced, instead of a hard
        # "no active swarm" error that strands the user's results.
        disk = _await_from_disk(task_id, fn_args)
        if disk is not None:
            return disk
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

def _await_from_disk(task_id: str, fn_args: dict) -> str | None:
    """Best-effort ``await_agents`` fallback when no live session exists.

    Only works when the caller named explicit ``ids`` — we look each one up
    via the cross-task on-disk transcript glob (``_read_agent_log``). With no
    ids there's nothing to scope to (the spawning task dir is unknown once
    the session is gone), so we return None and let the caller emit the
    standard "no active swarm" error.

    Returns a JSON string shaped like ``await_agents`` output (so the model
    sees a uniform contract), or None if nothing could be recovered.
    """
    ids_in = fn_args.get('ids') or []
    if not isinstance(ids_in, list) or not ids_in:
        return None
    completed: list[dict] = []
    missing: list[str] = []
    for raw_id in ids_in:
        sid = str(raw_id)
        found = _read_agent_log(task_id, sid)
        if found is None:
            missing.append(sid)
            continue
        log_text, _src = found
        completed.append({
            'agent_id':     sid,
            'status':       'completed',
            'source':       'disk',
            'preview':      log_text[:200],
            'final_answer': log_text,
        })
    if not completed:
        return None
    logger.info('[Swarm:%s] await_agents served %d agent(s) from disk '
                '(no live session)', task_id, len(completed))
    out = {
        'status':        'ok',
        'completed':     completed,
        'still_running': [],
        'mode':          fn_args.get('mode', 'any'),
        'timed_out':     False,
        'source':        'disk',
        'note': ('No live swarm session — these results were recovered from '
                 'the durable on-disk transcripts. Metadata (tokens/elapsed) '
                 'is unavailable; the full output is in final_answer.'),
    }
    if missing:
        out['still_running'] = []
        out['unknown'] = missing
        out['note'] += (f' Could not find on-disk transcripts for: '
                        f'{", ".join(missing)}.')
    return json.dumps(out, ensure_ascii=False)


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
        logger.debug('[Swarm:%s] agent log not found: %s', task_id, path)
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


def _handle_get_agent_result(fn_args: dict, *, task_id: str,
                             task: dict | None = None) -> str:
    agent_id = (fn_args.get('agent_id') or '').strip()
    if not agent_id:
        return json.dumps({
            'status': 'error',
            'error':  'agent_id is required',
        })

    swarm_key = swarm_key_for(task) if task is not None else task_id
    session = _get_session(swarm_key) or _get_session(task_id)
    if session is not None:
        add_session_alias(task_id, swarm_key)
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
