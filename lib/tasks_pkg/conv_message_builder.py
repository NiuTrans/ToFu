"""Server-side conversation message builder — replaces frontend buildApiMessages().

Loads raw conversation messages from PostgreSQL and transforms them into
the API-ready format that the LLM orchestrator expects.  This eliminates
the need for the frontend to construct messages — the POST body only needs
``{convId, config}``.

The transformations mirror what the old frontend ``buildApiMessages()`` did:
  1. Inject user system prompt (from config)
  2. Skip endpoint-mode display-only messages (_isEndpointPlanner, _isEndpointReview, _epIteration)
  3. Strip <notranslate>/<nt> tags from user text
  4. Prepend reply quotes
  5. Prepend conversation references
  6. Inline PDF text into user content
  7. Build multimodal image blocks (resolve /api/images/ URLs from disk)
  8. Expand stored ``toolRounds`` back into proper OpenAI-style
     ``assistant(tool_calls=[...])`` + ``tool(tool_call_id=..., content=...)``
     message sequences when the rounds have complete info (toolCallId +
     toolContent + status==done).  Falls back to a lossy ``toolSummary``
     JSON placeholder when rounds are legacy/incomplete.  This mirrors
     what ``lib.tasks_pkg.message_builder.inject_tool_history`` produces
     for Continue requests, so the debug preview and the real request
     see the same structure.
  9. Merge consecutive same-role messages (but never across structured
     tool-call sequences).
"""

from __future__ import annotations

import json
import os
import re

from lib.database import DOMAIN_CHAT, get_thread_db
from lib.log import get_logger

logger = get_logger(__name__)

# Regex to strip <notranslate> and <nt> wrapper tags
_NT_RE = re.compile(r'</?(?:notranslate|nt)>', re.IGNORECASE)

# Where uploaded images are stored on disk
_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'uploads')


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
    raw_messages = _load_messages_from_db(conv_id)
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
    raw_messages = _load_messages_from_db(conv_id)
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


def _transform_messages(
    raw_messages: list[dict],
    config: dict,
    *,
    exclude_last: bool = False,
) -> list[dict]:
    """Transform raw conversation messages into API-ready format.

    This is the server-side equivalent of the frontend's buildApiMessages().
    """
    messages = []

    # 1. System prompt from user settings
    sys_prompt = (config.get('systemPrompt') or '').strip()
    if sys_prompt:
        messages.append({'role': 'system', 'content': sys_prompt})

    # Determine source slice — exclude last message if requested
    src = raw_messages[:-1] if (exclude_last and raw_messages) else raw_messages
    # For normal flow: exclude the trailing assistant message (it's the one being generated)
    # The frontend's buildApiMessages did: conv.messages.slice(0, -1)
    # But here we get the FULL DB state. The frontend pushed the empty assistant msg
    # and then sliced it off. Since the empty assistant is persisted after the task starts,
    # we should exclude trailing empty assistant messages.
    if not exclude_last and src:
        last = src[-1]
        if (last.get('role') == 'assistant'
                and not last.get('content')
                and not last.get('toolSummary')
                and not last.get('toolRounds')):
            src = src[:-1]

    # ── Pre-process: collapse historical endpoint sessions ──
    # Historical (completed) endpoint sessions are replaced with just their
    # last worker output so follow-up messages have proper context.
    # The trailing (current/in-progress) session's messages are left as-is
    # for the skip-filter below.
    src = _collapse_historical_endpoint_sessions(src)

    for msg in src:
        # 2. Skip endpoint-mode display-only messages
        #    Only the trailing (current in-progress) endpoint session survives
        #    _collapse_historical_endpoint_sessions — skip all its messages.
        #    _isEndpointReview = critic feedback (role=user)
        #    _isEndpointPlanner = planner output (role=assistant)
        #    _epIteration = worker turn output (role=assistant)
        if msg.get('_isEndpointReview'):
            continue
        if msg.get('_isEndpointPlanner'):
            continue
        if msg.get('_epIteration'):
            continue

        role = msg.get('role', '')

        if role == 'user':
            messages.append(_build_user_message(msg))

        elif role == 'assistant':
            # May expand to multiple messages: assistant(tool_calls) +
            # tool(result) per round — see _build_assistant_messages.
            messages.extend(_build_assistant_messages(msg))

        # Skip other roles (system messages in the middle, etc.)

    # 9. Post-processing: merge consecutive same-role messages
    _merge_consecutive_same_role(messages)

    return messages


