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

import os

from lib.log import get_logger
from lib.swarm.resource_policy import swarm_session_capacity

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Session bookkeeping constants
# ═══════════════════════════════════════════════════════════

#: Sessions older than this are auto-aborted/evicted.
SESSION_TTL_SECONDS = 1800
#: Concurrent process session ceiling derived once at launch. New work is
#: rejected at capacity; productive sessions are never sacrificed merely to
#: admit a newer conversation.
MAX_SESSIONS = swarm_session_capacity()
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


def _persist_config(cfg: dict, model: str, thinking_enabled: bool,
                    project_path: str, parent_cfg: dict, *, user_id) -> dict:
    """Snapshot the config needed to rebuild a swarm's sub-agents on restart."""
    out = {
        'model':            model,
        'thinking_enabled': thinking_enabled,
        'project_path':     project_path,
        'parent_cfg':       dict(parent_cfg or {}),
        'user_id':          user_id,
    }
    for k in _PERSIST_CFG_KEYS:
        if k in (cfg or {}):
            out[k] = cfg[k]
    return out
