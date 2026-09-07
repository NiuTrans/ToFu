"""Chat turn-building helpers.

Pulls the send-path auto-translate engine, the user-message builder, and the
continue-checkpoint scanner out of ``routes/chat.py``. They have no Flask
request state, so they live in lib; the send translator delegates bounded
work to the process-wide attended-translation lane.

``_TRANSLATE_SEND_TIMEOUT`` is the synchronous translate budget; it must stay
comfortably below the frontend's safety abort timer (``_sendTimeout`` in
static/js/main.js — currently 90 s) so the user sees a clean fallback rather
than a generic AbortError.
"""

import threading
import time
import uuid

from lib.chat.messages import resolve_conv_refs
from lib.log import get_logger
from lib.tool_history_projection import build_tool_history_round
from lib.tool_round_identity import tool_round_batches
from lib.tool_round_replay import scan_replayable_tool_round_prefix

logger = get_logger(__name__)

# Max time (seconds) for the synchronous auto-translate during /api/chat/send.
_TRANSLATE_SEND_TIMEOUT = 45
_TRANSLATE_SEND_INNER_MARGIN_SECONDS = 5.0


def _should_translate_input(text, config):
    """Decide whether ``text`` needs translating to English — the input gate.

    Single source of truth for the "translate vs pass-through" decision shared
    by the UI send path (:func:`auto_translate_user`) and the headless API path
    (:func:`translate_user_text_to_english`). Returns True when a translation
    should be attempted.

    Precedence:
      * An explicit ``config['translateSourceLang']`` wins: translate unless it
        already IS English (the harness/SDK case that knows the language).
      * Otherwise defer to the cascade detector
        (:func:`lib.text_lang.detect_language`): skip only when it confidently
        says English. This REPLACES the old ``is_predominantly_english``
        Latin-ratio gate, which could not tell English from German/Spanish/
        Italian/Portuguese (all ~0.97 Latin) and so wrongly skipped them.

    The LLM-correction tier is consulted only when it is allowed for this
    request — resolved through ``personal_scope`` (fail-closed on headless)
    via :func:`lib.lang_correct.resolve_lang_correction_allowed` — so an
    unrelated API caller is never silently billed for a correction call.
    """
    source_lang = (config.get('translateSourceLang') or '').strip()
    if source_lang:
        return source_lang.lower() not in ('english', 'en')
    from lib.text_lang import detect_language
    from lib.lang_correct import (
        llm_language_corrector, resolve_lang_correction_allowed,
    )
    allow_llm = resolve_lang_correction_allowed(config)
    res = detect_language(
        text, allow_llm=allow_llm,
        llm_corrector=llm_language_corrector if allow_llm else None)
    # Skip translation only on a CONFIDENT English verdict. 'unknown' (no
    # signal) falls through to translate — the translator's own prompt keeps
    # already-English text intact, so a false "translate" is cheap-safe, while
    # a false "skip" would leave a non-English message untranslated.
    return res.code != 'en'


