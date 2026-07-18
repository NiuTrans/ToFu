"""Single-chunk translation engine.

Tries (in order):
    1. Translation cache lookup.
    2. Configured machine-translation provider (NiuTrans etc.).
    3. LLM dispatch (cheap-tier) with an aggressive retry loop that
       penalises slots / excludes models on empty / truncated / no-op
       output.

Returns ``(translated_text, usage_dict)``. The usage dict carries
``_dispatch.model`` and ``_dispatch.key`` so the caller can identify
which (key, model) actually produced the output.
"""

import re
import time

from lib import translate_cache
from lib.log import get_logger

from ..dedup import _dedup_repetition_loop
from ..prompt import _wrap_for_translation
from ._split import _ends_midsentence

logger = get_logger(__name__)

# ── Over-generation guard thresholds (symmetric to the truncation floors) ──
# A translation should be roughly the same order of magnitude as its input.
# When a cheap model finishes the real translation of a SHORT input then keeps
# going — appending a large block of unrelated training-corpus text (the
# "poetry bleed" contamination: a 45→2373 char, 52x expansion committed as
# verdict=ok because every other guard only defends against SHORT output) —
# reject it. Both floors must be crossed together so a legitimately terse input
# ("你好" → a short sentence) is never killed: the output must be BOTH more than
# _OVERGEN_RATIO× the input AND more than _OVERGEN_ABS chars longer than it.
# Module-level so tests can monkeypatch (a NEUTER control raises them so the
# guard cannot fire, proving it is load-bearing).
_OVERGEN_RATIO = 8.0
_OVERGEN_ABS = 800
# Below this input length the ratio is meaningless (a 3-char input trivially
# expands), so the guard is disabled — matches the truncation guard's own
# length gates.
_OVERGEN_MIN_INPUT = 20

# ── Identity-invariant content detection ──
# A URL or filesystem-path token (whole content, no internal whitespace).
# Covers scheme://… URLs and POSIX / home / relative / Windows-drive paths.
_URL_OR_PATH_RE = re.compile(
    r'^(?:'
    r'[A-Za-z][A-Za-z0-9+.\-]*://'   # scheme://  (http, https, ftp, file, ws…)
    r'|/'                            # POSIX absolute
    r'|~/'                           # home-relative
    r'|\.{1,2}/'                     # ./ or ../ relative
    r'|[A-Za-z]:[\\/]'               # Windows drive
    r')\S*$'
)
# Any Unicode letter (script-agnostic — CJK, Latin, Cyrillic, kana, …). Content
# with NO letter at all (digits / symbols / punctuation / whitespace only) has
# nothing to translate in any language. ``[^\W\d_]`` = a word char that is
# neither a digit nor an underscore = a letter.
_HAS_LETTER_RE = re.compile(r'[^\W\d_]', re.UNICODE)


def _is_identity_invariant(text, target):
    """Return ``(bool, reason)`` — True when ``text`` legitimately equals its
    own translation, so no model call is needed.

    Three shapes never need translating and, worse, provoke the no-op detector
    into burning the whole retry budget across many models when the model
    correctly echoes them — surfacing a ``ValueError`` for content that never
    needed a translation (observed: a 98-char absolute path burned 9 model
    attempts, task ``inc-translate-89dd9bf7``):

    * (a) no translatable letters — pure symbols / digits / punctuation.
    * (b) a single path or URL token (whole content, no internal whitespace).
    * (c) already predominantly in the target language with negligible foreign
          script — there is essentially nothing to translate. A MIXED
          bilingual message is deliberately EXCLUDED (its foreign part still
          needs the model), so the threshold is stricter than the plain
          "predominantly target" ratio.

    Pure predicate (no I/O) so the caller can short-circuit before any MT / LLM
    call. Deliberately conservative: a false negative merely falls through to
    the normal path (one wasted call at worst); a false positive would skip a
    genuinely-needed translation, so the bar is set to avoid that.
    """
    if not text:
        return False, ''
    # (a) no translatable letters anywhere.
    if not _HAS_LETTER_RE.search(text):
        return True, 'no translatable letters (symbols/digits only)'
    # (b) a lone path / URL token (single token, no internal whitespace).
    if not any(ch.isspace() for ch in text) and _URL_OR_PATH_RE.match(text):
        return True, 'path-or-URL token'
    # (c) already in the target language (monolingual, negligible foreign script).
    from lib.text_lang import (
        cjk_ratio, is_predominantly_chinese, is_predominantly_english,
        latin_ratio,
    )
    tl = (target or '').lower()
    _is_zh_target = (tl.startswith('chinese') or 'zh' in tl)
    _is_en_target = (tl == 'en' or tl.startswith('english')
                     or tl.startswith('en-') or tl.startswith('en_'))
    if _is_zh_target:
        if is_predominantly_chinese(text) and latin_ratio(text) < 0.15:
            return True, 'source already Chinese (target Chinese)'
    elif _is_en_target:
        if is_predominantly_english(text) and cjk_ratio(text) < 0.05:
            return True, 'source already English (target English)'
    return False, ''


