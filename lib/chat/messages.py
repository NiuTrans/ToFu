"""Chat user-message assembly helpers.

``resolve_conv_refs`` expands conversation @-mentions into formatted text;
``append_user_msg_idempotent`` reconciles an optimistic frontend copy of a
user message with the authoritative server-built one. Moved out of
``routes/chat.py`` so lib-layer callers (the server-side message queue) no
longer import UP into the routes package.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def resolve_conv_refs(conv_refs):
    """Resolve a list of conversation references into formatted text.

    Each ref is ``{id, title}``.  Loads the conversation from DB and formats
    it using ``lib/conv_ref.get_conversation`` (which handles tool details,
    PDFs, truncation, etc.).

    Args:
        conv_refs: List of dicts with ``id`` and ``title`` keys.

    Returns:
        List of ``{id, title, text}`` dicts, one per resolved ref.
    """
    if not conv_refs:
        return []
    from lib.conv_ref import get_conversation
    results = []
    for cr in conv_refs:
        ref_id = cr.get('id', '')
        ref_title = cr.get('title', '')
        if not ref_id:
            continue
        try:
            text = get_conversation(
                conversation_id=ref_id,
                include_tool_details=False,
            )
            results.append({'id': ref_id, 'title': ref_title, 'text': text})
        except Exception as e:
            logger.warning('[Send] Failed to resolve conv ref %s: %s', ref_id[:12], e)
            results.append({'id': ref_id, 'title': ref_title,
                            'text': f'[Error loading conversation: {e}]'})
    logger.info('[Send] Resolved %d conv refs', len(results))
    return results


# Engine-minted user-row flags whose turn can end WITHOUT a persisted
# assistant reply (an empty-'done' task — measured 2026-08-04, conv
# msco7vqmkf8yb2: task 17582690 done in 5s with content_len=0 — an abort, a
# crash). When the NEXT user row lands directly on such a tail, the DB holds
# a persisted user,user adjacency — exactly what the llm_sanitize same-role
# merge kept WARNING-flagging (1,203 hits in ~2.5 days, 100% dispatch-family
# pairs). The merge layer heals the WIRE; this settle heals the STORE.
_ENGINE_USER_FLAGS = ('_brainDispatch', '_isVirtualUser', '_isVuDirective',
                      '_peerMessage')


def is_engine_user_msg(msg) -> bool:
    """The shared predicate: a user-role row minted by an engine seam (brain
    kickoff / VU / peer / workflow), whose turn can end without a persisted
    assistant reply. SINGLE SOURCE — the settle path and the one-time heal
    migration (lib/conversations/engine_tail_heal.py) both ride it."""
    return (isinstance(msg, dict) and msg.get('role') == 'user'
            and any(msg.get(f) for f in _ENGINE_USER_FLAGS))


def build_engine_no_reply_tombstone(now_ms) -> dict:
    """The shared tombstone row for an engine turn that ended with no reply.
    NON-empty content is load-bearing: ``_drop_empty_assistant_messages``
    drops empty assistant rows at wire-build, which would recreate the
    same-role adjacency the tombstone exists to break."""
    return {
        'role': 'assistant',
        'content': '*(该引擎轮未产生回复 — 任务在生成输出前终止 / '
                   'the engine turn ended before producing a reply)*',
        'timestamp': now_ms,
        'finishReason': 'engine_no_output',
        '_engineNoReply': True,
    }


def settle_unanswered_engine_tail(messages, now_ms=None):
    """Append a tombstone assistant row when the tail is an unanswered
    ENGINE-minted user row (brain kickoff / VU / peer / workflow).

    Fires only inside ``append_user_msg_idempotent`` right before a genuinely
    new append (the twin-reconcile branch returns earlier), whose callers run
    only when NO live task can still answer the tail — the queue drain runs
    post-completion, and the /api/chat/send immediate-start branch runs only
    when no task is active. So an engine-flagged user tail here is by
    construction an orphaned turn.

    The tombstone is deliberately NON-empty: ``_drop_empty_assistant_messages``
    at wire-build time would drop an empty row and recreate the adjacency on
    the wire. Human tails are NOT settled: a human question going unanswered
    is a real incident that must stay loud (the merge WARNING), never be
    papered over.

    Returns True iff a tombstone was appended.
    """
    if not messages:
        return False
    tail = messages[-1]
    if not is_engine_user_msg(tail):
        return False
    if now_ms is None:
        import time as _time
        now_ms = int(_time.time() * 1000)
    messages.append(build_engine_no_reply_tombstone(now_ms))
    logger.warning('[Send] Settled an unanswered engine user tail with a '
                   'tombstone assistant row (ts=%s) — prevents a persisted '
                   'user,user adjacency', tail.get('timestamp'))
    return True


def append_user_msg_idempotent(messages, user_msg):
    """Append ``user_msg`` to ``messages`` unless the tail is already it.

    Root-cause guard for the duplicate-user-message bug: the frontend pushes
    an optimistic user message into the in-memory conversation, and a racing
    sync (the "rescue local-only conv" PUT from loadConversationsFromServer,
    another browser tab, or a /chat/send network retry) can plant that
    optimistic copy as the conversation's last row BEFORE this handler runs.
    A blind ``messages.append`` would then produce two user rows for one send.

    The optimistic copy and the server-built ``user_msg`` share the SAME
    ``timestamp`` (the frontend's payload timestamp flows through
    ``_build_user_msg_from_payload``), so we treat a trailing user message
    with a matching timestamp as "the same logical message" and reconcile it
    in place instead of appending a duplicate.  The server copy wins because
    it carries the authoritative translation fields (``content`` =
    translated, ``originalContent``, ``_translateDone``, ``_translateModel``).

    Args:
        messages: The conversation message list (mutated in place).
        user_msg: The freshly-built server-side user message dict.

    Returns:
        True if a new row was appended; False if an existing tail row was
        reconciled (duplicate prevented).
    """
    if messages:
        tail = messages[-1]
        if (isinstance(tail, dict)
                and tail.get('role') == 'user'
                and tail.get('timestamp') == user_msg.get('timestamp')):
            # Same logical message already present (optimistic copy planted by
            # a racing sync). Overwrite it with the authoritative server copy,
            # preserving any stable _msgId already assigned to the tail.
            preserved_id = tail.get('_msgId')
            tail.clear()
            tail.update(user_msg)
            if preserved_id and '_msgId' not in tail:
                tail['_msgId'] = preserved_id
            logger.info('[Send] Reconciled duplicate optimistic user msg in place '
                        '(ts=%s) — prevented duplicate row', user_msg.get('timestamp'))
            return False
    settle_unanswered_engine_tail(messages)
    messages.append(user_msg)
    return True


__all__ = ['resolve_conv_refs', 'append_user_msg_idempotent',
           'settle_unanswered_engine_tail', 'is_engine_user_msg',
           'build_engine_no_reply_tombstone']
