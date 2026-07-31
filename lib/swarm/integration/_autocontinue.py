"""lib/swarm/integration/_autocontinue.py — Phase-2 auto-continue helpers.

Wakes the main agent when a swarm settles with pending ``<swarm-update>``s but
no live turn to drain them.

The auto-continue *state* (``_autocontinue_chain`` / ``_autocontinue_inflight`` /
``_autocontinue_lock``) lives in ``_state`` and is imported BY REFERENCE — these
dicts/set must be the same objects the rest of the package (and tests, which do
``integ._autocontinue_chain[...]``) mutate.

Patchability note: the swarm-async tests do ``patch.object(integ,
'_key_is_live' / '_start_autocontinue_turn' / 'SWARM_AUTOCONTINUE_ENABLED')`` on
the FACADE package and then call ``integ._maybe_autocontinue(...)``. So
``_maybe_autocontinue`` resolves those three names through the
``lib.swarm.integration`` package namespace at call time (via ``_pkg``), NOT via
a fixed local binding — otherwise the monkeypatch would be a no-op.
"""

from __future__ import annotations

import time

from lib import agent_inbox
from lib.log import get_logger
from lib.swarm.integration._state import (
    _autocontinue_chain,
    _autocontinue_inflight,
    _autocontinue_lock,
)

logger = get_logger(__name__)


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
    # Resolve patchable knobs/functions through the facade package so
    # ``patch.object(lib.swarm.integration, ...)`` in tests takes effect.
    import lib.swarm.integration as _pkg
    if not _pkg.SWARM_AUTOCONTINUE_ENABLED or not swarm_key:
        return
    try:
        # A live turn (the spawning turn hasn't ended yet, or a user just
        # sent another message) will drain the inbox itself — don't race it.
        if _pkg._key_is_live(swarm_key):
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
            if chain >= _pkg.SWARM_AUTOCONTINUE_MAX_CHAIN:
                logger.warning(
                    '[Swarm:%s] autocontinue chain ceiling reached (%d) — '
                    'leaving %d update(s) for the next human turn',
                    swarm_key, _pkg.SWARM_AUTOCONTINUE_MAX_CHAIN,
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
            started = _pkg._start_autocontinue_turn(conv_id)
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
        from lib.conversations.turn_initiation import (INITIATOR_SWARM,
                                                       stamp_initiator)
        stamp_initiator(assistant_msg, INITIATOR_SWARM)
        messages.append(assistant_msg)

        from lib.conversations import build_search_text, update_conversation_fts
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db,
            'UPDATE conversations SET messages=?, updated_at=?, msg_count=?, '
            'search_text=? WHERE id=? AND user_id=1',
            (messages_json, now_ms, len(messages), search_text, conv_id))
        # Phase 5 dual-write (flag-gated, inert when off): tail append.
        # Guarded separately: the placeholder assistant turn is already
        # committed, so a mirror failure must not reach this function's
        # `except` and return False — that would strand the placeholder on
        # disk with no continuation task ever spawned.
        try:
            from lib.database.messages_rows import mirror_write_and_commit
            mirror_write_and_commit(db, conv_id, messages, now_ms=now_ms)
        except Exception as _mirror_err:
            logger.warning('[Swarm:%s] autocontinue row mirror failed '
                           '(non-fatal, turn already durable): %s',
                           conv_id, _mirror_err, exc_info=True)
        try:
            update_conversation_fts(db, conv_id, search_text)
        except Exception as e:
            logger.debug('[Swarm:%s] autocontinue fts update failed: %s', conv_id, e)

        # Event-driven cross-device sync: a brand-new assistant turn was
        # appended, so push the post-write rev → a sibling tab with this conv
        # open shows the new (streaming) bubble without a manual refresh.
        try:
            from lib.conversations import notify_conv_changed
            _ac_rev_row = db.execute(
                'SELECT rev FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)).fetchone()
            notify_conv_changed(conv_id, rev=(_ac_rev_row[0] if _ac_rev_row else None))
        except Exception as _ne:
            logger.debug('[Swarm:%s] autocontinue conv-changed notify skipped: %s',
                         conv_id, _ne)

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

        try:
            # Serialized read-merge-write (settings_store) so this activeTaskId
            # stamp doesn't clobber a concurrent tool-state / autopilot settings
            # write on the same row (reuses this thread's `db`).
            from lib.conversations import set_conversation_settings
            # notify=False: notify_conv_changed already emitted after the
            # messages write above (no double push); gate invalidates cache.
            set_conversation_settings(conv_id, {'activeTaskId': task_id}, db=db,
                                      notify=False)
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
