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
    messages.append(user_msg)
    return True


__all__ = ['resolve_conv_refs', 'append_user_msg_idempotent']
