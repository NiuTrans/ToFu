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

This module persists the **resumable** state to the canonical DB layer
(PostgreSQL primary, SQLite fallback — same durability story as everything
else, with corruption self-heal + nightly backups). Two tables:

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
* All SQL uses ``?`` placeholders (translated for PG by the wrapper layer)
  and goes through ``get_thread_db(DOMAIN_SYSTEM)`` /
  ``db_execute_with_retry`` like the rest of the system-domain code.
"""

from __future__ import annotations

import json

from lib.log import get_logger
from lib.timeutil import now_ms

logger = get_logger(__name__)


# Agent statuses considered "still has work to do" → re-spawned on rehydrate.
_NONTERMINAL = frozenset({'pending', 'running', 'retrying'})


_now_ms = now_ms


def _db():
    """Return a thread-local system-domain DB handle, or None if unavailable."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        return get_thread_db(DOMAIN_SYSTEM)
    except Exception as e:
        logger.warning('[SwarmPersist] DB handle unavailable: %s', e)
        return None


def _dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.warning('[SwarmPersist] JSON encode failed (%s) — storing empty', e)
        return '[]' if isinstance(obj, list) else '{}'


def _loads(raw, default):
    if raw is None or raw == '':
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        logger.debug('[SwarmPersist] JSON decode failed: %s', e)
        return default


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
    db = _db()
    if db is None:
        return
    now = _now_ms()
    try:
        from lib.database import db_execute_with_retry
        # Portable upsert: existence-probe then UPDATE or INSERT. Avoids
        # dialect-specific ON CONFLICT differences between PG and SQLite, and
        # does not rely on a rowcount (db_execute_with_retry returns None).
        exists = db.execute(
            'SELECT 1 FROM swarm_sessions WHERE swarm_key=?', (swarm_key,)).fetchone()
        if exists:
            db_execute_with_retry(
                db,
                'UPDATE swarm_sessions SET conv_id=?, task_id=?, status=?, '
                'specs_json=?, config_json=?, updated_at=? WHERE swarm_key=?',
                (conv_id, task_id, status, _dumps(specs), _dumps(config), now, swarm_key))
        else:
            db_execute_with_retry(
                db,
                'INSERT INTO swarm_sessions (swarm_key, conv_id, task_id, status, '
                'specs_json, config_json, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (swarm_key, conv_id, task_id, status, _dumps(specs),
                 _dumps(config), now, now))
        logger.debug('[SwarmPersist] saved session key=%s status=%s specs=%d',
                     swarm_key, status, len(specs))
    except Exception as e:
        # An INSERT racing another INSERT on the same key hits the PK — that's
        # benign (the row exists), so log at debug and move on. ANY other DB
        # failure means resumable session state was silently lost (a server
        # restart can no longer resume this swarm), which is a data-loss risk
        # — surface it at error so it isn't buried under the benign races.
        _es = str(e).lower()
        if 'duplicate' in _es or 'unique' in _es or 'primary key' in _es:
            logger.debug('[SwarmPersist] save_session(%s) PK race (benign): %s',
                         swarm_key, e)
        else:
            logger.error('[SwarmPersist] save_session(%s) FAILED — resumable '
                         'session state lost: %s', swarm_key, e, exc_info=True)


def mark_session_terminated(swarm_key: str) -> None:
    """Flag the session row terminated (driver thread exited)."""
    if not swarm_key:
        return
    db = _db()
    if db is None:
        return
    try:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(
            db, 'UPDATE swarm_sessions SET status=?, updated_at=? WHERE swarm_key=?',
            ('terminated', _now_ms(), swarm_key))
        logger.debug('[SwarmPersist] session %s → terminated', swarm_key)
    except Exception as e:
        logger.warning('[SwarmPersist] mark_session_terminated(%s) failed: %s',
                       swarm_key, e)