def auto_translate_user(text, config, *, user_id, conv_id=None):
    """Translate non-English user text to English if autoTranslate is on.

    English is the language large models perform best in, so when
    ``autoTranslate`` is enabled we translate the user's input from ANY
    source language into English before it reaches the model (the assistant
    reply is translated back on the output path). Text that is already
    predominantly English is passed through untouched — there's nothing to
    translate, and round-tripping it would only burn an LLM call.

    The source language is taken from ``config['translateSourceLang']`` when
    the caller knows it (e.g. a benchmark harness that knows each instance's
    language); otherwise it is left blank and the translator infers it.

    Capped at ``_TRANSLATE_SEND_TIMEOUT`` seconds to prevent the synchronous
    HTTP handler from blocking long enough to trigger the frontend's abort.

    ``user_id`` is the authenticated owner used for fair admission.
    ``conv_id`` is optional correlation data for the lane job id and logs.

    Returns:
        (translated_text, original_text_or_None, model_or_None, fail_reason)
        where ``fail_reason`` is ``None`` when translation succeeded or was
        not attempted (autoTranslate off / already English), or one of
        ``'timed_out'`` / ``'server_busy'`` / ``'failed'`` when a translation
        WAS attempted but did not produce usable output — the caller surfaces
        this to the user so the original-text fallback is never silent.
    """
    from lib.conv_config import resolve_auto_translate
    auto_translate = resolve_auto_translate(config)
    if not auto_translate or not text:
        return text, None, None, None

    # Target is always English (the model's strongest language).
    # Source language: an explicit hint wins (harness/SDK callers that know it);
    # otherwise blank lets the translate prompt infer it.
    source_lang = (config.get('translateSourceLang') or '').strip()
    # Confidence-aware gate (cascade detector; LLM tier when allowed). Replaces
    # the old Latin-ratio-only heuristic that misread German/Spanish as English.
    if not _should_translate_input(text, config):
        return text, None, None, None

    from concurrent.futures import TimeoutError as FutureTimeoutError
    from lib.agent_core.fair_work_lane import FairWorkLaneQueueFull
    from lib.translate.execution import (
        cancel_attended_translation,
        submit_attended_translation,
    )

    started_at = time.monotonic()
    deadline_at = started_at + _TRANSLATE_SEND_TIMEOUT
    translate_abort = threading.Event()

    def _do_translate():
        from lib.translate import (
            _build_translate_prompt,
            _translate_freetext,
            _extract_notranslate_blocks,
            _reattach_notranslate_blocks,
            _strip_notranslate_tags,
        )
        system_prompt = _build_translate_prompt('English', source_lang)
        # ── Extract <notranslate>/<nt> blocks so the LLM doesn't see the tags ──
        # Without this, the tags leak into the translated English `content`
        # (and stay visible in the "译文" display).
        inner_text, nt_blocks = _extract_notranslate_blocks(text)
        if nt_blocks and not inner_text.strip():
            # Whole message was inside <notranslate> — nothing to translate.
            return _strip_notranslate_tags(text), {'model': 'skipped',
                                                   '_dispatch': {'model': 'skipped'}}
        translate_target = inner_text if nt_blocks else text
        # The inner budget includes time already spent in the shared lane.
        # A margin leaves turn construction safely inside the frontend abort
        # window.
        inner_budget = max(
            0.0,
            deadline_at - time.monotonic()
            - _TRANSLATE_SEND_INNER_MARGIN_SECONDS,
        )
        translated, _u = _translate_freetext(
            translate_target, system_prompt, chunk_label=':send',
            source=source_lang, target='English',
            overall_deadline=inner_budget,
            abort_check=translate_abort.is_set,
        )
        if nt_blocks and translated:
            translated = _reattach_notranslate_blocks(translated, nt_blocks)
        return translated, _u

    # The process-wide lane owns finite worker and queue budgets. The request
    # thread awaits its deadline directly, avoiding the old per-request
    # executor and heartbeat thread.
    job_id = f'send:{conv_id or "detached"}:{uuid.uuid4().hex}'
    future = None
    timed_out = False

    try:
        try:
            future = submit_attended_translation(
                job_id,
                owner_user_id=user_id,
                function=_do_translate,
            )
        except FairWorkLaneQueueFull:
            logger.warning(
                '[Send] Auto-translate queue is full; sending original text '
                '(conv=%s, %d chars)',
                (conv_id or '?')[:8], len(text),
            )
            return text, None, None, 'server_busy'

        while True:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                result, _usage = future.result(timeout=remaining)
                break
            except FutureTimeoutError:
                # A completed worker may itself raise TimeoutError; preserve it
                # as a translation failure rather than mistaking it for a wait.
                if future.done():
                    raise
                timed_out = True
                break

        if timed_out:
            translate_abort.set()
            elapsed = time.monotonic() - started_at
            logger.warning('[Send] Auto-translate timed out after %.1fs, '
                           'sending original text (conv=%s, %d chars)',
                           elapsed, (conv_id or '?')[:8], len(text))
            return text, None, None, 'timed_out'
        if result and result.strip():
            _model = None
            if isinstance(_usage, dict):
                _disp = _usage.get('_dispatch', {})
                _model = _disp.get('model', _usage.get('model'))
            # Strip any <notranslate>/<nt> tags that the LLM may have leaked
            # through (defense in depth — _reattach above already handles the
            # common case, but the LLM sometimes echoes the tags literally).
            from lib.translate import _strip_notranslate_tags
            clean = _strip_notranslate_tags(result.strip()).strip()
            logger.info('[Send] Auto-translated user message: %d→%d chars model=%s nt_stripped=%s',
                        len(text), len(clean), _model, clean != result.strip())
            return clean, text, _model, None
    except Exception as e:
        logger.warning('[Send] Auto-translate failed: %s', e, exc_info=True)
    finally:
        translate_abort.set()
        if future is not None and not future.done():
            cancel_attended_translation(job_id)

    # Reached only when a translation was attempted (autoTranslate on + Chinese
    # present) but produced no usable output — either the LLM call raised or
    # returned empty/whitespace. Distinct from the timeout path above.
    return text, None, None, 'failed'


