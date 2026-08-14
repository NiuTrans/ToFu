"""Endpoint turn persistence + auto-translation helpers.

Extracted from the monolithic ``lib/tasks_pkg/endpoint.py``.  Ensures
multi-turn endpoint data survives SSE timeouts, page reloads, and server
crashes (``_sync_endpoint_turns_to_conversation`` /
``_store_endpoint_turns_on_task``), and fires server-side auto-translation
per-turn and as an end-of-task safety net (``_trigger_per_turn_auto_translate``
/ ``_trigger_endpoint_auto_translate``).

These four helpers are ALSO imported directly by
``lib/orchestration_endpoint_runner.py`` (the FlowExecutor path) so the
facade must re-export them verbatim.

Dependency direction: leaf module — lazy-imports its collaborators
(``agent_core.store`` / ``tasks_pkg.manager``) inside functions to avoid
import cycles, so ``_run`` can import it freely.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _sync_endpoint_turns_to_conversation(task, endpoint_turns):
    """Write the accumulated endpoint turns into the conversation's messages in the DB.

    In endpoint mode, the planner produces an assistant message, then each
    worker turn produces an assistant message and each critic review produces
    a user message (with _isEndpointReview=true).  These build up over
    multiple iterations.  The frontend creates them via SSE events, but if
    SSE disconnects (timeout, page close, network), the messages only exist
    in JS memory and are never persisted.

    This function writes the full multi-turn structure to the DB so it
    survives SSE disconnects, page reloads, and poll fallback recovery.

    Returns the absolute index in the DB ``messages`` array of the LAST
    appended endpoint turn (i.e. ``endpoint_turns[-1]``), or ``None`` if
    the sync was skipped or failed.  Callers use this to schedule
    per-turn auto-translation right after each turn lands, instead of
    waiting for the end-of-task safety net.
    """
    conv_id = task.get('convId', '')
    tid = task['id'][:8]
    pfx = f'[EndpointSync {tid}]'

    if not endpoint_turns:
        return None

    # V2 owns visible identity in conversation_turns.  The accumulated legacy
    # list is only an executor-local phase snapshot and must never be written
    # back into conversations.messages.
    if task.get('_turnProtocolV2'):
        from lib.turn_lifecycle import sync_visible_run_turns
        return sync_visible_run_turns(task, endpoint_turns)

    try:
        from lib.agent_core.store import get_conversation_store
        store = get_conversation_store()
        loaded = store.load_conversation_messages(conv_id)
        if loaded is None:
            logger.warning('%s conv=%s Conversation not found — cannot sync endpoint turns', pfx, conv_id)
            return None
        messages, _updated_at, load_rev = loaded

        if not messages:
            logger.warning('%s conv=%s Conversation has 0 messages — cannot sync', pfx, conv_id)
            return None

        # The base/endpoint split is expressed as a REPLAYABLE transform so a
        # lost CAS race re-derives it against the fresher transcript instead of
        # overwriting whatever landed meanwhile (an autopilot VU append commits
        # on exactly this boundary). Same code path on the first attempt and on
        # every retry — there is no second copy of the rule to drift.
        def _rebuild(src_messages, _rev):
            original_end = 0
            for i, msg in enumerate(src_messages):
                if (not msg.get('_epIteration') and not msg.get('_isEndpointReview')
                        and not msg.get('_isEndpointPlanner')
                        and not msg.get('_isVirtualUser')):
                    original_end = i + 1
            base = src_messages[:original_end]
            # ★ FIX: Strip trailing assistant messages without endpoint markers.
            # The frontend's startAssistantResponse() creates an empty
            # placeholder that may persist to DB (via syncConversationToServer)
            # before the endpoint sync runs.  In some race conditions, the
            # placeholder may even have content (e.g., planner deltas streamed
            # into it, or worker content copied via loadConversationMessages
            # merge).  Any trailing assistant without _epIteration or
            # _isEndpointPlanner is a ghost and must be removed — the
            # endpoint_turns list has the canonical copies.
            while (base
                   and base[-1].get('role') == 'assistant'
                   and not base[-1].get('_epIteration')
                   and not base[-1].get('_isEndpointPlanner')):
                ghost = base[-1]
                logger.debug('%s conv=%s Removing trailing ghost assistant placeholder '
                             'from base messages (content=%d chars, timestamp=%s)',
                             pfx, conv_id, len(ghost.get('content', '') or ''),
                             ghost.get('timestamp'))
                base.pop()
            return base + endpoint_turns

        new_messages = _rebuild(messages, load_rev)
        base_len = len(new_messages) - len(endpoint_turns)

        store.sync_conversation_with_search(conv_id, new_messages,
                                            expected_rev=load_rev,
                                            rebuild=_rebuild)

        logger.info('%s conv=%s ✅ Synced %d endpoint turns to conversation '
                    '(base=%d + endpoint=%d = %d total msgs)',
                    pfx, conv_id, len(endpoint_turns),
                    base_len, len(endpoint_turns), len(new_messages))
        return len(new_messages) - 1
    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to sync endpoint turns: %s',
                     pfx, conv_id, e, exc_info=True)
        return None


def _store_endpoint_turns_on_task(task, endpoint_turns):
    """Store the endpoint turns snapshot on the task dict for poll access."""
    task['_endpoint_turns'] = list(endpoint_turns)


def _trigger_per_turn_auto_translate(task, turn_msg, msg_idx):
    """Kick off auto-translation for a single endpoint turn that just landed
    in the conversation.  Lets translations run in parallel with the next
    LLM phase (pipelined), instead of all firing serially at the end.

    The end-of-task safety net (``_trigger_endpoint_auto_translate``) still
    runs and dedups against any translate task already in-flight here — so
    a turn that misses this hook (e.g. exception, missing msg_idx) will
    still get translated at the end.

    Parameters
    ----------
    task : dict
        Endpoint task dict (needs ``convId``).
    turn_msg : dict
        The endpoint turn message that was just appended.
    msg_idx : int | None
        Absolute DB index returned by ``_sync_endpoint_turns_to_conversation``.
        If ``None`` the call is a no-op (sync failed; safety net will retry).
    """
    if msg_idx is None:
        return
    conv_id = task.get('convId', '')
    if not conv_id:
        return
    content = turn_msg.get('content') or ''
    if not content:
        return
    role = turn_msg.get('role')
    # A flow-path autopilot VU turn (role=user + _isVirtualUser) is
    # DISPLAY-translated exactly like the live path — route it through the VU
    # safety net (keyed by _msgId), NOT the critic/assistant path.
    is_vu = bool(turn_msg.get('_isVirtualUser')) and role == 'user'
    is_critic = bool(turn_msg.get('_isEndpointReview')) and role == 'user'
    is_planner_or_worker = role == 'assistant' and (
        turn_msg.get('_isEndpointPlanner') or turn_msg.get('_epIteration')
    )
    if not (is_vu or is_critic or is_planner_or_worker):
        return

    try:
        from lib.tasks_pkg.manager import (
            _maybe_auto_translate_assistant,
            _maybe_auto_translate_critic,
        )
    except Exception as e:
        logger.warning('[Endpoint:PerTurnTranslate] task=%s conv=%s '
                       'helper import failed: %s',
                       task.get('id', '?')[:8], conv_id[:8], e)
        return

    try:
        if is_vu:
            from lib.tasks_pkg.autopilot import _maybe_auto_translate_vu
            _maybe_auto_translate_vu(conv_id, turn_msg.get('_msgId') or '',
                                     content)
        elif is_critic:
            _maybe_auto_translate_critic(conv_id, content, msg_idx)
        else:
            _maybe_auto_translate_assistant(conv_id, content, msg_idx)
    except Exception as e:
        logger.warning('[Endpoint:PerTurnTranslate] task=%s conv=%s msg=%s '
                       'failed (non-fatal, safety net will retry): %s',
                       task.get('id', '?')[:8], conv_id[:8], msg_idx, e)


def _trigger_endpoint_auto_translate(task, endpoint_turns):
    """Trigger server-side auto-translation for every assistant turn in an
    endpoint run.

    The single-turn safety net (``_maybe_auto_translate_assistant``) is
    normally invoked from ``_sync_result_to_conversation``, but
    ``persist_task_result`` deliberately skips that path for endpoint tasks
    (the multi-turn sync is done by ``_sync_endpoint_turns_to_conversation``
    instead).  Without this helper, NO endpoint turn — not the planner, not
    any worker iteration — would ever be auto-translated, even when the
    conversation has ``settings.autoTranslate`` ON.

    This helper re-reads the full persisted message list from the DB so it
    can compute the correct ``msg_idx`` for each assistant turn, then calls
    the existing safety-net function once per assistant turn.  The
    safety-net itself handles:
      - per-conversation ``settings.autoTranslate`` gate,
      - already-translated dedup,
      - running frontend-task dedup against ``_translate_tasks``,
      - stale-partial-translation detection,
      - background thread spawning.

    Critic review messages (``role == 'user'``, ``_isEndpointReview``) are
    also translated via ``_maybe_auto_translate_critic`` — same safety-net
    logic, same autoTranslate gate, just annotated with a ``Critic`` log
    prefix for observability.  The critic bubble displays the translation
    via the frontend's updated ``renderMessage`` critic branch.

    Parameters
    ----------
    task : dict
        The endpoint task dict (needs ``convId`` and ``id``).
    endpoint_turns : list
        The final list of endpoint turn messages synced to the DB.
    """
    conv_id = task.get('convId', '')
    tid = task['id'][:8]
    pfx = f'[Endpoint:AutoTranslate {tid}]'

    logger.info('%s conv=%s Entered — endpoint_turns=%d (task._endpoint_turns=%d)',
                pfx, conv_id[:8] if conv_id else '?',
                len(endpoint_turns or []),
                len(task.get('_endpoint_turns') or []))

    if not conv_id:
        logger.warning('%s Missing conv_id — cannot auto-translate', pfx)
        return
    if not endpoint_turns:
        logger.warning('%s conv=%s No endpoint_turns — nothing to auto-translate '
                       '(this may indicate _store_endpoint_turns_on_task was '
                       'never called before _finalize)', pfx, conv_id[:8])
        return

    # Lazy import to avoid circular-import issues between manager <-> endpoint
    try:
        from lib.tasks_pkg.manager import (
            _maybe_auto_translate_assistant,
            _maybe_auto_translate_critic,
        )
    except Exception as e:
        logger.warning('%s conv=%s Failed to import safety-net helper: %s',
                       pfx, conv_id[:8], e)
        return

    try:
        from lib.agent_core.store import get_conversation_store
        loaded = get_conversation_store().load_conversation_messages(conv_id)
        if loaded is None:
            logger.warning('%s conv=%s Conversation not found — skipping auto-translate',
                           pfx, conv_id[:8])
            return
        messages, _updated_at, _rev = loaded

        scheduled = 0
        skipped = 0
        per_role_scheduled = {'planner': 0, 'worker': 0, 'critic': 0}
        for idx, msg in enumerate(messages):
            role = msg.get('role')
            is_planner = bool(msg.get('_isEndpointPlanner'))
            is_worker = bool(msg.get('_epIteration')) and not msg.get('_isEndpointReview')
            is_critic = bool(msg.get('_isEndpointReview')) and role == 'user'
            is_vu = bool(msg.get('_isVirtualUser')) and role == 'user'

            # Only handle engine-produced turns.  Everything else
            # (the original user prompt, any non-endpoint assistant msg,
            # etc.) is skipped silently.
            if not (is_planner or is_worker or is_critic or is_vu):
                continue

            content = msg.get('content') or ''
            if not content:
                skipped += 1
                continue
            # Skip image-generation outputs (nothing to translate) — guard
            # replicated for the critic path even though critics never emit
            # image-gen markers today.
            if msg.get('_igResult') or msg.get('_isImageGen'):
                skipped += 1
                continue

            try:
                if is_planner:
                    ep_tag = 'planner'
                elif is_worker:
                    ep_tag = f"worker#{msg.get('_epIteration')}"
                elif is_vu:
                    ep_tag = 'vu'
                else:
                    ep_tag = 'critic'

                logger.info('%s conv=%s turn=%d role=%s ep=%s len=%d — scheduling auto-translate',
                            pfx, conv_id[:8], idx, role, ep_tag, len(content))

                if is_vu:
                    from lib.tasks_pkg.autopilot import _maybe_auto_translate_vu
                    _maybe_auto_translate_vu(conv_id, msg.get('_msgId') or '',
                                             content)
                    per_role_scheduled['vu'] = per_role_scheduled.get('vu', 0) + 1
                elif is_critic:
                    _maybe_auto_translate_critic(conv_id, content, idx)
                    per_role_scheduled['critic'] += 1
                else:
                    _maybe_auto_translate_assistant(conv_id, content, idx)
                    if is_planner:
                        per_role_scheduled['planner'] += 1
                    else:
                        per_role_scheduled['worker'] += 1
                scheduled += 1
            except Exception as e:
                logger.warning('%s conv=%s turn=%d auto-translate trigger failed: %s',
                               pfx, conv_id[:8], idx, e)

        logger.info('%s conv=%s Done — scheduled=%d (planner=%d worker=%d critic=%d) '
                    'skipped=%d (messages=%d)',
                    pfx, conv_id[:8], scheduled,
                    per_role_scheduled['planner'], per_role_scheduled['worker'],
                    per_role_scheduled['critic'],
                    skipped, len(messages))
    except Exception as e:
        logger.error('%s conv=%s ❌ Failed to trigger endpoint auto-translate: %s',
                     pfx, conv_id[:8], e, exc_info=True)
