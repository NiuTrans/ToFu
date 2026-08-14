"""Chat turn-building helpers.

Pulls the send-path auto-translate engine, the user-message builder, and the
continue-checkpoint scanner out of ``routes/chat.py``. These are pure data
transforms over plain dicts (plus a per-conv status side-table consumed by a
status-poll endpoint) — no Flask request state — so they live in lib.

``_TRANSLATE_SEND_TIMEOUT`` is the synchronous translate budget; it must stay
comfortably below the frontend's safety abort timer (``_sendTimeout`` in
static/js/main.js — currently 90 s) so the user sees a clean fallback rather
than a generic AbortError.
"""

import threading
import time

from lib.chat.messages import resolve_conv_refs
from lib.log import get_logger

logger = get_logger(__name__)

# Max time (seconds) for the synchronous auto-translate during /api/chat/send.
_TRANSLATE_SEND_TIMEOUT = 45


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


# ══════════════════════════════════════════════════════════
#  Send-path translate status (per-conv)
#
#  The atomic /api/chat/send handler translates synchronously, blocking the
#  HTTP response until the translate either succeeds or hits
#  _TRANSLATE_SEND_TIMEOUT. If the backend is retrying (429, empty output),
#  the user sees only the frontend "Translating…" bubble with no progress
#  clue. The status side-table lets a tiny poll endpoint surface the current
#  retry reason underneath the bubble while the send call is in flight.
# ══════════════════════════════════════════════════════════
_send_translate_status = {}           # conv_id -> {statusMessage, statusKind, updatedAt}
_send_translate_status_lock = threading.Lock()


def set_send_translate_status(conv_id, event):
    """Record a status event for an in-flight send-path translate.

    Called from the _translate_one_chunk status callback. ``event`` is a
    dict carrying ``kind``, ``attempt``, ``elapsed``, ``detail`` — see
    ``lib.translate._format_status_message``.
    """
    if not conv_id:
        return
    try:
        from lib.translate import _format_status_message
        msg = _format_status_message(event)
    except Exception as e:
        logger.debug('[Send] _format_status_message failed: %s', e)
        msg = event.get('kind', '')
    with _send_translate_status_lock:
        _send_translate_status[conv_id] = {
            'statusMessage': msg,
            'statusKind': event.get('kind', ''),
            'updatedAt': time.time(),
        }


def clear_send_translate_status(conv_id):
    if not conv_id:
        return
    with _send_translate_status_lock:
        _send_translate_status.pop(conv_id, None)


def get_send_translate_status(conv_id):
    """Return the current send-path translate status for a conv (or None)."""
    with _send_translate_status_lock:
        return _send_translate_status.get(conv_id)


