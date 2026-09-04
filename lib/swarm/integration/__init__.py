"""lib/swarm/integration — Glue between async swarm and the task orchestrator.

Routes the four swarm-control tools the master LLM may call:

  * ``spawn_agents``      — fire-and-forget; returns a handle dict
  * ``await_agents``      — blocking wait (capped at 60 s)
  * ``get_agent_result``  — pull one agent's full final answer
  * artifact tools (``store_artifact`` / ``read_artifact`` / ``list_artifacts``)
                          — proxied to the live session's ArtifactStore

There is **no** synchronous "run swarm and return synthesised answer" path
anymore. The async swarm handle is a JSON object the LLM sees as the tool
result; sub-agent completions arrive on subsequent turns as auto-injected
``<swarm-update>`` user messages (see ``lib.agent_inbox`` and the
orchestrator's between-round drain hook).

This module is a re-export FACADE — the import path
``lib.swarm.integration`` is unchanged and every symbol below resolves
byte-identically to the pre-split module. Implementations live in the
sub-modules:

  * ``_config``      — env consts + pure helpers (``swarm_key_for`` …)
  * ``_logs``        — durable on-disk sub-agent transcript access
  * ``_state``       — the ONE process-wide session registry + autocontinue
                       state + all fns that ``global``-rebind them
  * ``_autocontinue``— Phase-2 auto-continue helpers
  * ``_tools``       — ``execute_swarm_tool`` + spawn/await/get-result handlers
  * ``_rehydrate``   — startup rehydration of persisted sessions

CRITICAL: the shared session-registry / autocontinue state dicts+locks are
re-exported BY REFERENCE from ``_state`` — there is exactly one
``_active_sessions`` in the process. A divergent copy would strand live swarm
sessions.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

# ── Config: env consts + pure helpers ────────────────────
from lib.swarm.integration._config import (  # noqa: E402,F401
    AWAIT_AGENTS_HARD_CAP_SEC,
    MAX_SESSIONS,
    SESSION_TTL_SECONDS,
    SWARM_AUTOCONTINUE_ENABLED,
    SWARM_AUTOCONTINUE_MAX_CHAIN,
    SWARM_OUTPUT_DIR,
    _CLEANUP_INTERVAL,
    _env_truthy,
    _persist_config,
    _PERSIST_CFG_KEYS,
    swarm_key_for,
)

# ── Durable on-disk transcript access + output dir ───────
from lib.swarm.integration._logs import (  # noqa: E402,F401
    _read_agent_log,
    _read_log_file,
    _resolve_output_dir,
    _swarm_base_dir,
    start_swarm_output_cleanup,
    stop_swarm_output_cleanup,
    swarm_output_cleanup_snapshot,
)

# ── Shared process-wide session registry + autocontinue state ──
#  Re-exported BY REFERENCE — same dict/lock objects across the process.
from lib.swarm.integration._state import (  # noqa: E402,F401
    _active_sessions,
    _autocontinue_chain,
    _autocontinue_inflight,
    _autocontinue_lock,
    _background_cleanup,
    _cleanup_stale_sessions,
    _cleanup_timer,
    _get_session,
    _key_aliases,
    _key_is_live,
    _last_cleanup,
    _remove_session,
    _resolve_key,
    _session_timestamps,
    _sessions_lock,
    _set_session,
    _start_cleanup_timer,
    SwarmSessionCapacityExceeded,
    abort_swarm,
    add_session_alias,
    get_active_session,
    get_swarm_status,
    stop_swarm_cleanup_timer,
    swarm_cleanup_snapshot,
)

# ── Auto-continue helpers ────────────────────────────────
from lib.swarm.integration._autocontinue import (  # noqa: E402,F401
    _maybe_autocontinue,
    _start_autocontinue_turn,
    reset_autocontinue_chain,
)

# ── Tool dispatch + handlers ─────────────────────────────
from lib.swarm.integration._tools import (  # noqa: E402,F401
    _await_from_disk,
    _handle_artifact_tool,
    _handle_await_agents,
    _handle_get_agent_result,
    _handle_spawn_agents,
    execute_swarm_tool,
)

# ── Startup rehydration ──────────────────────────────────
from lib.swarm.integration._rehydrate import (  # noqa: E402,F401
    _rebuild_tool_list,
    _rehydrate_one,
    rehydrate_swarms_on_startup,
)

__all__ = [
    # Config / consts
    'SESSION_TTL_SECONDS', 'MAX_SESSIONS', 'SWARM_OUTPUT_DIR',
    'SwarmSessionCapacityExceeded',
    'AWAIT_AGENTS_HARD_CAP_SEC', 'SWARM_AUTOCONTINUE_ENABLED',
    'SWARM_AUTOCONTINUE_MAX_CHAIN', '_CLEANUP_INTERVAL',
    '_env_truthy', 'swarm_key_for', '_persist_config', '_PERSIST_CFG_KEYS',
    # Logs / disk
    '_resolve_output_dir', '_swarm_base_dir', '_read_log_file', '_read_agent_log',
    'start_swarm_output_cleanup', 'stop_swarm_output_cleanup',
    'swarm_output_cleanup_snapshot',
    # Shared state (by reference)
    '_active_sessions', '_session_timestamps', '_key_aliases', '_sessions_lock',
    '_last_cleanup', '_cleanup_timer',
    '_autocontinue_chain', '_autocontinue_inflight', '_autocontinue_lock',
    '_resolve_key', '_key_is_live', '_cleanup_stale_sessions',
    '_background_cleanup', '_start_cleanup_timer',
    '_get_session', '_set_session', '_remove_session', 'add_session_alias',
    'get_active_session', 'get_swarm_status',
    'abort_swarm', 'stop_swarm_cleanup_timer', 'swarm_cleanup_snapshot',
    # Auto-continue
    'reset_autocontinue_chain', '_maybe_autocontinue', '_start_autocontinue_turn',
    # Tools
    'execute_swarm_tool', '_handle_spawn_agents', '_handle_await_agents',
    '_handle_get_agent_result', '_handle_artifact_tool', '_await_from_disk',
    # Rehydration
    '_rebuild_tool_list', '_rehydrate_one', 'rehydrate_swarms_on_startup',
]