def _build_user_message(msg: dict) -> dict:
    """Build a single user message for the API."""
    text_content = msg.get('content') or ''

    # 3. Strip <notranslate>/<nt> wrapper tags
    if '<notranslate>' in text_content or '<nt>' in text_content:
        text_content = _NT_RE.sub('', text_content)

    # 4. Prepend reply quotes
    quotes = msg.get('replyQuotes') or []
    if not quotes and msg.get('replyQuote'):
        quotes = [msg['replyQuote']]
    if quotes:
        if len(quotes) == 1:
            quotes_block = f'[引用]\n{quotes[0]}\n[/引用]'
        else:
            parts = []
            for i, q in enumerate(quotes, 1):
                parts.append(f'[引用{i}]\n{q}\n[/引用{i}]')
            quotes_block = '\n\n'.join(parts)
        text_content = f'{quotes_block}\n\n{text_content}'

    # 5. Prepend conversation references
    conv_ref_texts = msg.get('convRefTexts') or []
    if conv_ref_texts:
        if len(conv_ref_texts) == 1:
            cr = conv_ref_texts[0]
            refs_block = (
                f'[REFERENCED_CONVERSATION title="{cr.get("title", "")}" '
                f'id="{cr.get("id", "")}"]\n{cr.get("text", "")}\n'
                f'[/REFERENCED_CONVERSATION]'
            )
        else:
            parts = []
            for i, cr in enumerate(conv_ref_texts, 1):
                parts.append(
                    f'[REFERENCED_CONVERSATION #{i} title="{cr.get("title", "")}" '
                    f'id="{cr.get("id", "")}"]\n{cr.get("text", "")}\n'
                    f'[/REFERENCED_CONVERSATION]'
                )
            refs_block = '\n\n'.join(parts)
        text_content = (
            f'The user has attached the following conversation(s) for reference:\n\n'
            f'{refs_block}\n\n---\n\n{text_content}'
        )

    # 6. Inline PDF text
    pdf_texts = msg.get('pdfTexts') or []
    for pdf in pdf_texts:
        name = pdf.get('name', 'document.pdf')
        pages = pdf.get('pages', '?')
        text_len = pdf.get('textLength', len(pdf.get('text', '')))
        text = pdf.get('text', '')
        text_content += (
            f'\n\n{"═" * 50}\n'
            f'PDF Document: {name} ({pages} pages, {text_len / 1024:.1f}KB)\n'
            f'{"═" * 50}\n{text}'
        )

    # 7. Build multimodal image blocks
    images = msg.get('images') or []
    has_images = any(img.get('base64') or img.get('url') for img in images)

    if has_images:
        content_blocks = []
        for img in images:
            img_url = ''
            if img.get('base64'):
                media_type = img.get('mediaType', 'image/png')
                img_url = f'data:{media_type};base64,{img["base64"]}'
            elif img.get('url'):
                # Pass through — backend _validate_image_blocks resolves
                # local /api/images/ URLs from disk
                img_url = img['url']

            if img_url:
                content_blocks.append({
                    'type': 'image_url',
                    'image_url': {'url': img_url},
                })
                if img.get('caption'):
                    content_blocks.append({
                        'type': 'text',
                        'text': f'[PDF p{img.get("pdfPage", "?")}: {img["caption"]}]',
                    })
                elif img.get('pdfPage'):
                    content_blocks.append({
                        'type': 'text',
                        'text': f'[PDF page {img["pdfPage"]}/{img.get("pdfTotal", "?")}]',
                    })

        if text_content:
            content_blocks.append({'type': 'text', 'text': text_content})
        return {'role': 'user', 'content': content_blocks}
    else:
        return {'role': 'user', 'content': text_content}