def translate_user_text_to_english(text, config):
    """Translate ``text`` to English for the headless API path, returning usage.

    Unlike :func:`auto_translate_user` (tuned for the synchronous UI send path
    with its tight abort budget), this is the variant the
    ``/api/v1/chat/completions`` handler uses: a generous deadline, and it
    RETURNS THE TRANSLATION TOKEN
    USAGE so the caller can fold the translate cost into the request's billing
    and cost reporting (English is the model's strongest language, but the
    translate round is a real expense that must be accounted for).

    Returns ``(translated_text, original_or_None, usage_or_None, fail_reason)``.
    ``usage`` is the engine's usage dict (carries ``_dispatch.model`` plus
    token counts) or ``None`` when no translation happened / it failed.
    """
    from lib.conv_config import resolve_auto_translate
    if not resolve_auto_translate(config) or not text:
        return text, None, None, None

    source_lang = (config.get('translateSourceLang') or '').strip()
    # Confidence-aware gate — same single source of truth as the UI send path.
    # A pinned translateSourceLang wins; otherwise the cascade detector decides
    # (skip only on a confident English verdict). This fixes the old
    # Latin-ratio heuristic that wrongly skipped German/Spanish/etc.
    if not _should_translate_input(text, config):
        return text, None, None, None

    from lib.translate import (
        _build_translate_prompt, _translate_freetext,
        _extract_notranslate_blocks, _reattach_notranslate_blocks,
        _strip_notranslate_tags,
    )
    system_prompt = _build_translate_prompt('English', source_lang)
    inner_text, nt_blocks = _extract_notranslate_blocks(text)
    if nt_blocks and not inner_text.strip():
        return _strip_notranslate_tags(text), None, None, None
    translate_target = inner_text if nt_blocks else text
    try:
        translated, usage = _translate_freetext(
            translate_target, system_prompt, chunk_label=':api',
            source=source_lang, target='English')
    except Exception as e:
        logger.warning('[API] input translate failed: %s', e, exc_info=True)
        return text, None, None, 'failed'
    if not translated or not translated.strip():
        return text, None, None, 'failed'
    if nt_blocks:
        translated = _reattach_notranslate_blocks(translated, nt_blocks)
    translated = _strip_notranslate_tags(translated).strip()
    return translated, text, usage, None


