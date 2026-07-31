"""Core transformation pipeline for the conversation message builder.

Transforms raw conversation messages (as stored in the DB) into the
API-ready format the LLM orchestrator expects.  This is the server-side
equivalent of the frontend's old ``buildApiMessages()``.
"""

from __future__ import annotations

import json
import re

from lib.log import get_logger

from lib.tasks_pkg.conv_message_builder._dedup import (
    _collapse_historical_endpoint_sessions,
    _dedup_duplicate_user_messages,
    _merge_consecutive_same_role,
)
from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
)

logger = get_logger(__name__)

# Regex to strip <notranslate> and <nt> wrapper tags
_NT_RE = re.compile(r'</?(?:notranslate|nt)>', re.IGNORECASE)


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

    # ── Pre-process: drop duplicate user rows (same logical message) ──
    # A send-path race (optimistic frontend copy + server-translated copy
    # planted by a racing PUT) or a stale-task resurrection can leave TWO
    # user rows for one logical turn — they share the same ``timestamp`` and
    # the same (or translated-vs-original) content. _merge_consecutive_same_role
    # would CONCATENATE them, doubling the visible user text in context (the
    # "U1 A1 U1 A2" doubled-context bug). Collapse them here, keyed on
    # timestamp, mirroring lib.chat.messages.append_user_msg_idempotent's
    # contract (server copy wins).
    src = _dedup_duplicate_user_messages(src)

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
        # NOTE: autopilot run summaries are NO LONGER messages — they live in
        # the conversation sidecar (settings.autopilotSummaries[runId]), human-
        # only, so there is nothing to skip here. Any legacy `_isAutopilotSummary`
        # assistant row from before the sidecar migration is still skipped below
        # as a defensive guard so old conversations don't replay the report.
        if msg.get('_isAutopilotSummary'):
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

    # 6. Inline PDF/doc text — each block carries a STABLE attachment ref so a
    #    tool (or a later turn) can re-read the full extracted text by that ref
    #    via lib.attachments.resolve_attachment. The ref is a backend-computed
    #    FACT (content hash), never a path the model fabricates.
    pdf_texts = msg.get('pdfTexts') or []
    _pdf_chars_before = len(text_content)
    for pdf in pdf_texts:
        name = pdf.get('name', 'document.pdf')
        pages = pdf.get('pages', '?')
        text_len = pdf.get('textLength', len(pdf.get('text', '')))
        text = pdf.get('text', '')
        try:
            from lib.attachments import attachment_text_ref
            _ref = attachment_text_ref(pdf)
        except Exception as _are:
            logger.debug('[Context] attachment_text_ref failed: %s', _are)
            _ref = ''
        _ref_line = f' [attachment ref: {_ref}]' if _ref else ''
        text_content += (
            f'\n\n{"═" * 50}\n'
            f'PDF Document: {name} ({pages} pages, {text_len / 1024:.1f}KB){_ref_line}\n'
            f'{"═" * 50}\n{text}'
        )
    if pdf_texts:
        logger.debug('[Context] inject block=pdf_inline docs=%d chars=%d',
                     len(pdf_texts), len(text_content) - _pdf_chars_before)

    # 7. Build multimodal image blocks
    images = msg.get('images') or []
    has_images = any(img.get('base64') or img.get('url') for img in images)

    if has_images:
        content_blocks = []
        for img in images:
            img_url = ''
            if img.get('base64'):
                media_type = img.get('mediaType', 'image/png')
                # Source guard: the stored ``mediaType`` is a DB value written
                # by the upload path and is NOT trusted — a PNG saved with
                # mediaType='image/jpeg' (or vice-versa) is where the mislabeled
                # data URI is BORN. Sniff the real format from the base64 prefix
                # and correct it here so the DB never even emits a mismatched
                # URL. The Anthropic Messages API HARD-REJECTS a media-type/bytes
                # mismatch (HTTP 400 "messages.N.content.0.image.source.base64:
                # The image was specified using the image/jpeg media type, but the
                # image does not appear to be in that format."). Best-effort: on
                # any decode failure keep the stored type; payload is untouched.
                try:
                    import base64 as _b64

                    from lib.llm.body import sniff_image_mime
                    _sniffed = sniff_image_mime(_b64.b64decode(img['base64'][:1364]))
                    if _sniffed and _sniffed != media_type:
                        logger.warning(
                            '[MsgBuilder] Corrected mislabeled stored image '
                            'mediaType %r → %r (bytes sniffed as %s) before '
                            'building data URI', media_type, _sniffed, _sniffed)
                        media_type = _sniffed
                except Exception as _mte:
                    logger.debug('[MsgBuilder] image mediaType sniff skipped: %s', _mte)
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
                # Stable re-access ref: prefer the disk-backed /api/images/ URL
                # (survives compaction stripping the inline block); the model
                # passes this to inspect_image to zoom the ORIGINAL. Emitted as
                # a backend FACT so the model never fabricates a path.
                # canonical_image_ref tolerates a reverse-proxy prefix
                # (``/proxy/<port>/api/images/<f>``) and strips it to the
                # canonical ``/api/images/...`` tail — WITHOUT it the guard
                # missed every proxied upload, so no ref hint was emitted and
                # the model fabricated a bogus path from the tool docstring.
                try:
                    from lib.attachments import canonical_image_ref
                    _img_ref = canonical_image_ref(img.get('url') or '')
                except Exception as _cre:
                    logger.debug('[Context] canonical_image_ref failed: %s', _cre)
                    _img_ref = ''
                if _img_ref:
                    content_blocks.append({
                        'type': 'text',
                        'text': f'[image ref: {_img_ref} — call inspect_image with '
                                f'path="{_img_ref}" to zoom/crop the original]',
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
        _n_img = sum(1 for b in content_blocks if b.get('type') == 'image_url')
        logger.debug('[Context] inject block=images count=%d blocks=%d',
                     _n_img, len(content_blocks))
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
        if final_content:
            return [{'role': 'assistant', 'content': final_content}]
        # No rounds, no content — but a legacy `toolSummary` placeholder
        # may still describe what the assistant did. Use it as the body
        # so the model sees something instead of an empty turn.
        if msg.get('toolSummary'):
            return [{'role': 'assistant', 'content': msg['toolSummary']}]
        # Empty assistant with nothing at all — almost always an ERROR GHOST:
        # a failed task persisted content=0 + an error envelope so the UI has
        # a bubble to render. On the wire it carries ZERO information, and
        # strict providers HARD-400 the whole request on it (Kimi/Moonshot:
        # "the message at position N with role 'assistant' must not be empty"
        # — 3 production convs became unretryable on this 2026-07-25).
        # Thinking-only rows land here too: stored `thinking` is never
        # replayed as wire `reasoning_content` for plain turns, so they would
        # serialize as the same empty ghost. Drop the row — adjacent user
        # neighbours are merged downstream by _merge_consecutive_same_role.
        logger.info('[MsgBuilder] Dropping empty assistant row from wire '
                    '(error=%s thinking=%dchars) — strict providers 400 on it',
                    bool(msg.get('error')), len(final_thinking))
        return []

    # ── Attempt structured reconstruction ──
    # Segment-first (epic pt_cb8f98b0cb9b47fb, step 4): when this row carries a
    # persisted `segments` timeline, drive the rebuild FROM the segment
    # structure (rehydrate against toolRounds first so Gemini extraContent is
    # recovered). Byte-identical to the toolRounds path by the reconstructor
    # parity gate; falls through to the toolRounds path for legacy rows with
    # no segments, or if the segment path can't reconstruct (→ None).
    structured = None
    seg_list = msg.get('segments')
    if seg_list:
        try:
            from lib.tasks_pkg.segments import (
                reconstruct_tool_messages_from_segments,
                rehydrate_segments,
            )
            structured = reconstruct_tool_messages_from_segments(
                rehydrate_segments(seg_list, rounds))
        except Exception as _seg_e:
            logger.warning('[MsgBuilder] segment reconstruction failed, '
                           'falling back to toolRounds: %s', _seg_e)
            structured = None
    if structured is None:
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
                except (json.JSONDecodeError, TypeError) as _e_audit:
                    logger.debug('[conv_message_builder] _build_assistant_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
                    args = None
            if isinstance(args, dict):
                call.update(args)
            elif r.get('query'):
                call['query'] = r['query']
            calls.append(call)
        try:
            tool_ctx = json.dumps(calls, ensure_ascii=False)
        except (TypeError, ValueError) as _e_audit:
            logger.debug('[conv_message_builder] _build_assistant_messages caught %s: %s', type(_e_audit).__name__, _e_audit)
            tool_ctx = str(calls)

    content = final_content or tool_ctx
    return [{'role': 'assistant', 'content': content}]
