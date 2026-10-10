"""lib/swarm/integration/_config.py — env-derived constants + pure config helpers.

Holds every environment-driven constant the swarm integration layer reads at
import time (session TTL / ceiling / output dir / await hard-cap / auto-continue
switches), plus the small pure helpers that don't touch shared session state:
``_env_truthy``, ``swarm_key_for``, and the durable-config snapshot
(``_persist_config`` / ``_PERSIST_CFG_KEYS``).

Kept dependency-free (no imports of the state/log submodules) so it sits at the
bottom of the package's import DAG.
"""

from __future__ import annotations

from copy import deepcopy
import os

from lib.log import get_logger
from lib.swarm.resource_policy import swarm_terminal_cache_target

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Session bookkeeping constants
# ═══════════════════════════════════════════════════════════

#: Sessions older than this are auto-aborted/evicted.
SESSION_TTL_SECONDS = 1800
#: Launch-derived terminal-session cache target. Live/recovering sessions may
#: exceed it; capacity cleanup retires only terminal memory backed by durable
#: truth and never refuses new work.
MAX_SESSIONS = swarm_terminal_cache_target()
#: Background cleanup tick.
_CLEANUP_INTERVAL = 300

#: Output dir override — falls back to ``./data/swarm`` when unset.
SWARM_OUTPUT_DIR = os.environ.get('TOFU_SWARM_OUTPUT_DIR', '')
#: Hard-cap how long ``await_agents`` may block. The model can ask for
#: up to 60 s, beyond which we degrade to "still running" and let the
#: main agent move on. An identical no-progress retry is suppressed by the
#: session receipt in ``MasterOrchestrator`` instead of blocking for 60 s again.
AWAIT_AGENTS_HARD_CAP_SEC = 60


# ═══════════════════════════════════════════════════════════
#  Auto-continue (Phase 2) switches
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


def swarm_key_for(task: dict | None) -> str:
    """Return the stable swarm key for *task* — conv id, else task id.

    Single source of truth for the session/inbox key. The orchestrator's
    inbox drain hook and ``MasterOrchestrator.inbox_key`` MUST agree with
    this, otherwise <swarm-update> items enqueued under one key are never
    drained under the other.
    """
    task = task or {}
    return (task.get('convId') or '') or task.get('id', 'unknown')


# ── Durable session config (for restart rehydration) ─────

#: cfg keys that materially affect sub-agent tool-list assembly. Persisted in
#: ``swarm_sessions.config_json`` so ``_rehydrate_one`` can rebuild the same
#: tool list a fresh spawn would have produced.
_PERSIST_CFG_KEYS = (
    'searchMode', 'search_mode', 'searchEnabled', 'fetchEnabled',
    'codeExecEnabled', 'browserEnabled', 'desktopEnabled',
    'imageGenEnabled', 'humanGuidanceEnabled', 'schedulerEnabled',
    'memoryEnabled', 'max_parallel', 'max_retries',
)


def _persist_config(cfg: dict, model: str, thinking_enabled: bool | None,
                    project_path: str, parent_cfg: dict, *, user_id,
                    parent_task: dict | None = None,
                    tool_catalog: list | None = None) -> dict:
    """Snapshot the config needed to rebuild a swarm's sub-agents on restart."""
    out = {
        'model':            model,
        'thinking_enabled': thinking_enabled,
        'project_path':     project_path,
        'parent_cfg':       deepcopy({key: value for key, value in (parent_cfg or {}).items()
                             if key not in ('_principalContext', '_tool_env',
                                 '_invocationEventSink', '_executionBudget')}),
        'user_id':          user_id,
    }
    if tool_catalog is not None:
        out['tool_catalog'] = deepcopy(tool_catalog)
    for k in _PERSIST_CFG_KEYS:
        if k in (cfg or {}):
            out[k] = deepcopy(cfg[k])
    if parent_task is not None:
        from lib.swarm.master import _copy_execution_context
        context = _copy_execution_context(parent_task)
        principal = parent_task.get('_principalContext')
        if principal is not None:
            out['principal_context'] = deepcopy(principal)
        if context:
            out['workspace_isolated'] = True
            out['workspace_execution_context'] = {'schema': 1, **context}
    return out