def build_user_msg_from_payload(payload, config, *, user_id, conv_id=None):
    """Build a user message dict from frontend payload + optional auto-translate.

    Args:
        payload: dict with text, images, attachments, legacy pdfTexts/videos,
            replyQuotes, convRefs, convRefTexts, timestamp
        config: task config dict (reads autoTranslate)
        user_id: authenticated owner used for referenced-conversation reads.
        conv_id: optional correlation id for translation lane jobs and logs.

    Returns:
        user_msg dict ready to append to conv.messages
    """
    text = payload.get('text', '')
    timestamp = payload.get('timestamp') or int(time.time() * 1000)

    translated_text, original_text, translate_model, translate_fail = auto_translate_user(
        text, config, user_id=user_id, conv_id=conv_id)

    user_msg = {
        'role': 'user',
        'content': translated_text,
        'timestamp': timestamp,
    }
    # Carry the client-generated stable _msgId through verbatim. The frontend
    #   assigns _msgId to the optimistic user message BEFORE the send POST, and
    #   its persistence layer dedups on _msgId (rescue-PUT rebase
    #   _rebaseUnackedTail; PATCH /messages/by-id). If we dropped it here,
    #   _assign_message_ids would mint a DIFFERENT server UUID → on a poor
    #   network where the send succeeded but its response was lost, the client's
    #   rescue-PUT rebase wouldn't recognise the server's copy and would append
    #   the message a SECOND time (duplicate user bubble). Preserving the id
    #   makes server and client agree on one identity for the turn.
    _client_msg_id = payload.get('_msgId')
    if _client_msg_id:
        user_msg['_msgId'] = _client_msg_id
    if original_text:
        user_msg['originalContent'] = original_text
        user_msg['_translateDone'] = True
        if translate_model:
            user_msg['_translateModel'] = translate_model
    elif translate_fail:
        # Auto-translate was attempted (autoTranslate on, Chinese present) but
        # failed/timed out — the ORIGINAL text was sent to the model. Flag it
        # so the frontend can show a non-silent 'sent original' notice.
        user_msg['_translateFailed'] = translate_fail
    if payload.get('images'):
        user_msg['images'] = payload['images']
    if payload.get('attachments'):
        from lib.media_attachments import resolve_client_refs
        attachments = resolve_client_refs(
            payload['attachments'], user_id=user_id)
        if attachments:
            user_msg['attachments'] = attachments
    if payload.get('pdfTexts'):
        user_msg['pdfTexts'] = payload['pdfTexts']
    if payload.get('videos'):
        user_msg['videos'] = _sanitize_video_attachments(payload['videos'])
    if payload.get('replyQuotes'):
        user_msg['replyQuotes'] = payload['replyQuotes']
    if payload.get('convRefs'):
        user_msg['convRefs'] = payload['convRefs']
    # Per-turn context snapshot (workspace/tools/model active when the turn
    # was sent) — opaque to the backend, persisted as-is so the frontend can
    # render the per-turn note after a reload. See static/js/info-rail.js.
    if payload.get('ctx'):
        user_msg['_ctx'] = payload['ctx']
    # Resolve convRefTexts server-side from convRefs if not already provided
    conv_ref_texts = payload.get('convRefTexts')
    if not conv_ref_texts and payload.get('convRefs'):
        conv_ref_texts = resolve_conv_refs(
            payload['convRefs'], user_id=user_id)
    if conv_ref_texts:
        user_msg['convRefTexts'] = conv_ref_texts

    return user_msg


# Known keys of a video attachment (see lib/video_analysis). Anything else the
# client sends is dropped — this payload is persisted and later expanded into
# prompts, so it is treated as untrusted input.
_VIDEO_ATTACHMENT_KEYS = (
    'video_id', 'name', 'video_url', 'poster', 'duration_s', 'width', 'height',
    'fps', 'frame_count', 'avg_frame_bytes',
    'transcript', 'transcript_status', 'transcript_model',
    'storyboard', 'storyboard_model',
)


def _sanitize_video_attachments(videos):
    """Whitelist a client-supplied videos[] payload down to the known shape.

    Frame URLs must be LOCAL durable ``/api/images/`` URLs — a frame is by
    construction a server-side extraction, so a remote URL here is never
    legitimate (and would let a client smuggle arbitrary remote images into
    the prompt).
    """
    out = []
    if not isinstance(videos, list):
        return out
    for v in videos:
        if not isinstance(v, dict):
            continue
        entry = {k: v[k] for k in _VIDEO_ATTACHMENT_KEYS if k in v}
        frames = v.get('frames')
        if isinstance(frames, list):
            entry['frames'] = [
                {'url': f['url'], 't': f.get('t', 0),
                 'bytes': int(f.get('bytes') or 0)}
                for f in frames
                if isinstance(f, dict)
                and isinstance(f.get('url'), str)
                and f['url'].startswith('/api/images/')
            ]
        out.append(entry)
    return out


