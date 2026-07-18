"""lib/swarm/integration/_logs.py — durable on-disk sub-agent transcript access.

The per-agent ``<base>/<task_id>/<agent_id>.log`` files OUTLIVE the in-memory
``MasterOrchestrator`` session — they are the durable fallback that lets
``await_agents`` / ``get_agent_result`` return a completed sub-agent's full
output even after the session is gone (TTL eviction, recycle, restart).

Also owns output-dir resolution (``_resolve_output_dir`` / ``_swarm_base_dir``)
since that's rooted in the same on-disk layout. Depends only on ``_config`` for
the ``SWARM_OUTPUT_DIR`` override.
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.swarm.integration._config import SWARM_OUTPUT_DIR

logger = get_logger(__name__)


# ── Output dir resolution ────────────────────────────────

def _resolve_output_dir(task_id: str) -> str:
    """Return absolute path to ``<base>/<task_id>/`` for sub-agent log streams."""
    return os.path.join(_swarm_base_dir(), task_id)


def _swarm_base_dir() -> str:
    """Root dir holding all ``<task_id>/`` sub-agent log folders.

    Honours the ``TOFU_SWARM_OUTPUT_DIR`` override, else ``<data_root>/swarm``.
    Uses ``lib.runtime_paths.data_root()`` — the single source of truth — so
    these sub-agent logs co-locate with the DB under the resolved writable root,
    not the code tree (which a fresh source checkout may place on a different
    mount).
    """
    if SWARM_OUTPUT_DIR:
        return SWARM_OUTPUT_DIR
    try:
        from lib.runtime_paths import data_root
        return os.path.join(data_root(), 'swarm')
    except Exception as e:  # pragma: no cover — defensive
        logger.warning('[swarm] runtime_paths.data_root() unavailable, '
                       'falling back to in-tree data/swarm: %s', e)
        return os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))),
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