def _build_trace(*, path, model, in_chars, out_chars, attempts=1,
                 content_fails=0, dispatch_fails=0, elapsed=0.0,
                 verdict='ok', target=''):
    """Build a structured provenance record for one translation call.

    Attached to the returned usage dict as ``_translate_trace`` and emitted
    as a single grep-able ``[Translate:trace]`` log line so every
    auto-translate leaves a machine-parseable audit trail (path taken,
    model, attempts, in/out length, ratio, completeness verdict).
    """
    return {
        'path': path, 'model': model, 'in_chars': in_chars,
        'out_chars': out_chars,
        'ratio': round(out_chars / in_chars, 3) if in_chars else 0.0,
        'attempts': attempts, 'content_fails': content_fails,
        'dispatch_fails': dispatch_fails, 'elapsed': round(elapsed, 1),
        'verdict': verdict, 'target': target,
    }


def _translate_freetext(text, system_prompt, chunk_label='',
                        source='', target='', status_cb=None,
                        progress_cb=None, overall_deadline=None,
                        use_cache=True):
    """Translate free-text in a SINGLE LLM call — no input splitting.

    Public free-text entry point used by every caller (server auto-translate,
    sync ``/api/v1/translate``, the chat send-path, the legacy message queue,
    and the re-translate script).  Forwards the WHOLE text to
    ``_translate_one_chunk``; the trailing ``chunk`` vocabulary is historical.

    Input splitting was tried and removed: cutting a message into pieces
    isolates an already-target-language tail (or severs a ``` fence) into its
    own call, where a weak cheap model "translates" it into the wrong language
    or echoes it verbatim.  A whole-document call lets the prompt's mixed-
    language rule keep already-target text intact while translating the rest,
    and the engine's truncation detector + model-rotation retry cover the
    long-input case the split was meant to solve.
    """
    return _translate_one_chunk(
        text, system_prompt, chunk_label=chunk_label,
        source=source, target=target, status_cb=status_cb,
        progress_cb=progress_cb, overall_deadline=overall_deadline,
        use_cache=use_cache)