def _build_assistant_messages(msg: dict) -> list[dict]:
    """Build assistant message(s) for the API from a stored conversation row.

    Returns a *list* because a single stored assistant message with tool
    rounds expands into multiple OpenAI-style messages::

        assistant(content=..., tool_calls=[...])    # one per batch
        tool(tool_call_id=..., content=...)         # one per tool call
        tool(tool_call_id=..., content=...)
        ...
        assistant(content=final_text)               # final answer text

    A "batch" is a contiguous group of rounds sharing the same ``llmRound``
    (or, for legacy data without ``llmRound``, separated by a gap of more
    than 1 in ``roundNum``).  This mirrors how the live orchestrator
    emits tool calls — and how ``inject_tool_history`` restores them on
    Continue requests.

    Fallback: if a round is missing the data needed to reconstruct a
    proper tool_call (``toolCallId`` + ``toolContent`` + ``status=='done'``
    + parsable ``toolArgs``), the whole message is collapsed to the
    legacy ``toolSummary`` JSON placeholder — which is lossy but keeps
    parity with older conversations that predate the checkpoint schema.
    """
    rounds = msg.get('toolRounds') or []
    final_content = msg.get('content') or ''
    final_thinking = msg.get('thinking') or ''

    # ── Short-circuit: no tool rounds → single plain assistant message ──
    if not rounds:
        if final_content or final_thinking:
            return [{'role': 'assistant', 'content': final_content or ''}]
        # Empty assistant with nothing at all → preserve as empty placeholder
        # (downstream merge-consecutive may clean it up).
        return [{'role': 'assistant', 'content': ''}]

    # ── Attempt structured reconstruction ──
    structured = _reconstruct_tool_call_messages(rounds)
    if structured is not None:
        # Append the final assistant text (if any) as a trailing message.
        if final_content:
            structured.append({'role': 'assistant', 'content': final_content})
        return structured

    # ── Fallback: legacy / incomplete rounds → summary JSON placeholder ──
    # This path is LOSSY (no tool_call_id/tool role messages) but keeps
    # old conversations working when they lack the required metadata.
    if msg.get('toolSummary'):
        tool_ctx = msg['toolSummary']
    else:
        calls = []
        for r in rounds:
            call = {'name': r.get('toolName', 'unknown')}
            args = r.get('toolArgs')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = None
            if isinstance(args, dict):
                call.update(args)
            elif r.get('query'):
                call['query'] = r['query']
            calls.append(call)
        try:
            tool_ctx = json.dumps(calls, ensure_ascii=False)
        except (TypeError, ValueError):
            tool_ctx = str(calls)

    content = final_content or tool_ctx
    return [{'role': 'assistant', 'content': content}]


