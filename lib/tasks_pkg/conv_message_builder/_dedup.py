"""Pre-/post-processing passes for the conversation message builder.

  * ``_dedup_duplicate_user_messages`` — collapse same-timestamp duplicate
    user rows (send-path race / stale-task resurrection guard).
  * ``_collapse_historical_flow_sessions`` — replace completed Flow sessions
    with their last worker output.
  * ``_merge_consecutive_same_role`` — merge consecutive same-role messages
    (never across structured tool-call sequences).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _dedup_duplicate_user_messages(src: list[dict]) -> list[dict]:
    """Drop duplicate user rows that represent the SAME logical send turn.

    Root cause this defends against: during the synchronous send-path
    auto-translate window, a racing conversation sync (the "rescue
    local-only conv" PUT, another browser tab, or a /chat/send retry) can
    plant the optimistic frontend copy of a user message into the DB while
    the server is still building/translating its own authoritative copy.
    Both rows share the SAME ``timestamp`` (the frontend payload timestamp
    flows through ``build_user_msg_from_payload``). A stale aborted task
    can similarly resurrect a just-truncated user turn.

    ``append_user_msg_idempotent`` reconciles this at the *write* site, but
    a row already corrupted before that fix shipped — or planted by a writer
    that bypasses the helper — still lives in the DB. Without this pass,
    ``_merge_consecutive_same_role`` would *concatenate* the two user rows
    (or carry both across an empty assistant), doubling the user's text in
    the LLM context.

    Strategy: keep only the LAST user row in any run of consecutive user
    rows that share a ``timestamp`` (the server-translated copy is appended
    after the optimistic one, so the last wins — it carries the
    authoritative ``content`` + translation fields). Flow-review user rows
    (``_isFlowReview``) are never collapsed — they legitimately repeat.

    Args:
        src: Raw conversation messages (mutated copy returned, input untouched).

    Returns:
        A new list with duplicate same-timestamp user rows collapsed.
    """
    if not src:
        return src

    result: list[dict] = []
    dropped = 0
    for msg in src:
        if (isinstance(msg, dict)
                and msg.get('role') == 'user'
                and not msg.get('_isFlowReview')
                and result):
            prev = result[-1]
            if (isinstance(prev, dict)
                    and prev.get('role') == 'user'
                    and not prev.get('_isFlowReview')
                    and msg.get('timestamp') is not None
                    and prev.get('timestamp') == msg.get('timestamp')):
                # Same logical turn — server copy (this one) supersedes the
                # optimistic copy already in `result`. Replace in place.
                result[-1] = msg
                dropped += 1
                continue
        result.append(msg)

    if dropped:
        logger.warning('[MsgBuilder] Collapsed %d duplicate same-timestamp user '
                       'row(s) — prevented doubled user turn in context', dropped)
    return result


def _collapse_historical_flow_sessions(src: list[dict]) -> list[dict]:
    """Replace completed Flow sessions with their last worker output.

    A Flow session is a contiguous block of messages tagged with
    ``_isFlowPlanner``, ``_isFlowReview``, or ``_flowIteration``.

    - **Historical** sessions (followed by non-Flow messages): the entire
      block is collapsed to just the last worker output (highest ``_flowIteration``),
      stripped of its Flow marker so downstream treats it as a normal
      assistant message.  This preserves conversation context for follow-up
      questions.
    - **Trailing** session (at the end, no non-Flow messages after): left
      as-is for the main loop to skip.
    """
    if not src:
        return src

    result = []
    i = 0
    while i < len(src):
        msg = src[i]
        is_flow = (msg.get('_isFlowReview')
                   or msg.get('_isFlowPlanner')
                   or msg.get('_flowIteration'))

        if not is_flow:
            result.append(msg)
            i += 1
            continue

        # Found a Flow block — scan to its end
        block_start = i
        last_worker = None
        while i < len(src):
            m = src[i]
            if (not m.get('_isFlowReview')
                    and not m.get('_isFlowPlanner')
                    and not m.get('_flowIteration')):
                break
            if m.get('_flowIteration') and m.get('role') == 'assistant':
                last_worker = m  # track the final worker output
            i += 1

        if i < len(src):
            # Historical block — include the last worker output as normal assistant
            if last_worker:
                clean_worker = dict(last_worker)
                clean_worker.pop('_flowIteration', None)
                result.append(clean_worker)
            # else: no worker output (e.g. aborted during planning) — skip entire block
        else:
            # Trailing block — current session, keep as-is for the skip filter
            for j in range(block_start, i):
                result.append(src[j])

    return result


def _merge_consecutive_same_role(messages: list) -> None:
    """Merge consecutive same-role messages in-place.

    After filtering out Flow-owned messages (_isFlowPlanner,
    _isFlowReview, _flowIteration), there may still be consecutive
    same-role messages from normal conversation flow. Merge by concatenation.

    NEVER merges structured tool-call messages (those with ``tool_calls``
    or ``tool_call_id``) — those must remain intact for the model to
    correlate calls and results.
    """
    i = len(messages) - 1
    while i > 0:
        curr = messages[i]
        prev = messages[i - 1]
        # Do not collapse structured tool-call sequences
        if (curr.get('tool_calls') or prev.get('tool_calls')
                or curr.get('tool_call_id') or prev.get('tool_call_id')):
            i -= 1
            continue
        if (curr.get('role') == prev.get('role')
                and curr.get('role') in ('user', 'assistant')):
            prev_content = prev.get('content', '') or ''
            curr_content = curr.get('content', '') or ''
            # Handle multimodal content (arrays)
            if isinstance(prev_content, list) or isinstance(curr_content, list):
                if isinstance(prev_content, str):
                    prev_content = [{'type': 'text', 'text': prev_content}] if prev_content else []
                if isinstance(curr_content, str):
                    curr_content = [{'type': 'text', 'text': curr_content}] if curr_content else []
                messages[i - 1] = dict(prev)
                messages[i - 1]['content'] = prev_content + curr_content
            else:
                sep = '\n\n' if prev_content and curr_content else ''
                messages[i - 1] = dict(prev)
                messages[i - 1]['content'] = prev_content + sep + curr_content
            messages.pop(i)
        i -= 1
