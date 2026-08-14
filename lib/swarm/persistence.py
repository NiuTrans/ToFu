"""lib/swarm/persistence.py — Durable, DB-backed swarm session/agent state.

Why this exists
---------------
Before this module the entire swarm lived in process memory: the session
registry (``integration._active_sessions``), each ``SubAgent.messages``
array, and the model-facing inbox (``lib.agent_inbox._inboxes``). A server
restart wiped all of it, so an in-flight sub-agent died permanently and a
"continue" turn after a restart could not resume it — only the streamed
``.log`` transcript survived (good for reading a *finished* agent's text,
useless for *resuming* an unfinished one).

This module persists the **resumable** state through the versioned Storage
Sidecar.  Business code never opens either backend or sends SQL. Two logical
tables are owned by the Sidecar:

  * ``swarm_sessions`` — one row per conversation-scoped swarm key:
    the spec set, the config needed to rebuild the tool list on rehydrate,
    and the session status (running / terminated).
  * ``swarm_agents`` — one row per sub-agent: its full ``messages`` array
    (the resumable conversation), live status, the final result, and a
    ``delivered`` flag that replaces the in-memory inbox for crash recovery.

Write cadence is **round-boundary only** (the same boundary where the
streaming ``.log`` is flushed), so a 20-round agent does ~20 small writes,
never per-token.

Design rules
------------
* Every function is best-effort and **never raises into the caller** — a DB
  hiccup must not kill a running sub-agent. Failures log at WARNING and
  return a falsy/empty value. Persistence is a safety net, not a critical
  path.
* Every write is one semantic command and one complete Sidecar transaction.
  A command receipt makes replay after an ambiguous acknowledgement safe.
"""

from __future__ import annotations

import uuid

from lib.log import get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)


_now_ms = now_ms


def _storage(*, write: bool = False):
    from lib.storage import get_storage_client
    return get_storage_client(write=write)


def _command(operation: str, payload: dict) -> dict:
    command_id = f'{operation}:{uuid.uuid4().hex}'
    return _storage(write=True).command(operation, payload, command_id)


# ═══════════════════════════════════════════════════════════
#  Session-level
# ═══════════════════════════════════════════════════════════

def save_session(swarm_key: str, *, conv_id: str, task_id: str,
                 specs: list, config: dict, status: str = 'running') -> None:
    """Upsert the session row. ``specs`` is a list of ``SubTaskSpec.to_dict()``.

    ``config`` carries everything needed to rebuild the sub-agent tool list
    and model on rehydrate (see ``integration._persist_config_for``).
    """
    if not swarm_key:
        return
    now = _now_ms()
    try:
        _command('swarm.session.save', {
            'swarm_key': swarm_key,
            'conv_id': conv_id,
            'task_id': task_id,
            'status': status,
            'specs': specs,
            'config': config,
            'now_ms': now,
        })
        logger.debug('[SwarmPersist] saved session key=%s status=%s specs=%d',
                     swarm_key, status, len(specs))
    except Exception as e:
        logger.error('[SwarmPersist] save_session(%s) FAILED — resumable '
                     'session state not updated: %s', swarm_key, e,
                     exc_info=True)


def mark_session_terminated(swarm_key: str) -> None:
    """Flag the session row terminated (driver thread exited)."""
    if not swarm_key:
        return
    try:
        _command('swarm.session.terminate', {
            'swarm_key': swarm_key, 'now_ms': _now_ms(),
        })
        logger.debug('[SwarmPersist] session %s → terminated', swarm_key)
    except Exception as e:
        logger.warning('[SwarmPersist] mark_session_terminated(%s) failed: %s',
                       swarm_key, e)


def delete_session(swarm_key: str) -> None:
    """Remove a session and all its agent rows (TTL eviction / abort)."""
    if not swarm_key:
        return
    try:
        _command('swarm.session.delete', {'swarm_key': swarm_key})
        logger.debug('[SwarmPersist] deleted session %s', swarm_key)
    except Exception as e:
        logger.warning('[SwarmPersist] delete_session(%s) failed: %s', swarm_key, e)


# ═══════════════════════════════════════════════════════════
#  Agent-level
# ═══════════════════════════════════════════════════════════

def save_agent(swarm_key: str, agent_id: str, *,
               role: str, objective: str, status: str,
               messages: list, result: dict | None = None,
               rounds_used: int = 0, delivered: bool | None = None) -> None:
    """Upsert one agent's checkpoint.

    ``messages`` is the agent's full conversation array — the resumable state.
    ``result`` is ``SubAgentResult.to_dict()`` (or None mid-run). ``delivered``
    is left unchanged when None (so a checkpoint write doesn't clobber a
    previously-set delivered flag).
    """
    if not swarm_key or not agent_id:
        return
    now = _now_ms()
    try:
        _command('swarm.agent.save', {
            'swarm_key': swarm_key,
            'agent_id': agent_id,
            'role': role,
            'objective': objective,
            'status': status,
            'messages': messages or [],
            'result': result or {},
            'rounds_used': rounds_used,
            'delivered': delivered,
            'now_ms': now,
        })
        logger.debug('[SwarmPersist] saved agent key=%s id=%s status=%s rounds=%d msgs=%d',
                     swarm_key, agent_id, status, rounds_used, len(messages or []))
    except Exception as e:
        logger.error('[SwarmPersist] save_agent(%s/%s) FAILED — resumable '
                     'agent checkpoint not updated: %s', swarm_key, agent_id,
                     e, exc_info=True)


def mark_delivered(swarm_key: str, agent_ids) -> None:
    """Mark the given agents' results as delivered to the main model.

    Called from every channel that hands a result to the model: the
    orchestrator's inbox drain, and the master's await/get_agent_result
    dedup. After this, a rehydrate will NOT re-enqueue these as
    ``<swarm-update>``s.
    """
    if not swarm_key or not agent_ids:
        return
    ids = [str(a) for a in agent_ids]
    if not ids:
        return
    try:
        _command('swarm.agents.mark_delivered', {
            'swarm_key': swarm_key, 'agent_ids': ids,
        })
    except Exception as e:
        logger.warning('[SwarmPersist] mark_delivered(%s) failed: %s', swarm_key, e)


# ═══════════════════════════════════════════════════════════
#  Rehydration (startup)
# ═══════════════════════════════════════════════════════════

def load_resumable_sessions() -> list[dict]:
    """Return all persisted sessions worth rehydrating on startup.

    A session is worth rehydrating when it has at least one non-terminal
    agent (work to resume) OR at least one completed-but-undelivered result
    (a notification the main agent never saw). Fully-terminated, fully-
    delivered sessions are skipped (their finished transcripts already live
    on disk and in ``swarm_agents`` for ad-hoc ``get_agent_result``).

    Returns a list of dicts::

        {swarm_key, conv_id, task_id, status, specs (list[dict]),
         config (dict), agents (list[dict])}

    where each agent dict has: agent_id, role, objective, status,
    messages (list), result (dict), rounds_used, delivered (bool).
    """
    try:
        rows = _storage().query('swarm.resumable.list', {})
    except Exception as e:
        logger.warning('[SwarmPersist] load_resumable_sessions failed: %s', e)
        return []
    if not isinstance(rows, list):
        logger.warning('[SwarmPersist] invalid resumable response')
        return []
    if rows:
        logger.info('[SwarmPersist] %d resumable swarm session(s) found on startup',
                    len(rows))
    return rows
