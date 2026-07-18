"""Public entrypoints + DB load for the conversation message builder.

  * ``build_api_messages_from_db`` — load a conversation and build API
    messages.
  * ``build_branch_api_messages`` — build API messages for a branch
    conversation.
  * ``_load_messages_from_db`` — raw PostgreSQL load helper.
"""

from __future__ import annotations

import json

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger

from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

logger = get_logger(__name__)


def _load_via_facade(conv_id: str) -> list[dict] | None:
    """Resolve ``_load_messages_from_db`` through the package facade.

    The public entrypoints look the loader up on the package module at call
    time (rather than binding the local reference) so tests that patch
    ``lib.tasks_pkg.conv_message_builder._load_messages_from_db`` — as they
    did against the pre-split monolithic module — keep working.
    """
    import lib.tasks_pkg.conv_message_builder as _facade
    return _facade._load_messages_from_db(conv_id)


def build_branch_api_messages(
    conv_id: str,
    msg_idx: int,
    branch_idx: int,
    config: dict,
) -> list[dict] | None:
    """Build API-ready messages for a branch conversation.

    Loads the main conversation from DB, extracts context up to the branch
    anchor point, appends the branch's own messages (decorated with topic
    and selection context), then runs the standard ``_transform_messages``
    pipeline.

    Parameters
    ----------
    conv_id : str
        Parent conversation ID.
    msg_idx : int
        Index of the message in the parent conversation that the branch
        is attached to.
    branch_idx : int
        Index of the branch within ``messages[msg_idx].branches``.
    config : dict
        Task config dict (reads ``systemPrompt``).

    Returns
    -------
    list[dict] | None
        API-ready message list, or None if conversation/branch not found.
    """
    raw_messages = _load_via_facade(conv_id)
    if raw_messages is None:
        return None

    # Validate indices
    if msg_idx < 0 or msg_idx >= len(raw_messages):
        logger.warning('[MsgBuilder] Branch msg_idx=%d out of range (conv=%s, len=%d)',
                       msg_idx, conv_id[:8], len(raw_messages))
        return None

    parent_msg = raw_messages[msg_idx]
    branches = parent_msg.get('branches') or []
    if branch_idx < 0 or branch_idx >= len(branches):
        logger.warning('[MsgBuilder] Branch branch_idx=%d out of range (conv=%s, msg_idx=%d, len=%d)',
                       branch_idx, conv_id[:8], msg_idx, len(branches))
        return None

    branch = branches[branch_idx]
    branch_msgs = branch.get('messages') or []

    # ── Determine context cut-off in the main conversation ──
    # Context = all completed rounds BEFORE the round being branched.
    # Branching from assistant at index N: include up to the user message
    # before N (exclude the user message that triggered the assistant reply).
    if parent_msg.get('role') == 'assistant' and msg_idx > 0:
        context_end = msg_idx
        for j in range(msg_idx - 1, -1, -1):
            if raw_messages[j].get('role') == 'user':
                context_end = j
            else:
                break
    else:
        context_end = msg_idx

    main_context = raw_messages[:context_end]

    # ── Branch messages: exclude the trailing empty assistant placeholder ──
    trimmed_branch = list(branch_msgs)
    if trimmed_branch:
        last = trimmed_branch[-1]
        if (last.get('role') == 'assistant'
                and not last.get('content')
                and not last.get('toolSummary')
                and not last.get('toolRounds')):
            trimmed_branch = trimmed_branch[:-1]

    # ── Decorate the first branch user message with topic + selection context ──
    decorated_branch = []
    for k, m in enumerate(trimmed_branch):
        if k == 0 and m.get('role') == 'user':
            m = dict(m)  # copy to avoid mutating original
            prefix = f'[分支话题: {branch.get("title", "")}]'
            parent_selection = branch.get('parentSelection', '')
            if parent_selection:
                prefix += (f'\n[选中的上下文]\n'
                           f'{parent_selection[:2000]}\n[/选中的上下文]')
            m['content'] = f'{prefix}\n{m.get("content", "")}'
            decorated_branch.append(m)
        else:
            decorated_branch.append(m)

    # ── Combine and transform ──
    combined = main_context + decorated_branch
    logger.info('[MsgBuilder] Branch conv=%s msg=%d branch=%d: context=%d + branch=%d msgs',
                conv_id[:8], msg_idx, branch_idx, len(main_context), len(decorated_branch))

    return _transform_messages(combined, config)


def build_api_messages_from_db(
    conv_id: str,
    config: dict,
    *,
    exclude_last: bool = False,
) -> list[dict] | None:
    """Load conversation messages from DB and build API-ready messages.

    Parameters
    ----------
    conv_id : str
        Conversation ID to load from.
    config : dict
        Task config dict (reads ``systemPrompt``).
    exclude_last : bool
        If True, exclude the last message (used by continueAssistant where
        the last assistant message is the one being regenerated).

    Returns
    -------
    list[dict] | None
        API-ready message list, or None if conversation not found.
    """
    raw_messages = _load_via_facade(conv_id)
    if raw_messages is None:
        return None

    return _transform_messages(raw_messages, config, exclude_last=exclude_last)


def _load_messages_from_db(conv_id: str) -> list[dict] | None:
    """Load raw messages from PostgreSQL for a conversation."""
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        if not row:
            logger.warning('[MsgBuilder] conv=%s not found in DB', conv_id[:8])
            return None
        messages = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        if not isinstance(messages, list):
            logger.warning('[MsgBuilder] conv=%s messages is not a list: %s',
                           conv_id[:8], type(messages).__name__)
            return None
        return messages
    except Exception as e:
        logger.error('[MsgBuilder] Failed to load conv=%s: %s', conv_id[:8], e, exc_info=True)
        return None