def auto_translate_user(text, config, conv_id=None):
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

    When ``conv_id`` is provided, transient retry statuses are published
    to ``_send_translate_status[conv_id]`` so the frontend can poll for
    them and surface them below the "Translating…" bubble.

    Returns:
        (translated_text, original_text_or_None, model_or_None, fail_reason)
        where ``fail_reason`` is ``None`` when translation succeeded or was
        not attempted (autoTranslate off / already English), or one of
        ``'timed_out'`` / ``'failed'`` when a translation WAS attempted but
        did not produce usable output — the caller surfaces this to the user
        so the silent original-text fallback is no longer invisible.
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

    import concurrent.futures

    def _status_cb(event):
        """Forward chunk-level status events into the per-conv dict."""
        set_send_translate_status(conv_id, event)

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
        # Tighter inner deadline than the outer wait — leaves a small
        # margin for status publication and pool teardown so the HTTP
        # response arrives well before the frontend safety abort.
        translated, _u = _translate_freetext(
            translate_target, system_prompt, chunk_label=':send',
            source=source_lang, target='English',
            status_cb=_status_cb if conv_id else None,
            overall_deadline=max(5.0, _TRANSLATE_SEND_TIMEOUT - 5),
        )
        if nt_blocks and translated:
            translated = _reattach_notranslate_blocks(translated, nt_blocks)
        return translated, _u

    # Build the executor manually — a `with` block calls shutdown(wait=True)
    # on exit, which would block the HTTP request until the worker actually
    # finishes (up to _translate_one_chunk's internal 10-min retry deadline)
    # and defeat the whole point of the timeout. We tear it down with
    # wait=False / cancel_futures=True so the request returns as soon as the
    # timeout hits, even if the worker thread is mid-LLM-call.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = None
    timed_out = False
    started_at = time.time()
    # Heartbeat: if the inner translate hasn't surfaced its own status yet,
    # publish a "still translating" event every few seconds so the polling
    # frontend doesn't show a static "Translating…" with no clue.
    _heartbeat_stop = threading.Event()

    def _heartbeat():
        while not _heartbeat_stop.wait(4.0):
            try:
                set_send_translate_status(conv_id, {
                    'kind': 'in_progress',
                    'attempt': 0,
                    'elapsed': time.time() - started_at,
                    'detail': '',
                })
            except Exception as e:
                logger.debug('[Send] heartbeat status publish failed conv=%s: %s',
                             (conv_id or '?')[:8], e)

    _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    if conv_id:
        _hb_thread.start()

    try:
        future = pool.submit(_do_translate)
        try:
            result, _usage = future.result(timeout=_TRANSLATE_SEND_TIMEOUT)
        except concurrent.futures.TimeoutError:
            timed_out = True
            elapsed = time.time() - started_at
            logger.warning('[Send] Auto-translate timed out after %.1fs, '
                           'sending original text (conv=%s, %d chars)',
                           elapsed, (conv_id or '?')[:8], len(text))
            # Surface the timeout to the frontend BEFORE we clear the
            # status dict in `finally` — the poll loop will pick it up
            # on the very next tick and the user sees a concrete reason.
            set_send_translate_status(conv_id, {
                'kind': 'timed_out',
                'attempt': 1,
                'elapsed': elapsed,
                'detail': f'no result after {_TRANSLATE_SEND_TIMEOUT}s',
            })
            # Do NOT block on shutdown — the worker may still be mid-LLM
            # call. cancel_futures requires Python 3.9+; we already require it.
            pool.shutdown(wait=False, cancel_futures=True)
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
        _heartbeat_stop.set()
        # Tear down the pool. When we timed out we already shut it down
        # non-blocking above. On the success / generic-error paths the
        # future is finished, so a regular shutdown(wait=False) returns
        # immediately and the worker thread is collected by the GC.
        if not timed_out:
            try:
                pool.shutdown(wait=False)
            except Exception as e:
                logger.debug('[Send] translate pool shutdown failed: %s', e)
        # Always clear the per-conv status on exit — no stale retry hint
        # should leak into a subsequent poll after the send returns.
        clear_send_translate_status(conv_id)

    # Reached only when a translation was attempted (autoTranslate on + Chinese
    # present) but produced no usable output — either the LLM call raised or
    # returned empty/whitespace. Distinct from the timeout path above.
    return text, None, None, 'failed'


