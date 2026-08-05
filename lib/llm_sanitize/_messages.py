"""lib/llm_sanitize/_messages.py — Structural message-list fixes.

Replaces empty user/tool content with placeholders and merges consecutive
same-role messages — both defensive against Anthropic HTTP 400s.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def _fix_empty_user_messages(messages: list) -> list:
    """Replace empty user/tool message content with a placeholder.

    Anthropic rejects requests where any user message has empty content
    (HTTP 400: ``messages.N: user messages must have non-empty content``).
    Empty content arises from:
      - sanitization stripping every block (e.g. all-image content for a
        non-vision model collapsing to ``''``)
      - failed uploads / transcription saving a blank user turn
      - compaction producing an empty placeholder
      - tool messages whose result was an empty string

    Strategy: scan all user and tool messages; for any whose ``content``
    is empty (``''``, ``[]``, all-empty text blocks, or ``None``), swap in
    a single text block with a non-empty placeholder so the request shape
    stays valid without losing the message's positional meaning (which
    matters for tool_call_id adjacency).

    Mutates messages in-place. Returns the same list for chaining.
    """
    if not messages:
        return messages

    fixed_user = 0
    fixed_tool = 0
    for idx, msg in enumerate(messages):
        role = msg.get('role')
        if role not in ('user', 'tool'):
            continue

        content = msg.get('content')
        is_empty = False
        if content is None:
            is_empty = True
        elif isinstance(content, str):
            if not content.strip():
                is_empty = True
        elif isinstance(content, list):
            if not content:
                is_empty = True
            else:
                # Empty if every block has no meaningful text/data
                has_text = False
                has_non_text = False
                for block in content:
                    if not isinstance(block, dict):
                        has_non_text = True
                        continue
                    btype = block.get('type')
                    if btype == 'text':
                        if (block.get('text') or '').strip():
                            has_text = True
                    else:
                        # image_url, tool_result, etc. — non-empty by virtue of presence
                        has_non_text = True
                if not has_text and not has_non_text:
                    is_empty = True

        if not is_empty:
            continue

        placeholder = ('[empty tool result]' if role == 'tool'
                       else '[empty message]')
        msg['content'] = placeholder
        if role == 'user':
            fixed_user += 1
        else:
            fixed_tool += 1
        logger.warning('[build_body] Replaced empty %s message at index %d '
                       'with placeholder (would trigger Anthropic HTTP 400)',
                       role, idx)

    if fixed_user or fixed_tool:
        logger.info('[build_body] Empty-content fixes: user=%d tool=%d',
                    fixed_user, fixed_tool)

    return messages


def _strip_empty_text_blocks(messages: list) -> list:
    """Remove empty/whitespace-only text blocks from list-form message content.

    A ``{'type': 'text', 'text': ''}`` block carries zero information, but
    strict providers HARD-400 the whole request on it — Kimi/Moonshot:
    ``Invalid request: text content is empty`` (verified in production
    2026-07-31, tasks 93b60577/76d686cb: a virtual-user turn whose user row
    had ``content=''`` was wrapped into ``[{text:''}]`` by the volatile-tail
    reminder seams, then reminder blocks appended — ``_fix_empty_user_messages``
    only fires when EVERY block is empty, so the phantom block sailed through
    and 4,337 retries burned on a deterministic rejection).

    Producers are the context-injection wrap seams (``_refresh_tail_block``,
    ``_refresh_detail_block``, ``_append_user_profile_block``, memory
    ``inject_relevant_memories``) which wrap ``content`` into
    ``[{text: content}]`` unconditionally, plus any frontend-sent multimodal
    message with an empty caption block. This is the single chokepoint that
    heals EVERY producer, present and future.

    When stripping leaves a list empty:
      * a message carrying ``tool_calls`` → drop the content key entirely
        (the proven-accepted no-content shape
        ``build_assistant_tool_call_message`` already emits);
      * anything else → collapse to ``''`` so the whole-content healers
        (``_fix_empty_user_messages`` / ``_drop_empty_assistant_messages``)
        claim it downstream.

    Runs BEFORE those healers in build_body. Mutates in place; returns the
    same list for chaining.
    """
    if not messages:
        return messages

    stripped = 0
    locations = []
    for i, msg in enumerate(messages):
        role = msg.get('role')
        content = msg.get('content')
        if not isinstance(content, list) or not content:
            continue
        kept = []
        for j, block in enumerate(content):
            if (isinstance(block, dict) and block.get('type') == 'text'
                    and not (block.get('text') or '').strip()):
                stripped += 1
                # §2 logging discipline: name WHERE each phantom block lived
                # (message index + role + block index) so the next producer is
                # one grep away, not a guessing session (task 76d686cb's
                # producer was never directly proven — no wire snapshot).
                if len(locations) < 8:
                    locations.append(f'#{i}/{role}/block{j}')
                continue
            kept.append(block)
        if len(kept) == len(content):
            continue
        if kept:
            msg['content'] = kept
        elif msg.get('tool_calls'):
            # assistant(tool_calls) with nothing else — the proven no-content
            # shape, never send content='' or content=[].
            msg.pop('content', None)
        else:
            msg['content'] = ''

    if stripped:
        logger.warning('[build_body] Stripped %d empty text block(s) from '
                       'message content at %s%s — strict providers HTTP 400 '
                       'on them (Kimi: "text content is empty")',
                       stripped, ', '.join(locations),
                       ' …' if stripped > len(locations) else '')
    return messages


def _drop_empty_assistant_messages(messages: list) -> list:
    """Drop assistant messages that carry NOTHING (pure ghosts).

    An assistant message with empty content AND no tool/reasoning fields
    conveys zero information to the model, and strict providers HARD-400 the
    entire request on it — Kimi/Moonshot: ``the message at position N with
    role 'assistant' must not be empty`` (verified in production 2026-07-25:
    three conversations became unretryable until the wire was healed);
    Anthropic likewise rejects empty non-trailing assistant turns.

    Ghosts arise from failed tasks persisting a 0-content error-bubble row,
    thinking-only rows whose thinking is not replayed on the wire, or
    vision-stripping collapsing an all-image assistant turn.

    Kept: any assistant with ``tool_calls``/``function_call`` (a function
    request — required for tool adjacency) or ``reasoning_content`` /
    ``reasoning_details`` / ``thinking_signature`` (a signed thinking block
    the Anthropic/Gemini replay protocol needs).

    Runs BEFORE ``_merge_consecutive_same_role`` so the adjacency the drop
    creates is merged right after. Returns a new list (input untouched).
    """
    if not messages:
        return list(messages)

    def _is_ghost(msg: dict) -> bool:
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            return False
        if (msg.get('tool_calls') or msg.get('function_call')
                or msg.get('reasoning_content') or msg.get('reasoning_details')
                or msg.get('thinking_signature')):
            return False
        content = msg.get('content')
        if content is None:
            return True
        if isinstance(content, str):
            return not content.strip()
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    return False
                if block.get('type') == 'text':
                    if (block.get('text') or '').strip():
                        return False
                else:
                    return False
            return True
        return False

    kept = []
    dropped = 0
    for msg in messages:
        if _is_ghost(msg):
            dropped += 1
            continue
        kept.append(msg)

    if dropped:
        logger.warning('[build_body] Dropped %d empty assistant message(s) '
                       '(pure ghosts — strict providers HTTP 400 on them)',
                       dropped)
    return kept


# Synthetic-context seams that DELIBERATELY place a user message adjacent to
# another user message — for these, the merge below IS the designated final
# assembly step, not a fault (measured 2026-08-03: ~2,000 merge log lines/day,
# 99.9% from these seams):
#   * the CLAUDE.md ``_isMeta`` carrier at index 1
#     (``system_context._insert_user_context_message`` — A/B-measured cache win,
#     see ``build_user_context_reminder``);
#   * the preference-profile blocks riding that carrier;
#   * the per-turn "Recently Modified Files" attachment
#     (``attachments.inject_attachments``);
#   * the coalesced swarm/peer/steer inbox user message
#     (``orchestrator._swarm_inbox.drain_and_inject_inbox``).
# Logging those at INFO was pure noise that also LOOKED like a bug being
# re-patched forever. Any OTHER pair is an unexpected producer (send-race
# duplicate user rows, error-ghost adjacency, endpoint leaks) and alarms at
# WARNING with a location + preview, so the source is one grep away.
_SYNTHETIC_PAIR_PREFIXES = ('<system-reminder>', '<swarm-update>')
_SYNTHETIC_PAIR_MARKERS = (
    '[PROJECT CO-PILOT MODE]',
    '[USER PREFERENCE PROFILE]',
    '## Recently Modified Files',
    '[Peer message from',
)


def _is_synthetic_context_msg(msg) -> bool:
    """True when a message is a synthetic-context injection whose same-role
    adjacency is BY DESIGN (see the seam list above)."""
    content = msg.get('content')
    head = ''
    if isinstance(content, str):
        head = content[:400]
    elif isinstance(content, list):
        for blk in content[:4]:
            if isinstance(blk, dict) and blk.get('type') == 'text':
                head = (blk.get('text') or '')[:400]
                if head:
                    break
    if not head:
        return False
    if head.startswith(_SYNTHETIC_PAIR_PREFIXES):
        return True
    return any(marker in head for marker in _SYNTHETIC_PAIR_MARKERS)


def _pair_preview(msg, limit: int = 60) -> str:
    content = msg.get('content')
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get('type') == 'text':
                parts.append(blk.get('text', '') or '')
        content = ' '.join(p for p in parts if p)
    if not isinstance(content, str):
        content = str(content or '')
    return content.replace('\n', ' ')[:limit]


def _merge_consecutive_same_role(messages: list) -> list:
    """Merge consecutive messages with the same role (except system/tool).

    Endpoint mode can produce consecutive assistant messages (planner + worker)
    in the DB conversation.  If the frontend fails to filter the planner message,
    this backend defense-in-depth merges them by concatenating content.

    Rules:
      - system messages: never merged (each has distinct purpose)
      - tool messages: never merged (each maps to a specific tool_call_id)
      - user/assistant: consecutive same-role messages are merged with \\n\\n separator
      - Messages with tool_calls are never merged (they are function-call requests)

    Observability (epic pt_99eeedbd): merges involving a synthetic-context
    seam (``_is_synthetic_context_msg`` on either side) are BY DESIGN and go
    to DEBUG; anything else is an unexpected producer and fires ONE WARNING
    carrying each merged-away ``#index/role`` + a content preview. The merge
    accumulator is deliberately NOT a laundering channel: once a designed
    pair has fused, the accumulator counts as real content, so a genuine
    duplicate following it still alarms.

    Mutates nothing — returns a new list.
    """
    if not messages or len(messages) < 2:
        return list(messages)

    merged = [messages[0]]
    merged_synthetic = _is_synthetic_context_msg(messages[0])
    merge_count = 0
    expected = 0
    unexpected = []
    for idx, msg in enumerate(messages[1:], start=1):
        role = msg.get('role', '')
        prev_role = merged[-1].get('role', '')

        # Never merge system, tool, or messages with tool_calls
        if (role == prev_role
                and role in ('user', 'assistant')
                and not msg.get('tool_calls')
                and not merged[-1].get('tool_calls')):
            # Merge content by concatenation
            prev_content = merged[-1].get('content', '') or ''
            new_content = msg.get('content', '') or ''
            # Handle multimodal content (list of blocks)
            if isinstance(prev_content, list) or isinstance(new_content, list):
                # Convert both to list form and concatenate
                if isinstance(prev_content, str):
                    prev_content = [{'type': 'text', 'text': prev_content}] if prev_content else []
                if isinstance(new_content, str):
                    new_content = [{'type': 'text', 'text': new_content}] if new_content else []
                merged[-1] = dict(merged[-1])
                merged[-1]['content'] = prev_content + new_content
            else:
                separator = '\n\n' if prev_content and new_content else ''
                merged[-1] = dict(merged[-1])
                merged[-1]['content'] = prev_content + separator + new_content
            merge_count += 1
            curr_synthetic = _is_synthetic_context_msg(msg)
            if merged_synthetic or curr_synthetic:
                expected += 1
            else:
                if len(unexpected) < 6:
                    unexpected.append(f"#{idx}/{role} '{_pair_preview(msg)}'")
            # The accumulator now fuses with real content unless BOTH sides
            # were synthetic — a fused accumulator must not launder a genuine
            # duplicate into the silent path on the next iteration.
            merged_synthetic = merged_synthetic and curr_synthetic
        else:
            merged.append(msg)
            merged_synthetic = _is_synthetic_context_msg(msg)

    if merge_count:
        if unexpected:
            logger.warning('[build_body] Merged %d consecutive same-role message(s) '
                           '(%d → %d messages); %d UNEXPECTED pair(s) at %s — a '
                           'producer is generating same-role adjacency; fix the '
                           'source, not the symptom',
                           merge_count, len(messages), len(merged),
                           len(unexpected), ', '.join(unexpected))
        if expected:
            logger.debug('[build_body] Merged %d designed synthetic-context pair(s) '
                         '(carrier/attachment/inbox seams — silent by design)',
                         expected)
    return merged