def _translate_one_chunk(chunk, system_prompt, chunk_label='',
                         source='', target='', status_cb=None,
                         progress_cb=None, overall_deadline=None,
                         use_cache=True):
    """Translate a single chunk of text.

    If a machine translation provider is configured (Settings → 机器翻译),
    uses that directly — no LLM prompt needed, faster and cheaper.
    Falls back to LLM cheap model if MT is not configured or fails.

    Includes truncation detection: if the model produces suspiciously short
    output (< 30% of input) or hits max_tokens (finish_reason='length'),
    the translation is retried with a fresh dispatch (likely a different model).

    Args:
        chunk: Text to translate.
        system_prompt: LLM system prompt (used only for LLM fallback).
        chunk_label: Label for logging (e.g. ':chunk1/3').
        source: Source language name/code (for MT provider).
        target: Target language name/code (for MT provider).
        status_cb: Optional callback ``fn(dict)`` invoked when a transient
            retry happens (rate-limit, dispatch error, empty/truncated
            output).  The dict carries ``{kind, attempt, elapsed, detail}``
            and is surfaced to the frontend poll endpoint as
            ``statusMessage`` so users see WHY a translation is slow.
        progress_cb: Optional callback ``fn(text_so_far)`` invoked as
            streamed text deltas arrive during the LLM path.  When set,
            the LLM dispatch switches to ``dispatch_stream`` so the
            frontend can render a live preview of the translation.
            Ignored on the MT-provider fast path (single HTTP, no stream).
            Cache hits skip the callback entirely.
        overall_deadline: Optional per-call wall-clock budget in seconds.
            The retry loop refuses to start a new attempt after this many
            seconds and raises ``ValueError`` if no result was produced.
            Defaults to 600 s (10 min) which fits the background async-
            translate path; the synchronous send-path wrapper passes a
            tighter budget so the HTTP request returns within the
            frontend's safety window.
    """
    def _notify(kind, attempt, elapsed, detail=''):
        """Safely invoke status_cb — never let caller's callback break us."""
        if status_cb is None:
            return
        try:
            status_cb({
                'kind': kind,
                'attempt': attempt,
                'elapsed': elapsed,
                'detail': detail,
            })
        except Exception as e:
            logger.debug('[Translate%s] status_cb failed: %s', chunk_label, e)

    # ── Cache lookup (sha256 of (target, source, text)) ──
    # use_cache=False forces a fresh translation — used by the truncation
    # repair script, where the on-disk cache may hold the SAME truncated
    # output we are trying to replace (it was put() there by the original
    # bad run).  The fresh result still refreshes the cache via put() below.
    cached = translate_cache.get(chunk, source, target) if use_cache else None
    if cached and cached.get('translated'):
        cached_text = cached['translated']
        cached_model = cached.get('model', '') or 'cache'
        logger.info('[Translate%s] Cache hit: %d→%d chars model=%s',
                    chunk_label, len(chunk), len(cached_text), cached_model)
        return cached_text, {'model': cached_model,
                             '_dispatch': {'model': cached_model},
                             '_cache_hit': True,
                             '_translate_trace': _build_trace(
                                 path='cache', model=cached_model,
                                 in_chars=len(chunk), out_chars=len(cached_text),
                                 verdict='cache', target=target)}

    # ── Identity-invariant short-circuit ──
    # Some content legitimately equals its own translation: a lone path/URL,
    # pure symbols/digits, or text already in the target language. Sending it
    # through the LLM makes a correct model echo it verbatim, which the no-op
    # detector below reads as a FAILURE — burning the whole retry budget across
    # many models and finally raising ValueError for content that never needed
    # translating (observed: a 98-char absolute path burned 9 model attempts,
    # task inc-translate-89dd9bf7). Accept it verbatim BEFORE the retry loop.
    _inv, _inv_reason = _is_identity_invariant(chunk.strip(), target)
    if _inv:
        _passthrough = chunk.strip()
        logger.info('[Translate%s] Identity-invariant content accepted verbatim '
                    '(%d chars, reason=%s) — skipping translation',
                    chunk_label, len(_passthrough), _inv_reason)
        if use_cache:
            translate_cache.put(chunk, source, target, _passthrough,
                                model='identity')
        return _passthrough, {
            'model': 'identity', '_dispatch': {'model': 'identity'},
            '_identity_invariant': True,
            '_translate_trace': _build_trace(
                path='identity', model='identity', in_chars=len(chunk),
                out_chars=len(_passthrough), verdict='identity', target=target)}

    # ── Try dedicated MT provider first (if configured) ──
    from lib.mt_provider import is_mt_configured, mt_translate_chunked
    if is_mt_configured():
        try:
            t0 = time.time()
            # mt_translate_chunked handles NiuTrans 5000-char limit internally
            result = mt_translate_chunked(chunk, source=source, target=target)
            elapsed = time.time() - t0
            logger.info('[Translate%s] MT provider: %d→%d chars in %.1fs',
                        chunk_label, len(chunk), len(result), elapsed)
            translate_cache.put(chunk, source, target, result, model='mt:niutrans')
            # Return with a synthetic usage dict for compatibility
            return result, {'model': 'mt:niutrans', '_dispatch': {'model': 'mt:niutrans'},
                            '_translate_trace': _build_trace(
                                path='mt', model='mt:niutrans',
                                in_chars=len(chunk), out_chars=len(result),
                                elapsed=elapsed, verdict='mt', target=target)}
        except Exception as e:
            logger.warning('[Translate%s] MT provider failed, falling back to LLM: %s',
                           chunk_label, e)
            _notify('mt_fallback', 1, 0, str(e)[:120])
            # Fall through to LLM translation below

    from lib.llm_dispatch import dispatch_stream, smart_chat
    from lib.llm_dispatch.factory import get_dispatcher
    from lib.llm import RateLimitError

    clen = len(chunk)
    _target_is_chinese = ((target or '').lower().startswith('chinese')
                          or 'zh' in (target or '').lower())
    # Bigger per-attempt budget + more dispatch retries per attempt.
    if clen > 6000:
        _mt, _timeout, _retries = 16000, 180, 12
    elif clen > 3000:
        _mt, _timeout, _retries = 12000, 120, 10
    else:
        _mt, _timeout, _retries = 8000, 60, 8

    # ── Aggressive retry policy ──
    # Rationale: auto-translate is a background task.  A user waiting for
    # the "译文" toggle expects it to eventually succeed if ANY model/key
    # recovers.  So:
    #   • Rate-limit / dispatch failures → retry indefinitely with backoff
    #     (up to a generous wall-clock budget).
    #   • Empty / truncated output       → retry up to _MAX_CONTENT_RETRIES
    #     times (different model usually fixes it; after that the issue
    #     is more likely with the input itself).
    _MAX_CONTENT_RETRIES = 5
    # Wall-clock budget for the whole retry loop. Defaults to 10min so the
    # background async-translate path can ride out a quota reset; the
    # send-path wrapper passes a tighter budget (see routes/chat.py).
    _OVERALL_DEADLINE_SEC = float(overall_deadline) if overall_deadline is not None else 600.0
    _BACKOFF_MIN = 1.0
    _BACKOFF_MAX = 30.0

    _start_ts = time.time()
    _attempt = 0
    _content_fail_count = 0
    _dispatch_fail_count = 0
    _last_err = None
    _terminal_verdict = 'ok'
    c, u = '', None
    # ── Models proven bad for THIS chunk ──
    # When a model returns an empty or truncated body, we add it here so the
    # next dispatch_stream / smart_chat call skips it. Without this the slot
    # picker keeps choosing the same low-latency cheap model (e.g. MiniMax-M2.5)
    # and burns the whole _MAX_CONTENT_RETRIES budget on identical failures.
    _excluded_models: set[str] = set()
    _dispatcher = get_dispatcher()

    # Emit a 'started' event up front so callers polling the status
    # side-channel see something within the first poll tick — without
    # this, a slow first attempt with no retries leaves the user staring
    # at a static "Translating…" with no indication anything is happening.
    _notify('started', 1, 0, f'starting translation ({len(chunk)} chars)')

    while True:
        _attempt += 1
        _elapsed = time.time() - _start_ts
        if _elapsed >= _OVERALL_DEADLINE_SEC:
            logger.error('[Translate%s] Giving up after %.0fs (attempts=%d, '
                         'content_fails=%d, dispatch_fails=%d, last_err=%s)',
                         chunk_label, _elapsed, _attempt - 1,
                         _content_fail_count, _dispatch_fail_count, _last_err)
            break

        try:
            _msgs = [{'role': 'system', 'content': system_prompt},
                     {'role': 'user', 'content': _wrap_for_translation(chunk)}]
            if progress_cb is not None:
                # Streamed path — frontend gets live preview as text arrives.
                _stream_buf = []

                def _on_content_delta(delta):
                    if not delta:
                        return
                    _stream_buf.append(delta)
                    try:
                        progress_cb(''.join(_stream_buf))
                    except Exception as cb_err:
                        logger.debug('[Translate%s] progress_cb failed: %s',
                                     chunk_label, cb_err)

                _stream_msg, _finish, _usage = dispatch_stream(
                    _msgs,
                    on_content=_on_content_delta,
                    max_tokens=_mt,
                    temperature=1,
                    capability='cheap',
                    log_prefix=f'[Translate{chunk_label}]',
                    max_retries=_retries,
                    exclude_models=_excluded_models or None,
                )
                # Match smart_chat's contract: c is the assistant content
                # string, u is the usage dict with finish_reason embedded
                # so the truncation detector below still works.
                # dispatch_stream returns the assistant message as a dict
                # ({'role': 'assistant', 'content': '...'}); unwrap it.
                if isinstance(_stream_msg, dict):
                    c = _stream_msg.get('content', '') or ''
                else:
                    c = _stream_msg or ''
                u = dict(_usage or {})
                if _finish:
                    u.setdefault('finish_reason', _finish)
            else:
                c, u = smart_chat(
                    messages=_msgs,
                    max_tokens=_mt,
                    temperature=1,
                    capability='cheap',
                    log_prefix=f'[Translate{chunk_label}]',
                    timeout=_timeout,
                    max_retries=_retries,
                    exclude_models=_excluded_models or None,
                )
        except RateLimitError as re_err:
            # All keys temporarily rate-limited — wait and retry forever
            # (within the overall deadline).  This is the most common transient
            # failure and users expect the translation to eventually land.
            _dispatch_fail_count += 1
            _last_err = f'RateLimitError: {re_err}'
            _sleep = min(_BACKOFF_MAX, _BACKOFF_MIN * (2 ** min(_dispatch_fail_count - 1, 5)))
            logger.warning('[Translate%s] All keys rate-limited (attempt %d, '
                           'total_rl_fails=%d, elapsed=%.0fs) — sleeping %.1fs and retrying',
                           chunk_label, _attempt, _dispatch_fail_count, _elapsed, _sleep)
            _notify('rate_limited', _attempt, _elapsed,
                    f'all keys busy (fails={_dispatch_fail_count}, retry in {_sleep:.0f}s)')
            time.sleep(_sleep)
            continue
        except Exception as se:
            # Other dispatch errors (network, timeout, bad payload, etc.) —
            # retry a few times but don't loop forever.
            _dispatch_fail_count += 1
            _last_err = f'dispatch error: {se}'
            if _dispatch_fail_count >= _MAX_CONTENT_RETRIES + 1:
                logger.error('[Translate%s] Too many dispatch failures (%d): %s',
                             chunk_label, _dispatch_fail_count, se, exc_info=True)
                _notify('dispatch_failed_final', _attempt, _elapsed, str(se)[:160])
                break
            _sleep = min(_BACKOFF_MAX, _BACKOFF_MIN * (2 ** min(_dispatch_fail_count - 1, 5)))
            logger.warning('[Translate%s] smart_chat raised (attempt %d, fails=%d): %s '
                           '— sleeping %.1fs and retrying',
                           chunk_label, _attempt, _dispatch_fail_count, se, _sleep)
            _notify('dispatch_error', _attempt, _elapsed,
                    f'{type(se).__name__}: {str(se)[:120]}')
            time.sleep(_sleep)
            continue

        # ★ Coerce structured Anthropic-style content (list of blocks
        #   like [{"type": "text", "text": "..."}, ...]) and stray dicts
        #   into a plain string so the regexes below don't crash with
        #   "expected string or bytes-like object, got 'dict'/'list'".
        if isinstance(c, list):
            try:
                c = ''.join(
                    b.get('text', '') for b in c
                    if isinstance(b, dict) and b.get('type') == 'text'
                )
            except Exception as _ce:
                logger.warning('[Translate%s] Failed to flatten list content: %s',
                               chunk_label, _ce)
                c = ''
        elif isinstance(c, dict):
            c = c.get('text', '') or c.get('content', '') or ''
        if not isinstance(c, str):
            c = str(c) if c is not None else ''

        # Strip thinking blocks and translate tag wrappers
        if c and '<think>' in c:
            c = re.sub(r'<think>[\s\S]*?</think>\s*', '', c).strip()
            if '<think>' in c:
                c = c[:c.index('<think>')].strip()
        c = re.sub(r'</?translate>', '', c).strip()

        # Identify which (key, model) just produced this output — needed for
        # both the empty and the truncated branches below to penalize the
        # slot and exclude the model on the next retry.
        _used_key = ''
        _used_model = ''
        if isinstance(u, dict):
            _disp_now = u.get('_dispatch') or {}
            _used_key = _disp_now.get('key', '')
            _used_model = _disp_now.get('model', u.get('model', ''))

        if not c or not c.strip():
            _content_fail_count += 1
            _last_err = f'empty result (len={len(chunk)})'
            # Penalize the slot in the dispatcher's score AND exclude this
            # model from the next pick — otherwise we'd just re-pick it.
            if _used_key and _used_model:
                _dispatcher.record_truncation(_used_key, _used_model,
                                              error='empty translation output')
                _excluded_models.add(_used_model)
            if _content_fail_count >= _MAX_CONTENT_RETRIES:
                logger.error('[Translate%s] Still empty after %d content retries — giving up',
                             chunk_label, _content_fail_count)
                _notify('empty_final', _attempt, _elapsed,
                        f'empty after {_content_fail_count} retries')
                break
            logger.warning('[Translate%s] Empty translation from %s (attempt %d, '
                           'content_fails=%d) — excluding model and retrying',
                           chunk_label, _used_model or '?', _attempt, _content_fail_count)
            _notify('empty_output', _attempt, _elapsed,
                    f'empty result, retrying (fails={_content_fail_count})')
            c = ''
            continue

        # ── Detect truncated translations ──
        _finish = (u or {}).get('finish_reason', '')
        # Reuse _used_model/_used_key extracted above for slot penalisation.
        _model = _used_model

        _is_truncated = False
        _reason = ''

        # EN→ZH output is naturally ~0.4-0.7x the source length, so a flat
        # 20% floor lets a translation that silently dropped HALF the
        # document (the dominant 漏译 mode — cheap models stop early with
        # finish_reason=stop) sail through undetected. Raise the floor for
        # Chinese targets so those drops are caught and retried.
        _short_floor = 0.30 if _target_is_chinese else 0.20
        # A SOFT floor (above the hard floor) that only bites when the output
        # ALSO ended mid-sentence — this catches the finish_reason=stop early
        # stop that lands between the hard floor and a full translation (the
        # exact bug: a 40%-length body ending on a bare word like "…are you"
        # cleared the 0.30 hard floor and was accepted). Ratio alone can't
        # raise the hard floor (a legit terse EN→ZH is ~0.35-0.5), so we
        # require the mid-sentence signal to avoid false positives.
        _soft_trunc_floor = 0.45 if _target_is_chinese else 0.35
        if _finish == 'length':
            _is_truncated = True
            _reason = f'finish_reason=length, model={_model}'
        elif clen > 500 and len(c) < clen * _short_floor:
            _is_truncated = True
            _reason = (f'output too short ({len(c)}/{clen} = {len(c)/clen*100:.0f}%, '
                       f'floor={_short_floor:.0%}), model={_model}')
        elif (clen > 200 and len(c) < clen * _soft_trunc_floor
              and _ends_midsentence(c)):
            _is_truncated = True
            _reason = (f'ends mid-sentence + short ({len(c)}/{clen} = '
                       f'{len(c)/clen*100:.0f}%, soft floor={_soft_trunc_floor:.0%}, '
                       f"tail={c.rstrip()[-16:]!r}), model={_model}")

        if _is_truncated:
            _content_fail_count += 1
            _last_err = _reason
            # Penalize the slot in the global score AND exclude this model
            # from the next pick. Without this the dispatcher keeps picking
            # MiniMax-M2.5 (lowest score in the cheap tier) every retry.
            if _used_key and _model:
                _dispatcher.record_truncation(_used_key, _model, error=_reason)
                _excluded_models.add(_model)
            if _content_fail_count >= _MAX_CONTENT_RETRIES:
                logger.warning('[Translate%s] Still truncated after %d content retries: %s '
                               '— accepting partial result (%d chars)',
                               chunk_label, _content_fail_count, _reason, len(c))
                _notify('truncated_final', _attempt, _elapsed,
                        f'truncated after {_content_fail_count} retries')
                # 溯源: committing a KNOWN-incomplete translation is a silent
                # failure unless we leave a loud, machine-parseable trail.
                try:
                    from lib.log import audit_log
                    audit_log('translate_truncated_accept',
                              chunk_label=chunk_label, model=_model,
                              in_chars=clen, out_chars=len(c),
                              ratio=round(len(c) / max(1, clen), 3),
                              content_fails=_content_fail_count,
                              reason=_reason, target=target)
                except Exception as _ae:
                    logger.debug('[Translate%s] audit_log failed: %s', chunk_label, _ae)
                _terminal_verdict = 'truncated'
                break  # accept best-effort
            logger.warning('[Translate%s] Truncated translation (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, _attempt, _content_fail_count, _reason)
            _notify('truncated', _attempt, _elapsed,
                    f'output truncated, retrying (fails={_content_fail_count})')
            continue

        # ── No-op detection: model echoed input verbatim instead of translating ──
        # Cheap models (e.g. deepseek-v4-flash) sometimes return the input
        # unchanged when the chunk is heavy on code/identifiers — they read
        # the prompt's "code identifiers stay original" carve-out as license
        # to copy the whole thing.  Detect by exact match (after stripping)
        # and by target-language-character ratio, then exclude the model and
        # retry with a different one.
        from lib.text_lang import (
            cjk_ratio, is_predominantly_chinese, is_predominantly_english,
        )
        _is_noop = False
        _noop_reason = ''
        _c_stripped = c.strip()
        _src_stripped = chunk.strip()
        # When the source is ALREADY in the target language, verbatim output
        # is a legitimate pass-through, not an echo. Translating Chinese→Chinese
        # corre
        _target_is_chinese = ((target or '').lower().startswith('chinese')
                              or 'zh' in (target or '').lower())
        _src_already_target = _target_is_chinese and is_predominantly_chinese(_src_stripped)
        if (not _src_already_target
                and _c_stripped == _src_stripped and len(_src_stripped) >= 40):
            _is_noop = True
            _noop_reason = f'output==input verbatim ({len(_c_stripped)} chars), model={_model}'
        elif (target or '').lower().startswith('chinese') or 'zh' in (target or '').lower():
            # Translating TO Chinese — output must contain a meaningful CJK ratio.
            # Skip the check on tiny chunks (likely all code/URLs) and on inputs
            # that are already Chinese (rule 7 scenario the prompt no longer covers).
            if len(_c_stripped) >= 200 and cjk_ratio(_src_stripped) < 0.1:
                _out_cjk = cjk_ratio(_c_stripped)
                if _out_cjk < 0.05:
                    _is_noop = True
                    _noop_reason = (f'output cjk_ratio={_out_cjk:.2%} for target=Chinese '
                                    f'(len={len(_c_stripped)}), model={_model}')

        if _is_noop:
            _content_fail_count += 1
            _last_err = _noop_reason
            if _used_key and _model:
                _dispatcher.record_truncation(_used_key, _model, error=_noop_reason)
                _excluded_models.add(_model)
            if _content_fail_count >= _MAX_CONTENT_RETRIES:
                logger.error('[Translate%s] Still no-op after %d content retries: %s '
                             '— giving up, will surface error',
                             chunk_label, _content_fail_count, _noop_reason)
                _notify('noop_final', _attempt, _elapsed,
                        f'no-op output after {_content_fail_count} retries')
                # Force the trailing emptiness check below to raise, since
                # accepting an identical-to-input "translation" is worse than
                # failing — it would clobber any earlier good translation.
                c = ''
                break
            logger.warning('[Translate%s] No-op translation (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, _attempt, _content_fail_count, _noop_reason)
            _notify('noop_output', _attempt, _elapsed,
                    f'output identical to input, retrying (fails={_content_fail_count})')
            continue

        # ── Wrong-language flip detection ──
        # The no-op CJK check above only fires when the SOURCE is almost pure
        # English (cjk_ratio < 0.1). It therefore misses the dominant failure
        # on MIXED English+Chinese input (target=Chinese): the model performs a
        # language NORMALISATION and rewrites the already-Chinese majority INTO
        # English to satisfy the prompt's anti-verbatim rule — so a source that
        # was English-then-Chinese comes back predominantly English. The prompt
        # forbids this, but weak cheap models do it anyway, so we verify the
        # OUTCOME instead of trusting the instruction: when target=Chinese and
        # the source carried meaningful Chinese, the output must NOT be
        # predominantly English. Reject + exclude the model + retry — a
        # different model usually keeps the Chinese intact.
        _is_flip = False
        _flip_reason = ''
        if (_target_is_chinese and len(_c_stripped) >= 200
                and cjk_ratio(_src_stripped) >= 0.10
                and is_predominantly_english(_c_stripped)):
            _is_flip = True
            _flip_reason = (
                f'wrong-language flip: target=Chinese, source cjk_ratio='
                f'{cjk_ratio(_src_stripped):.2f} but output latin-dominant '
                f'(out cjk={cjk_ratio(_c_stripped):.2f}, len={len(_c_stripped)}), '
                f'model={_model}')

        if _is_flip:
            _content_fail_count += 1
            _last_err = _flip_reason
            if _used_key and _model:
                _dispatcher.record_truncation(_used_key, _model, error=_flip_reason)
                _excluded_models.add(_model)
            if _content_fail_count >= _MAX_CONTENT_RETRIES:
                logger.error('[Translate%s] Still wrong-language after %d content retries: %s '
                             '— giving up, will surface error',
                             chunk_label, _content_fail_count, _flip_reason)
                _notify('wrong_language_final', _attempt, _elapsed,
                        f'output flipped language after {_content_fail_count} retries')
                # Refuse to commit a flipped translation — it would clobber the
                # already-Chinese source with English. Force the trailing
                # emptiness check to raise.
                c = ''
                break
            logger.warning('[Translate%s] Wrong-language flip (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, _attempt, _content_fail_count, _flip_reason)
            _notify('wrong_language', _attempt, _elapsed,
                    f'output flipped to wrong language, retrying (fails={_content_fail_count})')
            continue

        # ── Over-generation detection (the mirror of truncation) ──
        # Every guard above defends against output that is TOO SHORT. None
        # catches the opposite failure: the model translates the input
        # correctly, then keeps generating a large block of unrelated text
        # (hallucinated web-page / training-corpus prose). On a short input
        # this is unmistakable — a 45-char message must not translate to
        # 2373 chars (52x). Require BOTH an outsized ratio AND a large
        # absolute increase so a legitimately terse input ("你好" → a full
        # sentence) is never falsely rejected. Same remediation shape as
        # no-op/flip: exclude the model + retry; refuse to commit after the
        # budget (committing the contamination is worse than failing loudly).
        _is_overgen = False
        _overgen_reason = ''
        if (clen >= _OVERGEN_MIN_INPUT
                and len(_c_stripped) > clen * _OVERGEN_RATIO
                and len(_c_stripped) - clen > _OVERGEN_ABS):
            _is_overgen = True
            _overgen_reason = (
                f'over-generated: output {len(_c_stripped)}/{clen} = '
                f'{len(_c_stripped)/clen:.1f}x input (ratio floor={_OVERGEN_RATIO:.0f}x, '
                f'abs floor={_OVERGEN_ABS}), model={_model}')

        if _is_overgen:
            _content_fail_count += 1
            _last_err = _overgen_reason
            if _used_key and _model:
                _dispatcher.record_truncation(_used_key, _model, error=_overgen_reason)
                _excluded_models.add(_model)
            if _content_fail_count >= _MAX_CONTENT_RETRIES:
                logger.error('[Translate%s] Still over-generated after %d content retries: %s '
                             '— giving up, will surface error',
                             chunk_label, _content_fail_count, _overgen_reason)
                _notify('over_generated_final', _attempt, _elapsed,
                        f'over-generated after {_content_fail_count} retries')
                # 溯源: symmetric to translate_truncated_accept — leave a loud,
                # machine-parseable trail for the rejected contamination.
                try:
                    from lib.log import audit_log
                    audit_log('translate_over_generated_reject',
                              chunk_label=chunk_label, model=_model,
                              in_chars=clen, out_chars=len(_c_stripped),
                              ratio=round(len(_c_stripped) / max(1, clen), 3),
                              content_fails=_content_fail_count,
                              reason=_overgen_reason, target=target)
                except Exception as _ae:
                    logger.debug('[Translate%s] audit_log failed: %s', chunk_label, _ae)
                # Refuse to commit the over-generated body — it would persist a
                # correct translation fused with unrelated hallucinated text.
                # Force the trailing emptiness check to raise.
                _terminal_verdict = 'over_generated'
                c = ''
                break
            logger.warning('[Translate%s] Over-generated translation (attempt %d, content_fails=%d): %s '
                           '— excluding model and retrying',
                           chunk_label, _attempt, _content_fail_count, _overgen_reason)
            _notify('over_generated', _attempt, _elapsed,
                    f'output over-generated, retrying (fails={_content_fail_count})')
            continue
        break  # success

    # If every attempt produced an empty result, raise a clear error so the
    # caller can decide how to handle it (e.g. mark the task as failed).
    if not c or not c.strip():
        raise ValueError(
            f'Empty translation result for chunk{chunk_label} after '
            f'{_attempt} attempts ({_dispatch_fail_count} dispatch fails, '
            f'{_content_fail_count} content fails, elapsed={time.time() - _start_ts:.0f}s, '
            f'last_err={_last_err})'
        )

    # ── Post-processing: detect and truncate repetition loops ──
    c, was_truncated = _dedup_repetition_loop(c)
    if was_truncated:
        logger.info('[Translate%s] Repetition loop cleaned: %d chars after dedup',
                    chunk_label, len(c))

    final = c.strip()
    _model_for_cache = ''
    if isinstance(u, dict):
        _disp = u.get('_dispatch', {}) or {}
        _model_for_cache = _disp.get('model', u.get('model', '')) or ''
    # Do NOT persist a KNOWN-incomplete translation: caching a truncated body
    # pins the partial forever (every later request served the same cut-off
    # text — the reported "displayed incompletely" bug). A fresh request then
    # re-translates and a healthier model can produce a complete result.
    if _terminal_verdict != 'truncated':
        translate_cache.put(chunk, source, target, final, model=_model_for_cache)
    else:
        logger.warning('[Translate%s] NOT caching truncated result '
                       '(%d/%d chars) so a later request can re-translate',
                       chunk_label, len(final), clen)

    # ── 溯源: full lifecycle trace attached to the usage dict + one
    #    structured, grep-able line. Flags suspiciously-short output even
    #    when it cleared the truncation floor, so silent partial drops stay
    #    visible after the fact. ──
    _ratio = len(final) / clen if clen else 0.0
    _soft_floor = 0.40 if _target_is_chinese else 0.30
    _suspicious = bool(clen > 500 and _ratio < _soft_floor
                       and _terminal_verdict == 'ok')
    _trace = _build_trace(
        path='llm', model=_model_for_cache or '?', in_chars=clen,
        out_chars=len(final), attempts=_attempt,
        content_fails=_content_fail_count, dispatch_fails=_dispatch_fail_count,
        elapsed=time.time() - _start_ts, verdict=_terminal_verdict,
        target=target)
    _trace['suspicious'] = _suspicious
    logger.info('[Translate:trace%s] verdict=%s%s path=llm model=%s in=%d out=%d '
                'ratio=%.2f attempts=%d cfail=%d dfail=%d elapsed=%.1fs',
                chunk_label, _terminal_verdict,
                ' SUSPICIOUS-SHORT' if _suspicious else '',
                _trace['model'], clen, len(final), _ratio, _attempt,
                _content_fail_count, _dispatch_fail_count, _trace['elapsed'])
    if isinstance(u, dict):
        u['_translate_trace'] = _trace

    return final, u