def delete_session(swarm_key: str) -> None:
    """Remove a session and all its agent rows (TTL eviction / abort)."""
    if not swarm_key:
        return
    db = _db()
    if db is None:
        return
    try:
        from lib.database import db_execute_with_retry
        db_execute_with_retry(db, 'DELETE FROM swarm_agents WHERE swarm_key=?',
                              (swarm_key,))
        db_execute_with_retry(db, 'DELETE FROM swarm_sessions WHERE swarm_key=?',
                              (swarm_key,))
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
    db = _db()
    if db is None:
        return
    now = _now_ms()
    msgs_json = _dumps(messages or [])
    res_json = _dumps(result or {})
    try:
        from lib.database import db_execute_with_retry
        exists = db.execute(
            'SELECT delivered FROM swarm_agents WHERE swarm_key=? AND agent_id=?',
            (swarm_key, agent_id)).fetchone()
        if exists:
            if delivered is None:
                db_execute_with_retry(
                    db,
                    'UPDATE swarm_agents SET role=?, objective=?, status=?, '
                    'messages_json=?, result_json=?, rounds_used=?, updated_at=? '
                    'WHERE swarm_key=? AND agent_id=?',
                    (role, objective, status, msgs_json, res_json, rounds_used,
                     now, swarm_key, agent_id))
            else:
                db_execute_with_retry(
                    db,
                    'UPDATE swarm_agents SET role=?, objective=?, status=?, '
                    'messages_json=?, result_json=?, rounds_used=?, delivered=?, '
                    'updated_at=? WHERE swarm_key=? AND agent_id=?',
                    (role, objective, status, msgs_json, res_json, rounds_used,
                     1 if delivered else 0, now, swarm_key, agent_id))
        else:
            db_execute_with_retry(
                db,
                'INSERT INTO swarm_agents (swarm_key, agent_id, role, objective, '
                'status, messages_json, result_json, rounds_used, delivered, '
                'updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (swarm_key, agent_id, role, objective, status, msgs_json,
                 res_json, rounds_used, 1 if (delivered or False) else 0, now))
        logger.debug('[SwarmPersist] saved agent key=%s id=%s status=%s rounds=%d msgs=%d',
                     swarm_key, agent_id, status, rounds_used, len(messages or []))
    except Exception as e:
        # Benign PK race (concurrent INSERT on same key) → debug; any other
        # failure silently loses this agent's resumable checkpoint → error.
        _es = str(e).lower()
        if 'duplicate' in _es or 'unique' in _es or 'primary key' in _es:
            logger.debug('[SwarmPersist] save_agent(%s/%s) PK race (benign): %s',
                         swarm_key, agent_id, e)
        else:
            logger.error('[SwarmPersist] save_agent(%s/%s) FAILED — resumable '
                         'agent checkpoint lost: %s', swarm_key, agent_id, e,
                         exc_info=True)


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
    db = _db()
    if db is None:
        return
    try:
        from lib.database import db_execute_with_retry
        for aid in ids:
            db_execute_with_retry(
                db, 'UPDATE swarm_agents SET delivered=1 WHERE swarm_key=? AND agent_id=?',
                (swarm_key, aid))
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
    db = _db()
    if db is None:
        return []
    out: list[dict] = []
    try:
        srows = db.execute(
            'SELECT swarm_key, conv_id, task_id, status, specs_json, config_json '
            'FROM swarm_sessions').fetchall()
    except Exception as e:
        # Table may not exist on a brand-new DB before migration — benign.
        logger.debug('[SwarmPersist] load_resumable_sessions: no sessions (%s)', e)
        return []

    for s in srows:
        swarm_key = s['swarm_key']
        try:
            arows = db.execute(
                'SELECT agent_id, role, objective, status, messages_json, '
                'result_json, rounds_used, delivered FROM swarm_agents '
                'WHERE swarm_key=?', (swarm_key,)).fetchall()
        except Exception as e:
            logger.warning('[SwarmPersist] could not load agents for %s: %s',
                           swarm_key, e)
            arows = []

        agents = []
        has_nonterminal = False
        has_undelivered = False
        for a in arows:
            st = a['status']
            delivered = bool(a['delivered'])
            if st in _NONTERMINAL:
                has_nonterminal = True
            elif st == 'completed' and not delivered:
                has_undelivered = True
            agents.append({
                'agent_id':    a['agent_id'],
                'role':        a['role'],
                'objective':   a['objective'],
                'status':      st,
                'messages':    _loads(a['messages_json'], []),
                'result':      _loads(a['result_json'], {}),
                'rounds_used': a['rounds_used'] or 0,
                'delivered':   delivered,
            })

        if not (has_nonterminal or has_undelivered):
            continue

        out.append({
            'swarm_key': swarm_key,
            'conv_id':   s['conv_id'] or '',
            'task_id':   s['task_id'] or '',
            'status':    s['status'] or 'running',
            'specs':     _loads(s['specs_json'], []),
            'config':    _loads(s['config_json'], {}),
            'agents':    agents,
        })

    if out:
        logger.info('[SwarmPersist] %d resumable swarm session(s) found on startup',
                    len(out))
    return out