def _reconstruct_tool_call_messages(rounds: list[dict]) -> list[dict] | None:
    """Expand ``toolRounds`` into structured assistant/tool message pairs.

    Returns a list of messages on success, or ``None`` if any round
    lacks the data needed to reconstruct a proper tool_call sequence.
    Callers fall back to the legacy summary placeholder on ``None``.

    Required per-round fields:
      * ``toolCallId`` (non-empty str) — uniquely identifies the call
      * ``toolName`` (non-empty str)
      * ``status == 'done'`` — round ran to completion
      * ``toolContent`` (str) — the tool's result as seen by the model

    ``toolArgs`` is best-effort normalized to a JSON string suitable for
    ``function.arguments``.  ``assistantContent`` on the first round of
    a batch becomes the batch's assistant ``content`` (text written
    alongside the tool_calls, à la Claude).
    """
    # First pass: validate every round has the required data.
    for r in rounds:
        if not r.get('toolCallId'):
            return None
        if not r.get('toolName'):
            return None
        if r.get('status') != 'done':
            return None
        if r.get('toolContent') is None:
            return None

    # Group into batches by llmRound (preferred) or roundNum gap (legacy).
    has_llm_round = any(r.get('llmRound') is not None for r in rounds)
    batches: list[list[dict]] = []
    current: list[dict] = []
    prev_key = None
    for r in rounds:
        if has_llm_round:
            key = r.get('llmRound')
        else:
            key = r.get('roundNum')
            if current and isinstance(prev_key, int) and isinstance(key, int):
                # legacy: gap > 1 in roundNum → new batch
                if key > prev_key + 1:
                    batches.append(current)
                    current = []
        if current and has_llm_round and key != prev_key:
            batches.append(current)
            current = []
        current.append(r)
        prev_key = key
    if current:
        batches.append(current)

    out: list[dict] = []
    for batch in batches:
        tool_calls = []
        tool_results = []
        assistant_text = ''
        assistant_thinking = ''
        assistant_thinking_sig = ''
        for r in batch:
            tc_id = r['toolCallId']
            args_raw = r.get('toolArgs')
            if isinstance(args_raw, str):
                args_str = args_raw
            elif isinstance(args_raw, dict):
                try:
                    args_str = json.dumps(args_raw, ensure_ascii=False)
                except (TypeError, ValueError):
                    args_str = '{}'
            else:
                args_str = '{}'
            tc_entry: dict = {
                'id': tc_id,
                'type': 'function',
                'function': {
                    'name': r['toolName'],
                    'arguments': args_str,
                },
            }
            # Gemini: echo back thought_signature verbatim — the OpenAI-compat
            # proxy requires it on every replayed tool_call or returns HTTP 400.
            # Unused by other providers (they strip unknown fields server-side).
            if r.get('extraContent'):
                tc_entry['extra_content'] = r['extraContent']
            tool_calls.append(tc_entry)
            tool_results.append({
                'role': 'tool',
                'tool_call_id': tc_id,
                'content': r['toolContent'] or '',
            })
            # First-seen assistantContent / thinking in the batch become the
            # assistant message's text + reasoning (Claude-style prefix).
            if not assistant_text and r.get('assistantContent'):
                assistant_text = r['assistantContent']
            if not assistant_thinking and r.get('thinking'):
                assistant_thinking = r['thinking']
            if not assistant_thinking_sig and r.get('thinkingSignature'):
                assistant_thinking_sig = r['thinkingSignature']

        asst_msg: dict = {'role': 'assistant', 'tool_calls': tool_calls}
        if assistant_text:
            asst_msg['content'] = assistant_text
        # Only attach thinking block when we have BOTH text and signature —
        # Anthropic rejects a thinking block with no signature; other
        # providers just ignore both fields.  This matches the gating in
        # lib/tasks_pkg/message_builder.inject_tool_history.
        if assistant_thinking and assistant_thinking_sig:
            asst_msg['reasoning_content'] = assistant_thinking
            asst_msg['thinking_signature'] = assistant_thinking_sig
        out.append(asst_msg)
        out.extend(tool_results)

    return out


def _collapse_historical_endpoint_sessions(src: list[dict]) -> list[dict]:
    """Replace completed endpoint sessions with their last worker output.

    An endpoint session is a contiguous block of messages tagged with
    ``_isEndpointPlanner``, ``_isEndpointReview``, or ``_epIteration``.

    - **Historical** sessions (followed by non-endpoint messages): the entire
      block is collapsed to just the last worker output (highest ``_epIteration``),
      stripped of its endpoint marker so downstream treats it as a normal
      assistant message.  This preserves conversation context for follow-up
      questions.
    - **Trailing** session (at the end, no non-endpoint messages after): left
      as-is for the main loop to skip (current in-progress session managed by
      ``endpoint.py``).
    """
    if not src:
        return src

    result = []
    i = 0
    while i < len(src):
        msg = src[i]
        is_ep = (msg.get('_isEndpointReview')
                 or msg.get('_isEndpointPlanner')
                 or msg.get('_epIteration'))

        if not is_ep:
            result.append(msg)
            i += 1
            continue

        # Found an endpoint block — scan to its end
        block_start = i
        last_worker = None
        while i < len(src):
            m = src[i]
            if (not m.get('_isEndpointReview')
                    and not m.get('_isEndpointPlanner')
                    and not m.get('_epIteration')):
                break
            if m.get('_epIteration') and m.get('role') == 'assistant':
                last_worker = m  # track the final worker output
            i += 1

        if i < len(src):
            # Historical block — include the last worker output as normal assistant
            if last_worker:
                clean_worker = dict(last_worker)
                clean_worker.pop('_epIteration', None)
                result.append(clean_worker)
            # else: no worker output (e.g. aborted during planning) — skip entire block
        else:
            # Trailing block — current session, keep as-is for the skip filter
            for j in range(block_start, i):
                result.append(src[j])

    return result


def _merge_consecutive_same_role(messages: list) -> None:
    """Merge consecutive same-role messages in-place.

    After filtering out endpoint-mode messages (_isEndpointPlanner,
    _isEndpointReview, _epIteration), there may still be consecutive
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