def scan_continue_checkpoint(assistant_msg):
    """Scan the last assistant message's ``toolRounds`` for the latest recoverable
    checkpoint.  Mirrors ``continueAssistant()`` (static/js/main.js:2214-2410).

    Returns:
        dict with keys:
          kept_rounds (list), discarded_rounds (int),
          tool_history (list), preserved_content (str),
          preserved_thinking_chars (int),
          discarded_content (int), discarded_thinking (int),
          original_content_len (int), original_thinking_len (int)
        OR ``None`` if no recoverable checkpoint (caller falls back to
        full regeneration / pop-and-resend).
    """
    if not isinstance(assistant_msg, dict):
        return None
    raw_rounds = assistant_msg.get('toolRounds')
    all_rounds = raw_rounds if isinstance(raw_rounds, (list, tuple)) else []
    if not all_rounds:
        return None
    replay_prefix = scan_replayable_tool_round_prefix(all_rounds)
    kept_rounds = list(replay_prefix.rounds)

    if not kept_rounds:
        return None
    discarded_rounds = len(all_rounds) - len(kept_rounds)

    # Keep the full checkpoint/audit list on the message, but replay only the
    # newest effective checklist revision to the model.
    from lib.tools.todo import compact_todo_rounds_for_replay
    replay_rounds = compact_todo_rounds_for_replay(kept_rounds)
    # ``llmRound`` restarts at zero for every Continue attempt.  Preserve
    # provider-response chronology; a global dict keyed by that local counter
    # used to merge old/new R17 calls into one synthetic assistant message.
    replay_batches = tool_round_batches(replay_rounds)
    tool_history = [build_tool_history_round(batch)
                    for batch in replay_batches]

    preserved_content_parts = [
        r.get('assistantContent')
        for r in kept_rounds
        if isinstance(r.get('assistantContent'), str)
    ]
    preserved_content = '\n\n'.join(p for p in preserved_content_parts if p)
    raw_content = assistant_msg.get('content')
    original_content = raw_content if isinstance(raw_content, str) else ''
    # Fallback: if assistantContent was never populated on rounds (legacy DB rows),
    # reuse the full prior content so the visible text is preserved.
    if not preserved_content and kept_rounds and original_content:
        preserved_content = original_content
    discarded_content = max(0, len(original_content) - len(preserved_content))
    # The prose tail dropped on rollback — surfaced to the UI as a display-only
    # "Earlier Response" block (priorContent), mirroring discarded_thinking_text.
    # Cannot be replayed on the wire (the model regenerates from the tool-result
    # checkpoint), so it is stripped by _strip_non_api_fields before any LLM call.
    if discarded_content > 0:
        discarded_content_text = (
            original_content[len(preserved_content):].lstrip('\n')
            if original_content.startswith(preserved_content)
            else original_content
        )
    else:
        discarded_content_text = ''

    preserved_thinking_chars = sum(
        len(r.get('thinking'))
        for r in kept_rounds if isinstance(r.get('thinking'), str)
    )
    raw_thinking = assistant_msg.get('thinking')
    original_thinking = raw_thinking if isinstance(raw_thinking, str) else ''
    discarded_thinking = max(0, len(original_thinking) - preserved_thinking_chars)
    # Capture the message-level thinking text whenever it is not fully covered
    # by per-round thinking — this is the trailing reasoning the model emitted
    # after the last completed tool batch.  We can never replay it on the wire
    # (Anthropic rejects orphan thinking blocks; OpenAI-compat strips reasoning
    # server-side), but it is still useful to surface to the user as a
    # display-only "earlier thinking" block on the rolled-back turn.
    discarded_thinking_text = original_thinking if discarded_thinking > 0 else ''

    return {
        'kept_rounds': kept_rounds,
        'discarded_rounds': discarded_rounds,
        'tool_history': tool_history,
        'preserved_content': preserved_content,
        'preserved_thinking_chars': preserved_thinking_chars,
        'discarded_content': discarded_content,
        'discarded_content_text': discarded_content_text,
        'discarded_thinking': discarded_thinking,
        'discarded_thinking_text': discarded_thinking_text,
        'original_content_len': len(original_content),
        'original_thinking_len': len(original_thinking),
    }


__all__ = [
    '_TRANSLATE_SEND_TIMEOUT',
    'auto_translate_user',
    'translate_user_text_to_english',
    'build_user_msg_from_payload',
    'build_tool_history_round',
    'scan_continue_checkpoint',
]