def translate_user_text_to_english(text, config):
    """Translate ``text`` to English for the headless API path, returning usage.

    Unlike :func:`auto_translate_user` (tuned for the synchronous UI send path
    with its heartbeat/status side-table and tight abort budget), this is the
    variant the ``/api/v1/chat/completions`` handler uses: no per-conv status
    side effects, a generous deadline, and it RETURNS THE TRANSLATION TOKEN
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


def build_user_msg_from_payload(payload, config, conv_id=None):
    """Build a user message dict from frontend payload + optional auto-translate.

    Args:
        payload: dict with text, images, pdfTexts, replyQuotes, convRefs, convRefTexts, timestamp
        config: task config dict (reads autoTranslate)
        conv_id: optional — when provided, transient translate retry
            statuses are exposed via /api/chat/translate-status/<conv_id>
            so the frontend can display retry reasons under the "Translating…"
            bubble.

    Returns:
        user_msg dict ready to append to conv.messages
    """
    text = payload.get('text', '')
    timestamp = payload.get('timestamp') or int(time.time() * 1000)

    translated_text, original_text, translate_model, translate_fail = auto_translate_user(
        text, config, conv_id=conv_id)

    user_msg = {
        'role': 'user',
        'content': translated_text,
        'timestamp': timestamp,
    }
    # ★ Carry the client-generated stable _msgId through verbatim. The frontend
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
        conv_ref_texts = resolve_conv_refs(payload['convRefs'])
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


def build_tool_history_round(batch):
    """Server-side port of ``_buildToolHistoryRound()`` (static/js/main.js).

    Takes a batch of raw ``toolRounds`` entries (all from the same LLM round)
    and converts them into the ``toolHistory[i]`` shape consumed by
    ``lib/tasks_pkg/message_builder.inject_tool_history``.
    """
    round_out: dict = {
        'assistantContent': '',
        'toolCalls': [],
        'toolResults': [],
    }
    for r in batch:
        if not round_out['assistantContent'] and r.get('assistantContent'):
            round_out['assistantContent'] = r.get('assistantContent')
        if not round_out.get('thinking') and r.get('thinking'):
            round_out['thinking'] = r.get('thinking')
        if not round_out.get('thinkingSignature') and r.get('thinkingSignature'):
            round_out['thinkingSignature'] = r.get('thinkingSignature')
        tc = {
            'id': r.get('toolCallId'),
            'name': r.get('toolName'),
            'arguments': r.get('toolArgs') or '{}',
        }
        if r.get('extraContent'):
            tc['extraContent'] = r.get('extraContent')
        if isinstance(r.get('caller'), dict):
            tc['caller'] = dict(r['caller'])
        round_out['toolCalls'].append(tc)
        result = {
            'tool_call_id': r.get('toolCallId'),
            'content': r.get('toolContent') or '',
        }
        if isinstance(r.get('caller'), dict):
            result['caller'] = dict(r['caller'])
        round_out['toolResults'].append(result)
    return round_out


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
    all_rounds = assistant_msg.get('toolRounds') or []
    if not all_rounds:
        return None
    has_tool_call_ids = any(r.get('toolCallId') for r in all_rounds)
    if not has_tool_call_ids:
        return None

    has_llm_round = any(r.get('llmRound') is not None for r in all_rounds)
    batches: dict = {}
    batch_key = 0
    last_complete_idx = -1

    for i, r in enumerate(all_rounds):
        if not r.get('toolCallId'):
            continue
        if r.get('status') != 'done':
            break
        # Attempt to reconstruct toolContent from results metadata if missing
        # (parity with the JS scan — happens after DB round-trip when backend
        # checkpoint was written before toolContent was available).
        if r.get('toolContent') is None:
            results = r.get('results') or []
            reconstructed = ''
            if results:
                parts = []
                for res in results:
                    if not isinstance(res, dict):
                        continue
                    parts.append(res.get('snippet') or res.get('title') or res.get('content') or '')
                reconstructed = '\n'.join(p for p in parts if p)
            if not reconstructed:
                break
            r['toolContent'] = reconstructed or '[tool result not available]'
        if has_llm_round:
            batch_key = r.get('llmRound')
        else:
            prev = all_rounds[i - 1] if i > 0 else None
            if prev and prev.get('toolCallId') and r.get('roundNum', 0) > prev.get('roundNum', -999) + 1:
                batch_key += 1
        batches.setdefault(batch_key, []).append(r)
        last_complete_idx = i

    if last_complete_idx < 0:
        return None

    kept_rounds = all_rounds[:last_complete_idx + 1]
    discarded_rounds = len(all_rounds) - len(kept_rounds)

    # Keep the full checkpoint/audit list on the message, but replay only the
    # newest effective checklist revision to the model.
    from lib.tools.todo import compact_todo_rounds_for_replay
    replay_rounds = compact_todo_rounds_for_replay(kept_rounds)
    replay_batches: dict = {}
    replay_key = 0
    replay_prev = None
    replay_has_llm = any(r.get('llmRound') is not None for r in replay_rounds)
    for r in replay_rounds:
        if replay_has_llm:
            replay_key = r.get('llmRound')
        elif (replay_prev and r.get('roundNum', 0)
              > replay_prev.get('roundNum', -999) + 1):
            replay_key += 1
        replay_batches.setdefault(replay_key, []).append(r)
        replay_prev = r
    tool_history = [build_tool_history_round(batch)
                    for batch in replay_batches.values()]

    preserved_content_parts = [r.get('assistantContent') or '' for r in kept_rounds]
    preserved_content = '\n\n'.join(p for p in preserved_content_parts if p)
    original_content = assistant_msg.get('content') or ''
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

    preserved_thinking_chars = sum(len(r.get('thinking') or '') for r in kept_rounds)
    original_thinking = assistant_msg.get('thinking') or ''
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
    'set_send_translate_status',
    'clear_send_translate_status',
    'get_send_translate_status',
]
