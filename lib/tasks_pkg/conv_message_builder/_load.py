"""Public entrypoints + authority load for the conversation message builder.

  * ``build_api_messages_from_db`` — load a conversation and build API
    messages.
  * ``build_branch_api_messages`` — build API messages for a branch
    conversation.
  * ``_load_messages_from_db`` — owner-scoped transcript projection helper.
"""

from __future__ import annotations

from lib.log import get_logger

from lib.tasks_pkg.conv_message_builder._transform import _transform_messages

logger = get_logger(__name__)


def build_branch_api_messages(
    conv_id: str,
    msg_idx: int,
    branch_idx: int,
    config: dict,
    *,
    user_id: int,
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
    raw_messages = _load_messages_from_db(conv_id, user_id=user_id)
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

    return _transform_messages(combined, config, user_id=user_id)


def build_api_messages_from_db(
    conv_id: str,
    config: dict,
    *,
    exclude_last: bool = False,
    preloaded_messages: list[dict] | None = None,
    user_id: int,
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
    preloaded_messages : list[dict] | None
        Optional RAW transcript already loaded by the caller. When provided,
        the DB read is skipped and this list is transformed directly.

    Returns
    -------
    list[dict] | None
        API-ready message list, or None if conversation not found.
    """
    # ``preloaded_messages`` lets a request that already loaded the RAW
    # conversation snapshot (e.g. chat_send / chat_start's single-load path)
    # reuse it instead of issuing a second DB read. Other callers keep the
    # live-load fallback.
    if preloaded_messages is not None:
        raw_messages = preloaded_messages
    else:
        raw_messages = _load_messages_from_db(conv_id, user_id=user_id)
        if raw_messages is None:
            return None

    # Lifecycle no-refire note (): a regenerate re-asks the
    # model for the turn the crash interrupted. When the interrupted tail had
    # ALREADY emitted a restart/shutdown-class tool call, a fresh generation
    # must not blindly re-fire it — the command may have succeeded (its result
    # was simply never delivered). Inject a caution into the last user message
    # so every regenerate path (killed-recovery, continueAssistant, autopilot
    # re-drive) inherits the "result unknown — do not re-fire" contract.
    caution = _lifecycle_caution_note(raw_messages) if exclude_last else None

    built = _transform_messages(
        raw_messages, config, exclude_last=exclude_last, user_id=user_id)

    if caution and built:
        if _prepend_note_to_last_user(built, caution):
            logger.warning('[MsgBuilder] conv=%s lifecycle no-refire note '
                           'injected (interrupted tail carried a restart-class '
                           'call)', conv_id[:8])
            try:
                from lib.log import audit_log
                audit_log('lifecycle_recovery_norefire_note', conv_id=conv_id)
            except Exception as _e:
                logger.debug('[MsgBuilder] audit for lifecycle note skipped: %s', _e)

    return built


_LIFECYCLE_CAUTION_NOTE = (
    '[系统提示 / system note] 上一个被中断的回合在结果未知的情况下发出过服务器'
    '重启/关机类指令（restart/shutdown）。该指令可能已经成功执行——严禁再次发出'
    '同类指令；只允许做只读检查（如 data/.server_boots.json、进程与端口状态）确认'
    '当前状态，并向真人用户报告：重启/关机操作必须经真人批准。\n'
    '(The interrupted turn already issued a server restart/shutdown command whose '
    'outcome is unknown — it may have succeeded. Do NOT issue it again: verify '
    'state read-only and report to the human; restart/shutdown requires human '
    'approval.)'
)


def _lifecycle_caution_note(raw_messages: list[dict]) -> str | None:
    """The no-refire note when the interrupted tail carries lifecycle calls.

    Fires only for an interrupted assistant tail (``interruptedReason`` set)
    whose persisted toolRounds contain a restart/shutdown-class command —
    exactly the regenerate situation where a blind re-fire is possible.
    """
    if not raw_messages or not isinstance(raw_messages[-1], dict):
        return None
    tail = raw_messages[-1]
    if tail.get('role') != 'assistant' or not tail.get('interruptedReason'):
        return None
    try:
        from lib.lifecycle_approval import detect_lifecycle_calls
        if detect_lifecycle_calls(tail.get('toolRounds')):
            return _LIFECYCLE_CAUTION_NOTE
    except Exception as e:
        logger.debug('[MsgBuilder] lifecycle-call detection failed: %s', e)
    return None


def _prepend_note_to_last_user(built: list[dict], note: str) -> bool:
    """Prepend ``note`` to the last user message of an API message list.

    Handles both content shapes (plain string and block list). Returns False
    when there is no user message to carry the note (caller then skips).
    """
    for msg in reversed(built or []):
        if not isinstance(msg, dict) or msg.get('role') != 'user':
            continue
        content = msg.get('content')
        if isinstance(content, str):
            msg['content'] = note + '\n\n' + content
            return True
        if isinstance(content, list):
            content.insert(0, {'type': 'text', 'text': note})
            return True
    return False


def _load_messages_from_db(
    conv_id: str,
    *,
    user_id: int,
) -> list[dict] | None:
    """Load canonical messages through the conversation repository."""
    try:
        from lib.conversations.repository import get_conversation
        snapshot = get_conversation(conv_id, user_id=int(user_id))
        if snapshot is None:
            logger.warning('[MsgBuilder] conv=%s not found', conv_id[:8])
            return None
        messages = snapshot.messages
        if not isinstance(messages, list):
            logger.warning('[MsgBuilder] conv=%s messages is not a list: %s',
                           conv_id[:8], type(messages).__name__)
            return None
        return messages
    except Exception as e:
        logger.error('[MsgBuilder] Failed to load conv=%s: %s', conv_id[:8], e, exc_info=True)
        return None
